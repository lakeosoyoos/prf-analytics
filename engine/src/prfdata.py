"""PRF grid data fetcher: historical rainfall indices + premium rates + subsidy.

Feeds the PRF interval-allocation optimizer. Two authoritative RMA sources, cross-verified
against each other for grid 27663 (Idaho, Blaine/Camas/Lincoln) before either was trusted:

1. PRF support tool web API (the backend of https://public-rma.fpac.usda.gov/apps/prf —
   endpoints discovered from the app's Scripts/Services/PRFController.js):
     PrfExternalStates/GetCountiesAndStatesFromGridId?gridId=&gridName=
     PrfExternalIndexes/GetIndexValues?intervalType=BiMonthly&sampleYearMinimum=
         &sampleYearMaximum=&gridId=                      -> 1948..present indices
     PrfExternalIntervalCodes/GetValidIntervalCodes?intervalType=BiMonthly
         &irrigationPracticeCode=&organicPracticeCode=&intendedUseCode=&stateCode=
         &countyCode=&coverageLevelPercent=&gridId=&gridName=  -> 11 premium rates
2. RMA ADM (actuarial filing): A00810 Price rc03 rows give ADM Insurance Offer ID per
   grid x use x interval; A01130 Area Coverage Level rc05 (grid-keyed) maps offer x
   coverage level -> Area Rate ID; A01135 Area Rate carries the Base Rate. For grid 27663
   Grazing the ADM chain reproduces the API's premium rates EXACTLY at all 5 coverage
   levels x 11 intervals, and the A00070 Subsidy Percent rc04 plan-13 rows match the
   API's SubsidyLevel. Interval code names come from ADM A00480 (625=JAN-FEB..635=NOV-DEC).

The API is the fetch path (one small JSON per grid for indices, five per use for rates);
the ADM files stay the audit trail under data/cache/adm/. API responses are cached as JSON
under data/cache/prf/ so a re-run never re-hits RMA, and live calls go through http.Client
(per-host throttle, retries, project User-Agent).

index_value semantics: decimal percent-of-normal (1.000 = normal; indemnity triggers when
the final index < coverage level). premium_rate semantics: unloaded rate on policy
protection (total premium = protection x rate); producer premium = total x (1 - subsidy).

CLI:  .venv/bin/python -m src.prfdata --grid 27663 [--use Grazing] [--force]
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, http

API_BASE = "https://public-rma.fpac.usda.gov/apps/PrfWebApi"
PRF_CACHE_DIR = config.CACHE_DIR / "prf"

# Interval code -> name, verified against ADM A00480 Interval (RY2026):
# "625|JAN - FEB INDEX INTERVAL|JAN-FEB" ... "635|NOV - DEC INDEX INTERVAL|NOV-DEC".
INTERVALS = {
    "625": "JAN-FEB", "626": "FEB-MAR", "627": "MAR-APR", "628": "APR-MAY",
    "629": "MAY-JUN", "630": "JUN-JUL", "631": "JUL-AUG", "632": "AUG-SEP",
    "633": "SEP-OCT", "634": "OCT-NOV", "635": "NOV-DEC",
}
INTERVAL_ORDER = tuple(INTERVALS.values())

# intended use -> (Intended Use Code, Irrigation Practice Code, Organic Practice Code),
# ADM code tables A00470/A00490/A00500 (same mapping prf_adm uses). Grazing has no
# irrigated/non-irrigated split (997 = No Practice Specified).
USE_PARAMS = {
    "Grazing": ("007", "997", "997"),
    "Haying": ("030", "003", "997"),            # non-irrigated, conventional
    "Haying-Irrigated": ("030", "002", "997"),
}

COVERAGE_LEVELS = (0.70, 0.75, 0.80, 0.85, 0.90)

# Plan-13 subsidy percent by coverage level: ADM 2026_A00070_SubsidyPercent_YTD.txt
# record category 04 rows "A00070|04|2026|||13|<cov>|A||...|<subsidy>". 0.65 is CAT
# (coverage type C, 100% subsidized). Matches PrfWebApi GetPricingRates SubsidyLevel
# (0.510 at 90% for grid 27663) and RMA's published PRF subsidy schedule.
SUBSIDY_SCHEDULE = {
    0.65: 1.000,   # CAT
    0.70: 0.590,
    0.75: 0.590,
    0.80: 0.550,
    0.85: 0.550,
    0.90: 0.510,
}
SUBSIDY_SOURCE = ("RMA ADM RY2026 A00070 SubsidyPercent rc04 plan 13 "
                  "(pubfs-rma.fpac.usda.gov actuarial_data_master); verified vs "
                  "PrfWebApi GetPricingRates SubsidyLevel")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pure parsing (unit-tested, no network)
# ---------------------------------------------------------------------------

def parse_index_payload(payload: dict) -> list[tuple[int, str, float]]:
    """GetIndexValues JSON -> [(year, 'JAN-FEB', 0.798), ...].

    Rows whose PercentOfNormal is null (intervals not yet released for the current
    year) or whose interval code is unknown are dropped.
    """
    out: list[tuple[int, str, float]] = []
    for year_row in payload.get("HistoricalIndexRows", []):
        for col in year_row.get("HistoricalIndexDataColumns", []):
            name = INTERVALS.get(str(col.get("IntervalCode")))
            value = col.get("PercentOfNormal")
            if name is None or value is None:
                continue
            out.append((int(col["Year"]), name, float(value)))
    return out


def parse_rate_payload(payload: dict) -> dict[str, float]:
    """GetValidIntervalCodes JSON -> {'JAN-FEB': 0.1092, ...} (valid intervals only)."""
    out: dict[str, float] = {}
    for row in payload.get("validatedIntervals", []):
        name = INTERVALS.get(str(row.get("IntervalCode")))
        rate = row.get("PremiumRate")
        if name is None or rate is None or not row.get("IntervalIsValid"):
            continue
        out[name] = float(rate)
    return out


def parse_location_payload(payload: dict) -> list[tuple[str, str, str, str]]:
    """GetCountiesAndStatesFromGridId JSON -> [(state_code, county_code, state, county)]."""
    return [(str(r["StateCode"]), str(r["CountyCode"]),
             str(r.get("StateName", "")), str(r.get("CountyName", "")))
            for r in payload.get("locationData", [])]


# ---------------------------------------------------------------------------
# Cached API access
# ---------------------------------------------------------------------------

def _cached_get_json(client: http.Client | None, cache_name: str, url: str,
                     params: dict, force: bool = False) -> dict | list:
    """JSON GET with a file cache under data/cache/prf/ (skip network when cached)."""
    PRF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = PRF_CACHE_DIR / cache_name
    if path.exists() and not force:
        return json.loads(path.read_text())
    if client is None:
        raise RuntimeError(f"not cached ({path.name}) and no HTTP client provided")
    payload = client.get_json(url, params=params)
    # API errors come back as a list of {"Message": ...} — don't cache those.
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) \
            and "Message" in payload[0]:
        msgs = "; ".join(str(m.get("Message")) for m in payload)
        raise RuntimeError(f"PrfWebApi error for {path.name}: {msgs}")
    path.write_text(json.dumps(payload))
    return payload


def fetch_grid_location(grid_id: int, client: http.Client | None,
                        force: bool = False) -> list[tuple[str, str, str, str]]:
    payload = _cached_get_json(
        client, f"grid{grid_id}_location.json",
        f"{API_BASE}/PrfExternalStates/GetCountiesAndStatesFromGridId",
        {"gridId": grid_id, "gridName": grid_id}, force)
    locs = parse_location_payload(payload)
    if not locs:
        raise RuntimeError(f"grid {grid_id}: no counties returned (not a PRF grid?)")
    return locs


def fetch_indices(grid_id: int, client: http.Client | None, year_max: int,
                  force: bool = False) -> list[tuple[int, str, float]]:
    payload = _cached_get_json(
        client, f"grid{grid_id}_indices.json",
        f"{API_BASE}/PrfExternalIndexes/GetIndexValues",
        {"intervalType": "BiMonthly", "sampleYearMinimum": 1948,
         "sampleYearMaximum": year_max, "gridId": grid_id}, force)
    return parse_index_payload(payload)


def fetch_rates(grid_id: int, use: str, coverage: float, state_code: str,
                county_code: str, client: http.Client | None,
                force: bool = False) -> dict[str, float]:
    iu, irr, org = USE_PARAMS[use]
    cov_pct = int(round(coverage * 100))
    payload = _cached_get_json(
        client, f"grid{grid_id}_rates_{use}_{cov_pct}.json",
        f"{API_BASE}/PrfExternalIntervalCodes/GetValidIntervalCodes",
        {"intervalType": "BiMonthly", "irrigationPracticeCode": irr,
         "organicPracticeCode": org, "intendedUseCode": iu,
         "stateCode": state_code, "countyCode": county_code,
         "coverageLevelPercent": cov_pct, "gridId": grid_id,
         "gridName": grid_id}, force)
    return parse_rate_payload(payload)


# ---------------------------------------------------------------------------
# DB population + fetch API
# ---------------------------------------------------------------------------

def ensure_subsidy(conn) -> None:
    conn.executemany(
        """INSERT INTO prf_subsidy (coverage_level, subsidy_pct, source) VALUES (?,?,?)
           ON CONFLICT(coverage_level) DO UPDATE SET
             subsidy_pct=excluded.subsidy_pct, source=excluded.source""",
        [(lvl, pct, SUBSIDY_SOURCE) for lvl, pct in sorted(SUBSIDY_SCHEDULE.items())])
    conn.commit()


def _have_indices(conn, grid_id: int, min_years: int = 25) -> bool:
    n = conn.execute(
        """SELECT COUNT(*) FROM (SELECT year FROM prf_grid_index WHERE grid_id = ?
           GROUP BY year HAVING COUNT(*) = 11)""", (grid_id,)).fetchone()[0]
    return n >= min_years


def _have_rates(conn, grid_id: int, year: int, use: str,
                coverage_levels=COVERAGE_LEVELS) -> bool:
    n = conn.execute(
        """SELECT COUNT(DISTINCT coverage_level) FROM prf_grid_rate
           WHERE grid_id = ? AND year = ? AND intended_use = ?""",
        (grid_id, year, use)).fetchone()[0]
    return n >= len(coverage_levels)


def ensure_grid(grid_id: int, conn, *, cfg: config.Config | None = None,
                client: http.Client | None = None, use: str = "Grazing",
                coverage_levels=COVERAGE_LEVELS, force: bool = False) -> dict:
    """Populate prf_grid_index / prf_grid_rate / prf_subsidy for one grid (idempotent).

    Skips everything already in the DB (unless force), then everything already in the
    data/cache/prf/ JSON cache, and only then hits the PrfWebApi. `client` may be None
    when the DB or file cache already has the data. Returns a summary dict.
    """
    if use not in USE_PARAMS:
        raise ValueError(f"unknown use {use!r}; expected one of {sorted(USE_PARAMS)}")
    cfg = cfg or config.load()
    year = cfg.reinsurance_year
    fetched_at = _now_iso()
    summary = {"grid_id": grid_id, "use": use, "year": year}

    if client is None and (force or not (_have_indices(conn, grid_id)
                                         and _have_rates(conn, grid_id, year, use,
                                                         coverage_levels))):
        client = http.Client(cfg, conn)

    ensure_subsidy(conn)

    # Historical indices: 1948 -> current reinsurance year (current-year intervals not
    # yet released come back null and are skipped).
    if force or not _have_indices(conn, grid_id):
        rows = fetch_indices(grid_id, client, year, force)
        conn.executemany(
            """INSERT INTO prf_grid_index
                 (grid_id, year, interval_code, index_value, source, fetched_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(grid_id, year, interval_code) DO UPDATE SET
                 index_value=excluded.index_value, source=excluded.source,
                 fetched_at=excluded.fetched_at""",
            [(grid_id, y, iv, v, "prfwebapi", fetched_at) for y, iv, v in rows])
        conn.commit()
        summary["index_rows_fetched"] = len(rows)

    # Premium rates: one small GetValidIntervalCodes call per coverage level.
    if force or not _have_rates(conn, grid_id, year, use, coverage_levels):
        state_code, county_code = fetch_grid_location(grid_id, client, force)[0][:2]
        source = f"prfwebapi_{year}"
        n = 0
        for cov in coverage_levels:
            rates = fetch_rates(grid_id, use, cov, state_code, county_code,
                                client, force)
            conn.executemany(
                """INSERT INTO prf_grid_rate
                     (grid_id, year, intended_use, interval_code, coverage_level,
                      premium_rate, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(grid_id, year, intended_use, interval_code,
                               coverage_level) DO UPDATE SET
                     premium_rate=excluded.premium_rate, source=excluded.source,
                     fetched_at=excluded.fetched_at""",
                [(grid_id, year, use, iv, cov, r, source, fetched_at)
                 for iv, r in rates.items()])
            n += len(rates)
        conn.commit()
        summary["rate_rows_fetched"] = n

    summary["index_rows"] = conn.execute(
        "SELECT COUNT(*) FROM prf_grid_index WHERE grid_id = ?", (grid_id,)).fetchone()[0]
    summary["rate_rows"] = conn.execute(
        "SELECT COUNT(*) FROM prf_grid_rate WHERE grid_id = ? AND year = ? "
        "AND intended_use = ?", (grid_id, year, use)).fetchone()[0]
    lo, hi = conn.execute(
        "SELECT MIN(year), MAX(year) FROM prf_grid_index WHERE grid_id = ?",
        (grid_id,)).fetchone()
    summary["index_years"] = (lo, hi)
    return summary


def indices_matrix(grid_id: int, conn) -> dict[int, dict[str, float]]:
    """{year: {'JAN-FEB': 0.798, ...}} for every stored year of one grid."""
    out: dict[int, dict[str, float]] = {}
    for r in conn.execute(
            """SELECT year, interval_code, index_value FROM prf_grid_index
               WHERE grid_id = ? ORDER BY year""", (grid_id,)):
        out.setdefault(r["year"], {})[r["interval_code"]] = r["index_value"]
    return out


def rates_for(grid_id: int, use: str, coverage: float, conn,
              year: int | None = None) -> dict[str, float]:
    """{'JAN-FEB': 0.1092, ...} premium rates for one grid x use x coverage level.

    year defaults to the latest year stored for that grid/use.
    """
    if year is None:
        row = conn.execute(
            "SELECT MAX(year) FROM prf_grid_rate WHERE grid_id = ? AND intended_use = ?",
            (grid_id, use)).fetchone()
        year = row[0]
    return {r["interval_code"]: r["premium_rate"] for r in conn.execute(
        """SELECT interval_code, premium_rate FROM prf_grid_rate
           WHERE grid_id = ? AND year = ? AND intended_use = ?
             AND ABS(coverage_level - ?) < 1e-9""",
        (grid_id, year, use, coverage))}


def subsidy_schedule(conn) -> dict[float, float]:
    return {r["coverage_level"]: r["subsidy_pct"] for r in conn.execute(
        "SELECT coverage_level, subsidy_pct FROM prf_subsidy ORDER BY coverage_level")}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Fetch PRF grid indices + premium rates into the catalog DB.")
    ap.add_argument("--grid", type=int, required=True, help="PRF grid id, e.g. 27663")
    ap.add_argument("--use", default="Grazing", choices=sorted(USE_PARAMS),
                    help="intended use (default Grazing)")
    ap.add_argument("--force", action="store_true",
                    help="refetch even when DB/file cache already has the grid")
    args = ap.parse_args()

    conn = db.connect()
    db.init_db(conn)
    s = ensure_grid(args.grid, conn, use=args.use, force=args.force)
    lo, hi = s["index_years"]
    print(f"grid {s['grid_id']} ({s['use']}, RY{s['year']}): "
          f"{s['index_rows']} index rows ({lo}-{hi}), {s['rate_rows']} rate rows")
    m = indices_matrix(args.grid, conn)
    full_years = [y for y, ivs in m.items() if len(ivs) == 11]
    print(f"  complete years (11 intervals): {len(full_years)} "
          f"({min(full_years)}-{max(full_years)})")
    for cov in COVERAGE_LEVELS:
        r = rates_for(args.grid, args.use, cov, conn)
        if r:
            shown = ", ".join(f"{iv} {r[iv]:.4f}" for iv in INTERVAL_ORDER[:3] if iv in r)
            print(f"  rates @{int(cov*100)}%: {len(r)} intervals ({shown}, ...)")
    subs = subsidy_schedule(conn)
    print("  subsidy: " + ", ".join(f"{int(k*100)}%->{v:.3f}" for k, v in subs.items()))
    conn.close()


if __name__ == "__main__":
    _main()
