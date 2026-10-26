"""Self-contained PRF product page — one map, five metrics.

PRF = Pasture, Rangeland, Forage (rainfall index, plan code 13). This module is
the single PRF view in the app: the tab bar is PRODUCT-oriented, so the former
"PRF" (County Base Value) and "PRF Optimizer" tabs are folded into one page with
a metric dropdown.

THE FIVE METRICS
    cbv   County multiplier — County Base Value ($/acre)   [prf_county]
    win   Best win rate (%)                                [prf_opt_best]
    net   Best return per $1 of protection ($)             [prf_opt_best]
    acre  Best return per ACRE ($) — COMPUTED, see below   [both tables]
    comm  Commission per acre ($) — COMPUTED, see below    [both + seed CSV]

The first four answer "what does the PRODUCER get". `comm` answers a different,
standalone question — "what does the AGENCY earn" — and deliberately does NOT
re-rank or replace them.

THE PER-ACRE FORMULA (producer side)

    $/acre = best_net  x  CBV  x  coverage_level  x  productivity_factor

`best_net` is stored normalized per $1 of annual protection, and PRF protection
per acre is CBV x coverage level x productivity factor, so the product is the
average net return on one acre. Productivity factor is the grower's own election
(RMA allows 60%-150%); it is a page input, defaulting to 100%, and is applied
client-side at display time. Nothing is invented: a county needs BOTH a CBV and
a swept grid for the current selection, or it stays neutral.

THE COMMISSION FORMULA (agency side)

    protection/acre = CBV x coverage_level x productivity_factor
    premium/acre    = protection/acre x SUM(allocation_i x rate_i)
    commission/acre = premium/acre x commission_pct / 100

Agent commission is a percent of TOTAL premium — the subsidised portion included.
Premium is the County Base Value scaled by a PREMIUM RATE, and it is the rate the
CBV view leaves out: across RY2026 Grazing @ 90% the allocation-weighted rate runs
0.068 to 0.493, a 7.3x spread, so the same CBV buys very different premium
depending on the grid. That is the point of the metric — the two rankings are
correlated (~0.80 on real data) but not the same list: only 10 of the top 25
counties by CBV are also in the top 25 by commission per acre, and the single
best commission county carries less than half the top CBV. A big-CBV county is
not automatically a big-commission county.

    SUM(allocation_i x rate_i) is precomputed and STORED, not derived here.
    prf_grid_rate (2.1M rows) is dropped from the shipped app DB, so the
    allocation-weighted rate for each stored winner lives on the prf_opt_best row
    as best_net_rate_sum / best_win_rate_sum (written natively by
    src/prfsweep.compute_grid_best, and by scripts/backfill_rate_sums.py for rows
    predating those columns). The page does only the cheap multiplication.

The allocation used is the BEST-NET one — the optimizer's recommended policy —
so the number means "what I earn if the producer buys what the map recommends",
NOT the maximum premium an allocation could be made to generate.

commission_pct itself is HAND-MAINTAINED in data/seed/aip_commission.csv: it is
negotiated between AIP and agency and published nowhere, so it cannot be derived.
Every rate ships blank; the metric degrades to an honest "no rates set" note
rather than assuming a number.

THE USE AXIS (why one control drives two datasets)

prf_county is keyed by (intended_use, irrigation_practice); prf_opt_best is keyed
by a single intended_use whose values already encode irrigation. They line up
exactly, and this module derives the shared key rather than hardcoding it:

    prf_county(Grazing, Non-Irrigated)  ->  "Grazing"
    prf_county(Haying,  Non-Irrigated)  ->  "Haying"
    prf_county(Haying,  Irrigated)      ->  "Haying-Irrigated"

i.e. key = intended_use, suffixed "-Irrigated" when the practice is irrigated —
which is precisely the optimizer's own naming. A practice combination the map
has never seen (say Grazing/Irrigated) would therefore surface as its own key
instead of being silently dropped.

AGGREGATION RULE (optimizer metrics): a county can touch several grids. For each
county x use x coverage the county takes the BEST value among its grids
INDEPENDENTLY per metric (max best_win_rate; max best_net — the winning grid may
differ). The tooltip keeps the per-grid detail.

Rendering mirrors src/webmap.py's zero-network approach: d3 v7, topojson-client
and the us-atlas counties-10m topology are embedded inline; every control lives
INSIDE the embedded HTML and re-shades client-side. Degrades gracefully — an
empty prf_county, an empty prf_opt_best, or a missing prf_grid_county mapping
each produce a valid map with an honest note rather than an error.
"""
from __future__ import annotations

import ast
import csv
import datetime as _dt
import json
import re
import sqlite3
from pathlib import Path

# Coverage levels RMA offers on PRF, used only to order/label what the sweep stored.
DEFAULT_COVERAGE = "0.9"

# RMA's productivity-factor election range, in percent.
PROD_MIN_PCT = 60
PROD_MAX_PCT = 150
PROD_DEFAULT_PCT = 100

# Hand-maintained agency commission rates (see the module docstring). Repo-relative so the
# page reports the path the user has to edit, not an absolute one from this machine.
COMMISSION_CSV_REL = "data/seed/aip_commission.csv"
COMMISSION_CSV = Path(__file__).resolve().parents[1] / COMMISSION_CSV_REL

TIMEZONE_CSV_REL = "data/seed/commission_by_timezone.csv"
TIMEZONE_CSV = Path(__file__).resolve().parents[1] / TIMEZONE_CSV_REL

# State FIPS -> predominant US time zone, for the REGIONAL commission rate.
#
# A per-AIP rate has no geographic dimension, so a commission map built from it alone is an
# exact rescale of the premium map — same shape, different axis labels. A regional rate is
# what lets commission rank counties differently from premium.
#
# Thirteen states straddle a zone boundary (TX/KS/NE/ND/SD Central-Mountain, FL Eastern-Central,
# ID/OR Pacific-Mountain, MI/IN/KY/TN Eastern-Central) and every county in them is assigned the
# state's MAJORITY zone. That is a simplification, correct enough for a rate band and
# documented in data/seed/commission_by_timezone.csv, but it is not a time-zone boundary map.
# AK and HI are outside PRF's CONUS grid; they are listed only so a lookup never returns None
# for a real FIPS.
STATE_TIMEZONE = {
    "02": "Pacific", "06": "Pacific", "15": "Pacific", "32": "Pacific",
    "41": "Pacific", "53": "Pacific",
    "04": "Mountain", "08": "Mountain", "16": "Mountain", "30": "Mountain",
    "35": "Mountain", "49": "Mountain", "56": "Mountain",
    "01": "Central", "05": "Central", "17": "Central", "19": "Central",
    "20": "Central", "22": "Central", "27": "Central", "28": "Central",
    "29": "Central", "31": "Central", "38": "Central", "40": "Central",
    "46": "Central", "48": "Central", "55": "Central",
    "09": "Eastern", "10": "Eastern", "11": "Eastern", "12": "Eastern",
    "13": "Eastern", "18": "Eastern", "21": "Eastern", "23": "Eastern",
    "24": "Eastern", "25": "Eastern", "26": "Eastern", "33": "Eastern",
    "34": "Eastern", "36": "Eastern", "37": "Eastern", "39": "Eastern",
    "42": "Eastern", "44": "Eastern", "45": "Eastern", "47": "Eastern",
    "50": "Eastern", "51": "Eastern", "54": "Eastern",
}


def seed_mtime() -> float:
    """Newest mtime across every hand-edited commission seed.

    The page's cache key must move when EITHER seed is edited. Callers previously stat'd
    COMMISSION_CSV alone, which would have silently ignored edits to the time-zone rates.
    """
    newest = 0.0
    for p in (COMMISSION_CSV, TIMEZONE_CSV):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return newest


def load_commission_zones(path=None) -> dict:
    """Regional commission rates by time zone, from the hand-maintained seed CSV.

    Same defensive reading as load_aip_commission: '#' comments stripped, a blank or
    unparseable rate becomes None rather than 0, an out-of-range value is dropped as a typo,
    and a missing file yields an empty table (which simply means "fall back to the per-AIP
    rate"). Shape: {"zones": {zone: pct}, "path": rel, "with_rate": n}.
    """
    p = Path(path) if path is not None else TIMEZONE_CSV
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    except OSError:
        lines = []

    zones: dict[str, float] = {}
    for r in csv.DictReader(lines):
        zone = (r.get("zone") or "").strip()
        if not zone:
            continue
        raw = (r.get("commission_pct") or "").strip()
        if not raw:
            continue
        try:
            v = float(raw.rstrip("%"))
        except ValueError:
            continue
        if 0.0 <= v <= 100.0:
            zones[zone] = v

    return {
        "zones": zones,
        "state_zone": STATE_TIMEZONE,
        "path": TIMEZONE_CSV_REL if path is None else str(p),
        "with_rate": len(zones),
    }


def _fips5(value) -> str:
    """Normalize a county FIPS to the atlas's 5-digit string form."""
    s = str(value or "").strip()
    if not s:
        return s
    return s.zfill(5)


def _norm(text) -> str:
    """Lowercase, alphanumeric-only form of a label ('Non-Irrigated' -> 'nonirrigated')."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def use_key(intended_use, irrigation_practice) -> str:
    """Shared use key joining prf_county's two dimensions to prf_opt_best's one.

    'Haying' + 'Irrigated' -> 'Haying-Irrigated'; any non-irrigated practice
    leaves the intended use unchanged. This reproduces the optimizer's own
    intended_use vocabulary, so both tables index off one control.
    """
    u = str(intended_use or "").strip()
    if _norm(irrigation_practice) == "irrigated":
        return f"{u}-Irrigated"
    return u


def _cov_key(cov) -> str:
    """Canonical string key for a coverage level (0.90 / '0.9' / 0.9 -> '0.9').

    JSON object keys must be strings; '%g' collapses trailing zeros so the
    Python-built key always equals JavaScript's String(parseFloat(key)).
    """
    try:
        return "%g" % float(cov)
    except (TypeError, ValueError):
        return str(cov)


def _parse_list(text) -> list:
    """Parse a combo/props column into a plain list.

    The sweep stores JSON (e.g. '["JUN-JUL","AUG-SEP"]' / '[50,50]'), but the
    optimizer's export path historically used Python reprs with single quotes,
    so fall back to ast.literal_eval. Anything unparseable -> [].
    """
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        return list(text)
    s = str(text).strip()
    if not s:
        return []
    try:
        val = json.loads(s)
    except ValueError:
        try:
            val = ast.literal_eval(s)
        except (ValueError, SyntaxError):
            return []
    return list(val) if isinstance(val, (list, tuple)) else []


def _f(value, nd: int | None = None):
    """float() or None (bad/missing values become None, never raise).

    `nd` rounds — the sweep stores full float repr (0.33313671111111115), which
    costs ~12 bytes per value across ~200k rows in the embedded payload. The
    page displays win rates to 0.1% and net returns to $0.001 per $1, so 4-5
    decimals is already well past what any label can show; nothing visible
    changes and the payload shrinks by a third.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if nd is None else round(v, nd)


def _col(row, name):
    """row[name] or None — tolerates a DB predating a column (sqlite3.Row raises).

    build_opt_payload does SELECT *, so a catalog.db that has not had
    db.apply_migrations() run against it simply has no rate-sum column; that must
    render an honest un-shaded commission metric, not a traceback.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


# ------------------------------------------------------- commission rates (CSV)
def load_aip_commission(path=None, product: str = "PRF") -> dict:
    """Agency commission rate per AIP for ONE PRODUCT, from the hand-maintained seed CSV.

    THE PRODUCT ARGUMENT IS LOAD-BEARING. Commission is negotiated per product line, and
    livestock (LRP, LGM, DRP) commonly sits on a different schedule from MPCI-family business
    (PRF, ROWCROP). A rate entered against one product is never returned for another: ask for
    a product with no rows and you get an EMPTY roster, so the page renders its "no rates set"
    state and says which product is missing. Falling back to another product's rate would
    report a commission the agency does not earn, on a map someone plans a week around.

    A file with no `product` column at all is treated as the legacy single-product PRF card,
    so an un-migrated rate card keeps working for the product it was written for and yields
    nothing for the rest.

    Shape:
        {"aips": [{"code","name","pct" (float|None),"notes"}, ...],
         "path": "data/seed/aip_commission.csv",
         "with_rate": <how many carry a number>, "row_count": <rows read>}

    The file is a real spreadsheet a human edits, so it is read defensively:
    '#' comment lines are stripped before the header is parsed, a blank or
    unparseable commission_pct becomes None (NOT 0 — "unknown" and "zero
    commission" are different claims), and a missing file yields an empty roster.
    In every one of those cases the page shows its "no rates set" state.

    A pct outside 0-100 is dropped as a typo: agent commission is a percent of
    premium, and neither a negative nor a >100% share is a rate this tool should
    silently multiply a producer's premium by.
    """
    p = Path(path) if path is not None else COMMISSION_CSV
    aips: list[dict] = []
    rows = 0
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    except OSError:
        lines = []

    def _pct(raw):
        """A rate, or None. None is NOT 0 — see the module docstring."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            v = float(raw.rstrip("%"))
        except ValueError:
            return None
        return v if 0.0 <= v <= 100.0 else None

    reader = csv.DictReader(lines)
    # Region columns are whatever the file declares, minus the fixed ones — so adding a
    # region is adding a column, with no code change. A legacy single-rate file (one
    # `commission_pct` column, no region columns) still loads: its rate becomes the AIP's
    # flat fallback for every region.
    fixed = {"aip_code", "aip_name", "commission_pct", "notes", "product"}
    regions = [c for c in (reader.fieldnames or []) if c and c not in fixed]
    has_product = "product" in (reader.fieldnames or [])
    want = (product or "").strip().upper()

    for r in reader:
        code = (r.get("aip_code") or "").strip()
        name = (r.get("aip_name") or "").strip()
        if not code and not name:
            continue
        # A file WITH a product column makes a claim about products, and that claim is
        # honoured strictly — a PRF rate is never returned for LRP. A file WITHOUT one makes
        # no such claim, so it loads for whoever asked: that is a single-product rate card the
        # caller chose deliberately (build_*_page_payload(commission_csv=...)), and refusing
        # it would break an explicit override to protect against a mix-up the file cannot
        # cause. The leak the product column prevents is in the SHARED default card, which
        # now always has one.
        if has_product and (r.get("product") or "").strip().upper() != want:
            continue
        rows += 1
        by_region = {}
        for reg in regions:
            v = _pct(r.get(reg))
            if v is not None:
                by_region[reg] = v
        flat = _pct(r.get("commission_pct"))
        aips.append({"code": code or name, "name": name or code,
                     "pct": flat, "by_region": by_region,
                     "notes": (r.get("notes") or "").strip()})

    aips.sort(key=lambda a: a["name"].lower())
    return {
        "aips": aips,
        "product": want,
        "has_product_column": has_product,
        "regions": regions,
        "path": COMMISSION_CSV_REL if path is None else str(p),
        # An AIP counts as rated if it carries ANY rate — a regional one or a flat one.
        "with_rate": sum(1 for a in aips if a["pct"] is not None or a["by_region"]),
        "row_count": rows,
    }


# --------------------------------------------------------------- CBV payload
def build_cbv_payload(conn: sqlite3.Connection) -> dict:
    """County Base Values, re-keyed onto the shared use axis. Pure read; testable.

    Shape:
        counties[fips][use_key][organic][year] = cbv  ($/acre float)

    Plus the axes the in-map dropdowns are built from (uses / organics / years,
    and organics_by_use so a use is never offered an organic practice its data
    does not carry), county display names, use_map (use_key -> the prf_county
    intended_use / irrigation_practice it came from), and the min/max CBV over
    every stored value (the color scale's domain). When prf_county is empty (or
    the table is missing) every collection comes back empty and min/max are
    None — the page still renders, all-neutral, with an honest note.
    """
    counties: dict[str, dict] = {}
    county_names: dict[str, str] = {}
    use_map: dict[str, dict] = {}
    organics_by_use: dict[str, set] = {}
    organics: set[str] = set()
    years: set[int] = set()
    values: list[float] = []

    try:
        rows = conn.execute(
            "SELECT year, state, county_fips, county_name, intended_use, "
            "irrigation_practice, organic_practice, county_base_value "
            "FROM prf_county"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []  # table not created yet — degrade to empty

    for r in rows:
        fips = _fips5(r["county_fips"])
        if not fips:
            continue
        ukey = use_key(r["intended_use"], r["irrigation_practice"])
        organic = (r["organic_practice"] or "Conventional").strip() or "Conventional"
        try:
            year = int(r["year"])
        except (TypeError, ValueError):
            continue
        cbv = r["county_base_value"]

        # Axes reflect every row present so the dropdowns show the real domain
        # even where some cells lack a CBV.
        use_map.setdefault(ukey, {
            "intended_use": (r["intended_use"] or "").strip(),
            "irrigation_practice": (r["irrigation_practice"] or "").strip(),
        })
        organics_by_use.setdefault(ukey, set()).add(organic)
        organics.add(organic)
        years.add(year)
        if r["county_name"] and fips not in county_names:
            county_names[fips] = str(r["county_name"]).strip()

        if cbv is None:
            continue  # keep the axis, but no shaded value for this cell
        try:
            cbv = float(cbv)
        except (TypeError, ValueError):
            continue
        values.append(cbv)
        (counties
            .setdefault(fips, {})
            .setdefault(ukey, {})
            .setdefault(organic, {})[year]) = cbv

    return {
        "counties": counties,
        "county_names": county_names,
        "uses": sorted(use_map),
        "use_map": use_map,
        "organics": sorted(organics),
        "organics_by_use": {k: sorted(v) for k, v in organics_by_use.items()},
        "years": sorted(years),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "row_count": len(rows),
        "value_count": len(values),
    }


# --------------------------------------------------------- optimizer payload
def build_opt_payload(conn: sqlite3.Connection) -> dict:
    """Best-of-sweep results aggregated to counties. Pure read; testable.

    Shape:
        counties[fips][use_key][cov_key] = {
            "win":  max best_win_rate over the county's grids (or None),
            "net":  max best_net over the county's grids (or None),
            "nrs":  the best-net grid's allocation-weighted PREMIUM RATE,
            "ni":   that grid's grid_detail index (which grid the premium is from),
            "g":    [grid_detail indices, best win rate first],
        }
        grid_detail[i] = {"grid", "win", "win_net", "wp",
                          "net", "net_win", "np", "nrs"}  # wp/np -> policies index
        policies[j]    = [interval codes, % allocations]

    "nrs" is prf_opt_best.best_net_rate_sum = SUM(allocation_i x premium_rate_i) over the
    BEST-NET allocation. It is what turns a stored policy into dollars of premium (and hence
    agent commission) per acre without shipping prf_grid_rate; see the module docstring. It
    is taken from the SAME grid that supplied the county's "net" — the commission has to be
    the commission on the policy the map is recommending, not a mix of grids. Its sibling
    column best_win_rate_sum stays in the DB but is deliberately NOT embedded: no metric
    reads it, and another number on ~195k rows is ~1.5 MB of payload for nothing.

    plus axes (uses / coverages), county display names, per-metric min/max over
    every aggregated county value (the color-scale domains), and row counts so
    the client can distinguish "sweep not run" from "no grid->county mapping".

    WHY THE INDIRECTION: a grid touches ~2.3 counties, so inlining each grid's
    detail under every county it serves duplicated the whole sweep ~2.3x — the
    embedded payload came to 158 MB, which is not something a browser should be
    asked to parse inside an iframe. Grid details are therefore stored ONCE and
    referenced by index, and the winning allocations (interval codes + percent
    splits) are interned into `policies` because a few thousand distinct
    allocations are shared across ~200k rows. Same information, ~8x smaller.

    prf_opt_best.intended_use already uses the shared key vocabulary
    ('Haying-Irrigated'), so no remapping is needed here.
    """
    # --- grid -> counties mapping -----------------------------------------
    grid_counties: dict = {}
    county_names: dict[str, str] = {}
    mapping_rows = 0
    try:
        for r in conn.execute(
            "SELECT grid_id, state, county_fips, county_name FROM prf_grid_county"
        ):
            mapping_rows += 1
            fips = _fips5(r["county_fips"])
            if not fips:
                continue
            grid_counties.setdefault(r["grid_id"], []).append(fips)
            if r["county_name"] and fips not in county_names:
                county_names[fips] = str(r["county_name"]).strip()
    except sqlite3.OperationalError:
        pass  # table not created yet — degrade to empty

    # Inverse of grid_counties, for the map's grid layer. Deliberately NOT derived from the
    # sweep results: which grids lie in a county is geography, so the drill-down must be able
    # to draw the cells even for a metric with no per-grid value (County Base Value is
    # published per COUNTY, so every cell in a county shares one CBV) and even where the
    # sweep has not run. Cell corners come from grid_id arithmetic (see src/prfgrid.py), so
    # only the ids travel — no geometry is embedded in the page.
    # NB: do not name a loop variable _f here — that is the module-level number formatter,
    # and shadowing it inside this function breaks every _f() call further down.
    grids_by_county: dict[str, list[int]] = {}
    for grid_key, fips_list in grid_counties.items():
        for county_key in fips_list:
            grids_by_county.setdefault(county_key, []).append(int(grid_key))
    for county_key in grids_by_county:
        grids_by_county[county_key].sort()

    # --- best-of-sweep rows ------------------------------------------------
    try:
        rows = conn.execute("SELECT * FROM prf_opt_best").fetchall()
    except sqlite3.OperationalError:
        rows = []

    # The county's actuarial maximum percent of value. prf_opt_best is keyed by max_pct
    # because a grid straddling a cap boundary has two legal policy universes and therefore
    # two different best policies; this is what decides WHICH of those rows belongs to a given
    # county. Without it the last row processed would win, arbitrarily, and a Texas county
    # under a 40% cap could be shown an allocation legal only in a 50% county next door.
    county_cap: dict[str, int] = {}
    try:
        for fips, cap in conn.execute(
                "SELECT state_code || county_code, MIN(max_pct) FROM prf_max_pct "
                "GROUP BY state_code, county_code"):
            county_cap[str(fips)] = int(cap)
    except sqlite3.OperationalError:
        pass                       # pre-cap DB: every row carries the same max_pct

    counties: dict = {}
    uses: set[str] = set()
    coverages: set[str] = set()
    unmatched: set = set()
    grid_detail: list[dict] = []
    policies: list[list] = []
    policy_ix: dict = {}

    def _policy(combo_text, props_text) -> int:
        """Intern an (intervals, % allocations) pair; -1 when there isn't one."""
        combo = _parse_list(combo_text)
        props = _parse_list(props_text)
        if not combo:
            return -1
        key = (tuple(combo), tuple(props))
        ix = policy_ix.get(key)
        if ix is None:
            ix = len(policies)
            policy_ix[key] = ix
            policies.append([combo, props])
        return ix

    for r in rows:
        use = (r["intended_use"] or "").strip()
        ck = _cov_key(r["coverage_level"])
        uses.add(use)
        coverages.add(ck)
        gid = r["grid_id"]
        fipses = grid_counties.get(gid)
        if not fipses:
            unmatched.add(gid)  # swept grid with no county mapping (yet)
            continue
        detail = {
            "grid": gid,
            "win": _f(r["best_win_rate"], 4),
            "win_net": _f(r["best_win_avg_net"], 5),
            "wp": _policy(r["best_win_combo"], r["best_win_props"]),
            "net": _f(r["best_net"], 5),
            "net_win": _f(r["best_net_win_rate"], 4),
            "np": _policy(r["best_net_combo"], r["best_net_props"]),
            # 6dp: rates carry 4 decimals and the weights are integer percents, so the
            # product is exact at 6 — and $0.000001 of rate is ~$0.00005/acre of premium.
            "nrs": _f(_col(r, "best_net_rate_sum"), 6),
        }
        di = len(grid_detail)
        grid_detail.append(detail)
        row_cap = _col(r, "max_pct")
        for fips in fipses:
            # Only the row swept under THIS county's cap describes a policy this county's
            # producers can actually buy. Grids with a single cap are unaffected; the 617 that
            # straddle a boundary contribute a different row to each side.
            want = county_cap.get(str(fips))
            if want is not None and row_cap is not None and int(row_cap) != want:
                continue
            cell = (counties
                    .setdefault(fips, {})
                    .setdefault(use, {})
                    .setdefault(ck, {"win": None, "net": None, "nrs": None,
                                     "ni": -1, "g": []}))
            cell["g"].append(di)
            # BEST-per-metric aggregation: max over grids, independently.
            if detail["win"] is not None and (cell["win"] is None
                                              or detail["win"] > cell["win"]):
                cell["win"] = detail["win"]
            if detail["net"] is not None and (cell["net"] is None
                                              or detail["net"] > cell["net"]):
                cell["net"] = detail["net"]
                # The premium/commission travels WITH the winning grid, so it is
                # reassigned here rather than maximized on its own — a higher rate is a
                # bigger bill for the producer, not a better county to be in.
                cell["nrs"] = detail["nrs"]
                cell["ni"] = di

    # Sort each cell's grid references best-win-rate first; collect metric
    # domains over the AGGREGATED county values (what the map actually shades).
    def _sortkey(i: int):
        d = grid_detail[i]
        return (-(d["win"] if d["win"] is not None else float("-inf")), str(d["grid"]))

    wins: list[float] = []
    nets: list[float] = []
    for by_use in counties.values():
        for by_cov in by_use.values():
            for cell in by_cov.values():
                cell["g"].sort(key=_sortkey)
                if cell["win"] is not None:
                    wins.append(cell["win"])
                if cell["net"] is not None:
                    nets.append(cell["net"])

    def _covsort(k: str):
        try:
            return float(k)
        except ValueError:
            return float("inf")

    return {
        "counties": counties,
        "grid_detail": grid_detail,
        "policies": policies,
        "grids_by_county": grids_by_county,
        "county_names": county_names,
        "uses": sorted(uses),
        "coverages": sorted(coverages, key=_covsort),
        "min_win": min(wins) if wins else None,
        "max_win": max(wins) if wins else None,
        "min_net": min(nets) if nets else None,
        "max_net": max(nets) if nets else None,
        "row_count": len(rows),
        "mapping_rows": mapping_rows,
        "county_count": len(counties),
        "unmatched_grids": len(unmatched),
    }


# ------------------------------------------------------------ merged payload
def build_prf_page_payload(conn: sqlite3.Connection, commission_csv=None,
                           timezone_csv=None) -> dict:
    """Both PRF datasets on one shared use axis, plus the commission roster. Pure read.

    The two builders above are joined by `uses` — the sorted union of the use
    keys each dataset carries — and by county FIPS. Nothing is cross-filled: a
    use present in only one dataset still appears (the other metric family just
    renders neutral for it), which is how a partially-swept sweep stays honest.

    `commission_csv` overrides the seed path (tests, or an agency keeping its
    negotiated rates outside the repo); None reads data/seed/aip_commission.csv.
    """
    cbv = build_cbv_payload(conn)
    opt = build_opt_payload(conn)

    county_names = dict(opt["county_names"])
    county_names.update(cbv["county_names"])  # ADM names win; both are county-grain

    uses = sorted(set(cbv["uses"]) | set(opt["uses"]))

    return {
        "generated": _dt.date.today().isoformat(),
        "uses": uses,
        "county_names": county_names,
        "cbv": cbv,
        "opt": opt,
        "comm": load_aip_commission(commission_csv, product="PRF"),
        "commzone": load_commission_zones(timezone_csv),
        "prod": {"min": PROD_MIN_PCT, "max": PROD_MAX_PCT, "default": PROD_DEFAULT_PCT},
        "default_coverage": DEFAULT_COVERAGE,
    }


def _js_embed_json(obj) -> str:
    """JSON serialized for safe inline embedding inside a <script> block."""
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def render_prf_page_html(payload: dict, d3_js: str, topojson_js: str,
                         atlas: dict) -> str:
    """Render the self-contained merged PRF county choropleth HTML string."""
    for blob in (d3_js, topojson_js):
        if "</script" in blob.lower():
            raise ValueError("asset JS contains '</script'; cannot inline safely")
    html = _TEMPLATE
    html = html.replace("__GENERATED__", payload.get("generated", ""))
    html = html.replace("__D3__", d3_js)
    html = html.replace("__TOPOJSON__", topojson_js)
    html = html.replace("__ATLAS__", _js_embed_json(atlas))
    html = html.replace("__PAYLOAD__", _js_embed_json(payload))
    return html


# The template uses __TOKENS__ (not str.format) so the JS braces stay literal.
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRF — Pasture, Rangeland, Forage</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb; --page: #f9f9f7;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --ring: rgba(11,11,11,0.10);
    --none: #ececea; --accent: #2a78d6;
  }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--ink); font-size: 14px;
    display: flex; flex-direction: column;
  }
  header { padding: 12px 18px 8px; }
  header h1 { font-size: 16px; font-weight: 650; }
  header .sub { color: var(--ink-2); font-size: 12.5px; margin-top: 2px; }
  .filters {
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    padding: 8px 18px 10px; border-bottom: 1px solid var(--grid);
    background: var(--surface);
  }
  .filters label { color: var(--ink-2); font-size: 12px; }
  .filters select, .filters input[type=number] {
    font: inherit; font-size: 13px; padding: 4px 6px; max-width: 260px;
    border: 1px solid var(--baseline); border-radius: 6px; background: var(--surface);
    color: var(--ink);
  }
  .filters input[type=number] { width: 74px; }
  .filters select:disabled { color: var(--muted); background: var(--page); }
  #mSel { font-weight: 600; max-width: 300px; }
  .seg { display: inline-flex; border: 1px solid var(--baseline); border-radius: 6px;
    overflow: hidden; }
  .seg button {
    font: inherit; font-size: 12.5px; padding: 4px 11px; border: none;
    background: var(--surface); color: var(--ink-2); cursor: pointer;
  }
  .seg button + button { border-left: 1px solid var(--baseline); }
  .seg button.on { background: #238b45; color: #fff; }
  /* THE LENS IS A MODE SWITCH, NOT A FILTER, and must not look like one. Every other .seg
     on these pages selects a value within the current view (coverage, quarter, band) and
     turns green when active. The lens changes WHOSE NUMBERS the whole page is showing, so it
     takes the ink colour instead — the same control shape, deliberately a different weight,
     so the eye does not file it with the filters beside it. Matched by the Streamlit-native
     segmented control on the LGM and LRP tabs, which have no iframe. */
  #lensSeg button.on { background: var(--ink); color: #fff; font-weight: 650; }
  #lensSeg button { padding: 4px 13px; }
  #formulaNote {
    padding: 6px 18px; font-size: 11.5px; color: var(--ink-2);
    background: var(--surface); border-bottom: 1px solid var(--grid); display: none;
  }
  #formulaNote code { font-size: 11.5px; background: var(--page);
    border: 1px solid var(--grid); border-radius: 4px; padding: 0 4px; }
  #formulaNote .fn { display: none; }
  #formulaNote .fn.on { display: block; }
  /* Metric range slider (dual-thumb) — filters which counties are shaded. */
  .rangebar {
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
    padding: 24px 18px 9px; border-bottom: 1px solid var(--grid); background: var(--surface);
  }
  .rangebar > label { color: var(--ink-2); font-size: 12px; white-space: nowrap; }
  .dual { position: relative; flex: 1; min-width: 220px; max-width: 520px; height: 26px; }
  .dual .track { position: absolute; top: 11px; left: 0; right: 0; height: 4px;
    background: var(--grid); border-radius: 3px; }
  .dual .fill { position: absolute; top: 11px; height: 4px; background: #41ab5d; border-radius: 3px; }
  .dual .bubble {
    position: absolute; top: -16px; transform: translateX(-50%);
    font-size: 11px; font-variant-numeric: tabular-nums; color: var(--ink);
    background: var(--surface); border: 1px solid var(--baseline); border-radius: 4px;
    padding: 0 4px; line-height: 15px; white-space: nowrap; pointer-events: none;
  }
  .dual input[type=range] {
    position: absolute; top: 0; left: 0; width: 100%; height: 26px; margin: 0;
    -webkit-appearance: none; appearance: none; background: transparent; pointer-events: none;
  }
  .dual input[type=range]::-webkit-slider-runnable-track { height: 26px; background: transparent; }
  .dual input[type=range]::-moz-range-track { height: 26px; background: transparent; }
  .dual input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none; pointer-events: auto;
    width: 16px; height: 16px; border-radius: 50%; background: var(--surface);
    border: 2px solid #238b45; box-shadow: 0 1px 3px var(--ring); cursor: pointer;
  }
  .dual input[type=range]::-moz-range-thumb {
    pointer-events: auto; width: 16px; height: 16px; border-radius: 50%;
    background: var(--surface); border: 2px solid #238b45; cursor: pointer;
  }
  .rangebar .readout { font-size: 12.5px; color: var(--ink);
    font-variant-numeric: tabular-nums; white-space: nowrap; }
  .rangebar .reset { font: inherit; font-size: 12px; color: var(--accent);
    background: none; border: none; cursor: pointer; padding: 0; }
  #main { flex: 1; display: flex; min-height: 0; }
  #mapWrap { flex: 1; position: relative; min-width: 0; }
  #map { width: 100%; height: 100%; display: block; }
  .county { stroke: var(--surface); stroke-width: 0.4; vector-effect: non-scaling-stroke; }
  .statelines { fill: none; stroke: var(--baseline); stroke-width: 0.7;
                pointer-events: none; vector-effect: non-scaling-stroke; }
  .county.hovered { stroke: var(--ink); stroke-width: 1.3; }
  #tooltip {
    position: absolute; pointer-events: none; display: none; z-index: 5;
    background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
    box-shadow: 0 2px 10px rgba(11,11,11,0.12); padding: 7px 10px; font-size: 12.5px;
    max-width: 340px;
  }
  #tooltip .t-name { font-weight: 650; }
  #tooltip .t-val { color: var(--ink-2); margin-top: 1px; }
  #tooltip .t-math { color: var(--ink-2); font-size: 11.5px; margin-top: 3px;
    font-variant-numeric: tabular-nums; }
  #tooltip .t-grid { margin-top: 5px; border-top: 1px solid var(--grid); padding-top: 4px; }
  #tooltip .t-gid { font-weight: 600; font-size: 12px; }
  #tooltip .t-line { color: var(--ink-2); font-size: 11.5px; margin-top: 1px; }
  #tooltip .t-more { color: var(--muted); font-size: 11px; margin-top: 4px; }
  #legend {
    position: absolute; left: 16px; bottom: 14px; background: var(--surface);
    border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 11.5px;
  }
  #legend .l-title { color: var(--ink-2); margin-bottom: 5px; }
  #legend .l-row { display: flex; align-items: center; gap: 0; }
  #legend .l-cell { width: 34px; height: 10px; }
  #legend .l-labels { display: flex; font-size: 10px; color: var(--muted); margin-top: 1px; }
  #legend .l-labels span { width: 34px; text-align: left; }
  #legend .l-none { display: flex; align-items: center; gap: 6px; margin-top: 7px; color: var(--muted); }
  #legend .l-none .sw { width: 14px; height: 10px; background: var(--none); border: 1px solid var(--grid); }
  #note {
    position: absolute; top: 12px; left: 16px; right: 16px; z-index: 4; display: none;
    background: #fdf6e7; border: 1px solid #eda100; border-radius: 8px;
    color: #6b4b00; padding: 8px 12px; font-size: 12.5px;
  }
  footer { padding: 6px 18px; color: var(--muted); font-size: 11px; border-top: 1px solid var(--grid); }

  /* ---- drill-down: breadcrumb + zoom control ---- */
  #crumb {
    position: absolute; top: 12px; left: 16px; z-index: 4; display: flex; align-items: center;
    gap: 6px; background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
    padding: 5px 9px; font-size: 12px; box-shadow: 0 1px 4px rgba(11,11,11,0.06);
  }
  #crumb .c-step { color: var(--accent); cursor: pointer; }
  #crumb .c-step:hover { text-decoration: underline; }
  #crumb .c-here { color: var(--ink); font-weight: 600; }
  #crumb .c-sep { color: var(--muted); }
  #crumb .c-hint { color: var(--muted); border-left: 1px solid var(--grid); padding-left: 8px; }
  #zoomBox {
    position: absolute; right: 16px; bottom: 14px; z-index: 4; display: flex; flex-direction: column;
    align-items: center; gap: 6px; background: var(--surface); border: 1px solid var(--ring);
    border-radius: 8px; padding: 9px 7px; box-shadow: 0 1px 4px rgba(11,11,11,0.06);
  }
  #zoomBox button {
    font: inherit; font-size: 15px; line-height: 1; width: 24px; height: 22px; cursor: pointer;
    border: 1px solid var(--baseline); border-radius: 5px; background: var(--surface); color: var(--ink);
  }
  #zoomBox button:hover { border-color: var(--accent); color: var(--accent); }
  /* Vertical slider: up = closer in, which is the direction the map moves. */
  #zSlider { writing-mode: vertical-lr; direction: rtl; width: 22px; height: 104px; accent-color: var(--accent); }
  #zLabel { font-size: 10.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
  /* Grid cells: only ever drawn for the focused county, so a handful of paths. */
  .gridcell { stroke: rgba(11,11,11,0.30); stroke-width: 0.5; vector-effect: non-scaling-stroke; cursor: default; }
  .gridcell.hovered { stroke: var(--ink); stroke-width: 1.6; }
  .county.dimmed { opacity: 0.28; }
  .county.focused { stroke: var(--ink); stroke-width: 1.1; }
  .focusline { fill: none; stroke: var(--ink); stroke-width: 2.2; stroke-linejoin: round;
               pointer-events: none; vector-effect: non-scaling-stroke; }
  /* Hover readout — a dedicated top-most outline, NOT a stroke on the hovered shape.
     A county's border is SHARED with its neighbours, and in SVG whichever sibling is drawn
     later paints over it. Styling the shape itself therefore gives an outline that is whole
     on some edges and erased on others — it reads as noise rather than as "you are here".
     Grid cells are worse: opaque, and drawn in a later group entirely. .focusline exists for
     exactly this reason. The pale casing under the dark line keeps it legible against both
     ends of the choropleth ramp, and both are non-scaling so the weight is the same at every
     zoom level instead of thinning out as you drill in. */
  .hovercase { fill: none; stroke: var(--surface); stroke-width: 3.4; stroke-linejoin: round;
               pointer-events: none; vector-effect: non-scaling-stroke; opacity: 0.85; }
  .hoverline { fill: none; stroke: var(--ink); stroke-width: 1.5; stroke-linejoin: round;
               pointer-events: none; vector-effect: non-scaling-stroke; }
</style>
</head>
<body>
<header>
  <h1 id="pageTitle">PRF — Pasture, Rangeland, Forage</h1>
  <div class="sub" id="pageSub">Generated __GENERATED__.</div>
</header>
<div class="filters">
  <!-- WHOSE MONEY. Every metric here answers one of two questions and they sat in one
       dropdown together: four describe the PRODUCER's outcome, one describes the AGENCY's.
       The lens picks the question and the Show list then offers only the metrics that answer
       it. Nothing was removed — all five are still reachable, just sorted. -->
  <label>Lens
    <span class="seg" id="lensSeg">
      <button data-lens="buy" class="on">Buy — producer</button>
      <button data-lens="sell">Sell — agency</button>
    </span>
  </label>
  <label>Show <select id="mSel"></select></label>
  <label>Intended use <select id="fUse"></select></label>
  <label id="covWrap">Coverage
    <span class="seg" id="covSeg"></span>
  </label>
  <label id="organicWrap">Organic <select id="fOrganic"></select></label>
  <label id="yearWrap" style="display:none">Year <select id="fYear"></select></label>
  <label id="prodWrap" style="display:none">Productivity factor
    <input type="number" id="fProd" min="60" max="150" step="1" value="100"> %
  </label>
  <label id="aipWrap" style="display:none">AIP <select id="fAip"></select></label>
  <span id="countLine" style="color:var(--muted);font-size:12px;margin-left:auto"></span>
</div>
<div id="formulaNote">
  <div class="fn" data-m="acre">
    <b>Return per acre</b> =
    <code>best net (per $1 of protection) &times; County Base Value ($/acre) &times; coverage level &times; productivity factor</code>.
    PRF protection per acre is CBV &times; coverage level &times; productivity factor, so this is the
    average net return on one acre. Productivity factor is the grower's election (RMA allows
    60–150%); a county needs BOTH a CBV and a swept grid for the current selection or it stays neutral.
  </div>
  <div class="fn" data-m="comm">
    <b>Commission per acre</b> =
    <code>County Base Value ($/acre) &times; coverage level &times; productivity factor &times; &Sigma;(allocation &times; premium rate) &times; commission %</code>.
    Agent commission is a percent of <b>total</b> premium (the subsidised portion included), and
    premium is the County Base Value scaled by the <b>premium rate</b> — which the CBV view leaves
    out entirely and which varies several-fold between grids at the same coverage. So the
    biggest-CBV counties are not the biggest-commission counties: a mid-CBV county on an expensive
    grid outearns a top-CBV county on a cheap one. The allocation priced is the optimizer's
    <b>recommended</b> (best-net) policy, so this is what you earn if the producer buys what this
    map recommends — not a maximum-premium figure.
    Commission % is your own negotiated rate, hand-entered in
    <code>data/seed/aip_commission.csv</code> as an <b>AIP &times; region</b> table &mdash; rates
    differ by region within one AIP and between AIPs, and the same rate recurs across AIPs, so a
    single number per AIP cannot express them (and would make this map an exact rescale of
    premium). Region is the county's time zone, assigned per state. It is published nowhere and
    cannot be derived. <b>The rates shipped in that file are SAMPLE data</b> &mdash; replace them
    with your own before quoting any figure here.
  </div>
  <div class="fn" data-m="win">
    Intended use does not move this metric. A PRF premium rate is a function of the grid's
    rainfall distribution, the interval and the coverage level — not of what the forage is used
    for — so for effectively every grid this number is identical across Grazing, Haying and
    Haying-Irrigated. Switching use changes <b>which grids are rated</b> (Haying is offered on
    fewer), so the shaded set moves even when the values do not. Use drives the County Base Value
    and therefore the two per-acre metrics.
  </div>
  <div class="fn" data-m="net">
    Intended use does not move this metric. A PRF premium rate is a function of the grid's
    rainfall distribution, the interval and the coverage level — not of what the forage is used
    for — so for effectively every grid this number is identical across Grazing, Haying and
    Haying-Irrigated. Switching use changes <b>which grids are rated</b> (Haying is offered on
    fewer), so the shaded set moves even when the values do not. Use drives the County Base Value
    and therefore the two per-acre metrics.
  </div>
</div>
<div class="rangebar" id="rangebar">
  <label id="rangeLabel">Metric range</label>
  <div class="dual">
    <div class="track"></div>
    <div class="fill" id="rFill"></div>
    <div class="bubble" id="rBubbleLo"></div>
    <div class="bubble" id="rBubbleHi"></div>
    <input type="range" id="rMin">
    <input type="range" id="rMax">
  </div>
  <span class="readout" id="rReadout"></span>
  <button class="reset" id="rReset" type="button">reset</button>
</div>
<div id="main">
  <div id="mapWrap">
    <div id="note"></div>
    <div id="crumb"></div>
    <svg id="map" viewBox="0 0 975 610" preserveAspectRatio="xMidYMid meet"></svg>
    <div id="zoomBox">
      <button id="zIn" type="button" title="Zoom in">+</button>
      <input type="range" id="zSlider" min="0" max="100" value="0">
      <button id="zOut" type="button" title="Zoom out">&minus;</button>
      <span id="zLabel">1&times;</span>
    </div>
    <div id="legend"></div>
    <div id="tooltip"></div>
  </div>
</div>
<footer id="pageFoot"></footer>

<script>__D3__</script>
<script>__TOPOJSON__</script>
<script>
var US_ATLAS = __ATLAS__;
var DATA = __PAYLOAD__;

(function () {
  "use strict";

  var NONE = getComputedStyle(document.documentElement).getPropertyValue("--none").trim() || "#ececea";
  // Sequential single-hue "money" green ramp (light->dark = low->high).
  var RAMP = ["#e5f5e0", "#c7e9c0", "#a1d99b", "#74c476", "#41ab5d", "#238b45", "#005a32"];
  var UNITS = 1000;  // slider resolution (0..UNITS mapped onto [lo, hi])

  var CBV = DATA.cbv, OPT = DATA.opt, COMM = DATA.comm || { aips: [], with_rate: 0, path: "" };
  var CZONE = DATA.commzone || { zones: {}, state_zone: {}, with_rate: 0, path: "" };

  // Metric registry. `family` decides which dataset (and therefore which
  // controls and which tooltip) the metric belongs to; "acre" needs both.
  var METRICS = {
    cbv:  { label: "County Base Value", legend: "County Base Value ($/acre)",
            family: "cbv", needsCbv: true,  needsOpt: false,
            title: "PRF county multiplier — County Base Value ($/acre)",
            sub: "The RMA per-acre county dollar figure that scales PRF protection " +
                 "(acres × productivity factor × coverage level × CBV). Darker = higher $/acre.",
            none: "no value for this selection" },
    win:  { label: "Best win rate", legend: "Best win rate (%)",
            family: "opt", needsCbv: false, needsOpt: true,
            title: "PRF optimizer — best win rate by county",
            sub: "Share of historical years the best interval allocation returned a positive net. " +
                 "Each county shows the best result among the grids it touches.",
            none: "not swept for this selection" },
    net:  { label: "Best return per $1", legend: "Best return per $1 of protection",
            family: "opt", needsCbv: false, needsOpt: true,
            title: "PRF optimizer — best return per $1 of protection",
            sub: "Average net return per $1 of annual protection for the best interval allocation. " +
                 "Multiply by CBV × coverage × productivity for $/acre (see “per ACRE”).",
            none: "not swept for this selection" },
    acre: { label: "Best return per acre", legend: "Best return per acre ($)",
            family: "opt", needsCbv: true,  needsOpt: true,
            title: "PRF optimizer — best return per ACRE ($)",
            sub: "Best return per $1 of protection converted to dollars on one acre using this " +
                 "county's Base Value, the selected coverage level and your productivity factor.",
            none: "no CBV or no swept grid for this selection" },
    // AGENCY-side, and deliberately standalone: it prices the SAME recommended policy the
    // producer metrics rank, it does not re-rank them. Commission is a percent of total
    // premium, so its driver is the premium RATE — which is why this map does not look
    // like the CBV map.
    comm: { label: "Commission per acre", legend: "Commission per acre ($)",
            family: "opt", needsCbv: true,  needsOpt: true, needsComm: true,
            title: "PRF agent commission — $ per ACRE on the recommended policy",
            sub: "Your commission on one acre if the producer buys the optimizer's recommended " +
                 "(best-net) allocation: premium per acre × your negotiated rate for the " +
                 "selected AIP. Commission follows PREMIUM, which is the County Base Value " +
                 "scaled by a premium rate that varies several-fold between grids — so this " +
                 "map ranks counties differently from the CBV map.",
            none: "no CBV, no swept grid, or no commission rate for this selection" }
  };
  var metric = "cbv";

  // ---------------- geometry (same AlbersUSA params us-atlas pre-projects to)
  var path = d3.geoPath(d3.geoAlbersUsa().scale(1300).translate([487.5, 305]));
  var countiesFC = topojson.feature(US_ATLAS, US_ATLAS.objects.counties).features;
  var stateMesh = topojson.mesh(US_ATLAS, US_ATLAS.objects.states, function (a, b) { return true; });

  var statesFC = topojson.feature(US_ATLAS, US_ATLAS.objects.states).features;
  var stateById = {}; statesFC.forEach(function (s) { stateById[String(s.id)] = s; });

  var svg = d3.select("#map");
  var g = svg.append("g");
  var gCounties = g.append("g");
  var countySel = gCounties.selectAll("path").data(countiesFC).join("path")
      .attr("class", "county").attr("d", path)
      // .call(this, ...) — NOT hover(ev, d). Called plainly, `this` inside hover is not
      // the path, so d3.select(this) selects nothing and the .county.hovered rule has
      // never applied. The county highlight visible today comes entirely from the
      // .hoverline outline; this rule was dead from the day it was written.
      .on("mousemove", function (ev, d) { hover.call(this, ev, d); })
      .on("mouseout", unhover)
      .on("click", function (ev, d) { ev.stopPropagation(); countyClicked(d); });
  g.append("path").attr("class", "statelines").attr("d", path(stateMesh));
  // Grid cells sit ABOVE the state mesh so their borders are not hidden by it. Only ever
  // populated for the focused county — a few dozen paths, never the 13,462-cell lattice.
  var gGrids = g.append("g");
  // The drilled county's own outline, drawn ABOVE the cells. Grid cells are opaque and
  // routinely overhang the county (a 0.25-degree cell rarely respects a county line), so
  // without this the county you drilled into vanishes under its own grids.
  var gFocus = g.append("path").attr("class", "focusline");
  // Last of all, so it is never overdrawn by anything above: the hover outline. Two paths,
  // a pale casing under a dark line, both driven by showHover().
  var gHoverCase = g.append("path").attr("class", "hovercase");
  var gHoverLine = g.append("path").attr("class", "hoverline");
  function showHover(feat) {
    var d = feat ? path(feat) : null;
    gHoverCase.attr("d", d);
    gHoverLine.attr("d", d);
  }
  var countyById = {}; countiesFC.forEach(function (c) { countyById[String(c.id)] = c; });

  // ---------------- PRF grid lattice
  // The 0.25-degree cell corners are pure arithmetic on grid_id; RMA ships no shapefile in
  // any feed this project pulls. Mirror of src/prfgrid.py — see that module for how the
  // constants were fitted and validated (99.6% of 30,345 grid/county pairs).
  var GRID_STRIDE = 300, GRID_CELL = 0.25, GRID_LON0 = -130.0, GRID_LAT0 = 20.0;
  function gridBounds(id) {
    var k = id - 1, row = Math.floor(k / GRID_STRIDE), col = k - row * GRID_STRIDE;
    var w = GRID_LON0 + col * GRID_CELL, s = GRID_LAT0 + row * GRID_CELL;
    return [w, s, w + GRID_CELL, s + GRID_CELL];
  }
  // The ring is wound CLOCKWISE on purpose. d3-geo treats polygons as spherical and takes
  // the interior to be the region left of the ring's travel — the opposite of GeoJSON's
  // counter-clockwise-exterior convention — so a counter-clockwise cell renders as the whole
  // globe minus the cell, i.e. every cell floods the viewport. Mirrors prfgrid.grid_polygon.
  function gridFeature(id) {
    var b = gridBounds(id);
    return { type: "Feature", id: id,
             geometry: { type: "Polygon",
                         coordinates: [[[b[0], b[1]], [b[0], b[3]], [b[2], b[3]],
                                        [b[2], b[1]], [b[0], b[1]]]] },
             properties: { grid_id: id } };
  }

  // ---------------- shared controls
  var mSel = document.getElementById("mSel"),
      fUse = document.getElementById("fUse"),
      fOrganic = document.getElementById("fOrganic"),
      fYear = document.getElementById("fYear"),
      fProd = document.getElementById("fProd"),
      fAip = document.getElementById("fAip");

  function fill(sel, items, preferred) {
    var prev = sel.value;
    sel.innerHTML = "";
    items.forEach(function (v) {
      var o = document.createElement("option"); o.value = v; o.textContent = v;
      sel.appendChild(o);
    });
    sel.disabled = items.length < 2;
    if (!items.length) return;
    if (items.indexOf(prev) >= 0) { sel.value = prev; return; }   // keep the user's pick
    var want = null;
    (preferred || []).forEach(function (p) {
      if (want === null && items.indexOf(p) >= 0) want = p;
    });
    sel.value = want !== null ? want : items[0];
  }

  // ONE intended-use control drives BOTH datasets: prf_county's
  // (intended_use, irrigation_practice) pair is keyed as "Haying-Irrigated"
  // etc. server-side, which is prf_opt_best's own vocabulary.
  fill(fUse, DATA.uses, ["Grazing"]);

  // Organic practice varies by use (Grazing carries Conventional only), so the
  // options are rebuilt per use rather than offering combinations with no data.
  function organicsForUse() {
    var byUse = CBV.organics_by_use || {};
    return byUse[fUse.value] || CBV.organics || [];
  }
  function refillOrganic() { fill(fOrganic, organicsForUse(), ["Conventional"]); }
  refillOrganic();

  // Years: newest first; the selector only appears when the data has >1 year.
  var yearsDesc = (CBV.years || []).slice().sort(function (a, b) { return b - a; });
  fill(fYear, yearsDesc.map(String), []);

  // Coverage: the discrete PRF choices the sweep actually stored -> segmented buttons.
  var coverage = OPT.coverages.length
      ? (OPT.coverages.indexOf(DATA.default_coverage) >= 0
          ? DATA.default_coverage : OPT.coverages[OPT.coverages.length - 1])
      : null;
  function covLabel(c) { return Math.round(parseFloat(c) * 100) + "%"; }
  var covButtons = [];
  (function () {
    var seg = document.getElementById("covSeg");
    OPT.coverages.forEach(function (c) {
      var b = document.createElement("button");
      b.type = "button"; b.dataset.c = c; b.textContent = covLabel(c);
      b.classList.toggle("on", c === coverage);
      b.addEventListener("click", function () {
        if (coverage === c) return;
        coverage = c;
        covButtons.forEach(function (x) { x.classList.toggle("on", x === b); });
        onControlChange();
      });
      seg.appendChild(b);
      covButtons.push(b);
    });
  })();

  function prodFactor() {
    var v = parseFloat(fProd.value);
    if (!isFinite(v)) v = DATA.prod.default;
    v = Math.min(DATA.prod.max, Math.max(DATA.prod.min, v));
    return v / 100;
  }

  // AIP roster for the commission metric. EVERY AIP in the seed file is listed, including
  // the ones with no rate entered — hiding them would make an unfilled file look like a
  // short list of AIPs rather than an unfilled file. Picking a rate-less AIP is legal and
  // simply leaves the map neutral with a note naming the file to edit.
  (function () {
    COMM.aips.forEach(function (a, i) {
      var o = document.createElement("option");
      o.value = String(i);
      o.textContent = a.name + (a.pct === null || a.pct === undefined
        ? " — no rate set" : " — " + a.pct + "%");
      fAip.appendChild(o);
    });
    var first = -1;
    COMM.aips.forEach(function (a, i) {
      if (first < 0 && a.pct !== null && a.pct !== undefined) first = i;
    });
    if (!COMM.aips.length) {
      var o2 = document.createElement("option");
      o2.textContent = "no AIPs in " + COMM.path;
      fAip.appendChild(o2);
      fAip.disabled = true;
    } else {
      fAip.value = String(first >= 0 ? first : 0);
    }
  })();
  function aip() {
    var a = COMM.aips[parseInt(fAip.value, 10)];
    return a || null;
  }
  // The county's time zone, or null off-lattice. Zone is assigned per STATE (majority zone
  // for the thirteen states that straddle a boundary) — see prfpage.STATE_TIMEZONE.
  function zoneFor(fips) {
    if (!fips) return null;
    return CZONE.state_zone[String(fips).slice(0, 2)] || null;
  }
  function zonePctFor(fips) {
    var z = zoneFor(fips);
    if (!z) return null;
    var v = CZONE.zones[z];
    return (v === null || v === undefined) ? null : v;
  }
  // Rates vary BOTH by region within one AIP and between AIPs, and the same rate recurs
  // across AIPs — so the rate is a cell in an AIP x region table, not a property of either
  // one alone. Resolution, most specific first:
  //   1. this AIP's rate for this county's region
  //   2. this AIP's flat rate, if the file carries one
  //   3. the region default in commission_by_timezone.csv
  // null, never 0: "no rate entered" and "0% commission" are different claims and only one
  // of them may be multiplied into a dollar figure.
  function commSource(fips) {
    var a = aip(), zone = zoneFor(fips);
    if (a && zone && a.by_region && a.by_region[zone] !== undefined) {
      return { pct: a.by_region[zone], from: esc(a.name) + " · " + esc(zone),
               path: COMM.path };
    }
    if (a && a.pct !== null && a.pct !== undefined) {
      return { pct: a.pct, from: esc(a.name) + " (all regions)", path: COMM.path };
    }
    var z = zonePctFor(fips);
    if (z !== null) {
      return { pct: z, from: esc(zone) + " region default", path: CZONE.path };
    }
    return { pct: null, from: null, path: null };
  }
  function commPct(fips) { return commSource(fips).pct; }
  // True when SOME rate exists anywhere — drives the "no rates set" notice.
  function anyRateSet() {
    return CZONE.with_rate > 0 || COMM.with_rate > 0;
  }

  // ---------------- value lookups
  function cbvFor(fips) {
    var c = CBV.counties[fips]; if (!c) return null;
    var a = c[fUse.value];      if (!a) return null;
    var o = a[fOrganic.value];  if (!o) return null;
    var v = o[fYear.value];
    return (v === undefined || v === null) ? null : v;
  }
  function optCell(fips) {
    var c = OPT.counties[fips]; if (!c) return null;
    var a = c[fUse.value];      if (!a) return null;
    return a[coverage] || null;
  }
  // $/acre = best net (per $1 of protection) x CBV x coverage x productivity.
  function acreFrom(net, cbv) {
    if (net === null || net === undefined || cbv === null || coverage === null) return null;
    return net * cbv * parseFloat(coverage) * prodFactor();
  }
  // Dollar protection on one acre — the common factor of both per-acre metrics.
  function protectionFrom(cbv) {
    if (cbv === null || cbv === undefined || coverage === null) return null;
    return cbv * parseFloat(coverage) * prodFactor();
  }
  // TOTAL premium per acre (subsidised portion included — agent commission is computed on
  // the whole premium): protection x the allocation-weighted premium rate of the stored
  // best-net policy. `rs` is prf_opt_best.best_net_rate_sum, precomputed server-side
  // because prf_grid_rate is far too big to ship with the app.
  function premiumFrom(rs, cbv) {
    var prot = protectionFrom(cbv);
    if (prot === null || rs === null || rs === undefined) return null;
    return prot * rs;
  }
  function commFrom(rs, cbv, fips) {
    var prem = premiumFrom(rs, cbv), pct = commPct(fips);
    if (prem === null || pct === null) return null;
    return prem * pct / 100;
  }
  function valFor(fips) {
    if (metric === "cbv") return cbvFor(fips);
    var cell = optCell(fips);
    if (!cell) return null;
    if (metric === "win") return cell.win === undefined ? null : cell.win;
    if (metric === "net") return cell.net === undefined ? null : cell.net;
    if (metric === "comm") return commFrom(cell.nrs, cbvFor(fips), fips);
    return acreFrom(cell.net, cbvFor(fips));   // acre
  }

  // ---------------- formatting
  function fmtWin(v) { return v === null || v === undefined ? "&mdash;" : (v * 100).toFixed(1) + "%"; }
  function fmtNet(v) { return v === null || v === undefined ? "&mdash;" : "$" + v.toFixed(3); }
  function fmtCbv(v) {
    if (v === null || v === undefined) return "&mdash;";
    return "$" + v.toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "/ac";
  }
  function fmtAcre(v) {
    if (v === null || v === undefined) return "&mdash;";
    return "$" + v.toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "/ac";
  }
  function fmtRate(v) {
    return v === null || v === undefined ? "&mdash;" : v.toFixed(5);
  }
  function fmtPct(v) {
    return v === null || v === undefined ? "&mdash;" : (+v).toFixed(2).replace(/\.?0+$/, "") + "%";
  }
  function fmtFull(v) {
    if (metric === "win") return fmtWin(v);
    if (metric === "net") return fmtNet(v) + " per $1";
    if (metric === "cbv") return fmtCbv(v);
    return fmtAcre(v);   // acre + comm are both $/acre
  }
  // Compact form for the slider bubbles, readout and legend ticks — the units
  // ARE the metric's units, so the same slider reads %, $ per $1, or $/acre.
  function fmtShort(v) {
    if (v === null || v === undefined) return "&mdash;";
    var span = Math.abs(hi - lo);
    if (metric === "win") return Math.round(v * 100) + "%";
    if (metric === "net") return "$" + v.toFixed(span < 1 ? 3 : 2);
    if (metric === "cbv") return "$" + Math.round(v).toLocaleString();
    // acre / comm: cents matter here (a per-acre return, and even more so a per-acre
    // commission, is single-digit dollars on rangeland), so keep 2 decimals until the
    // numbers get big.
    return "$" + (Math.abs(v) < 100 ? v.toFixed(2) : Math.round(v).toLocaleString());
  }
  function readoutSuffix() {
    if (metric === "win") return "";
    if (metric === "net") return " per $1";
    return "/ac";
  }

  // ---------------- metric-dependent color scale + range slider
  var lo = 0, hi = 1, hasData = false, scale = null;
  var rMin = document.getElementById("rMin"),
      rMax = document.getElementById("rMax"),
      rFill = document.getElementById("rFill"),
      rReadout = document.getElementById("rReadout"),
      rBar = document.getElementById("rangebar"),
      bubbleLo = document.getElementById("rBubbleLo"),
      bubbleHi = document.getElementById("rBubbleHi");
  [rMin, rMax].forEach(function (r) { r.min = 0; r.max = UNITS; r.step = 1; });

  // Domain: fixed and global for the three STORED metrics (a given value is
  // always the same green). "acre" and "comm" are DERIVED from the current coverage /
  // organic / year / productivity (and, for comm, AIP) election, so their domain is
  // recomputed over exactly what is on screen — otherwise the ramp would be meaningless.
  function domainFor() {
    if (metric === "cbv") return [CBV.min, CBV.max];
    if (metric === "win") return [OPT.min_win, OPT.max_win];
    if (metric === "net") return [OPT.min_net, OPT.max_net];
    var min = null, max = null;
    for (var fips in OPT.counties) {
      var v = valFor(fips);
      if (v === null) continue;
      if (min === null || v < min) min = v;
      if (max === null || v > max) max = v;
    }
    return [min, max];
  }

  function syncControls() {
    var m = METRICS[metric];
    // Coverage level is meaningless for a County Base Value — hide it rather
    // than leave a dead control on the page.
    document.getElementById("covWrap").style.display =
      (m.needsOpt && OPT.coverages.length) ? "" : "none";
    // Organic practice + crop year only move a CBV, so they show for the two
    // metrics that read one.
    document.getElementById("organicWrap").style.display =
      (m.needsCbv && organicsForUse().length) ? "" : "none";
    document.getElementById("yearWrap").style.display =
      (m.needsCbv && yearsDesc.length > 1) ? "" : "none";
    // Productivity factor scales protection, so it enters BOTH per-acre formulas.
    document.getElementById("prodWrap").style.display =
      (metric === "acre" || metric === "comm") ? "" : "none";
    // Whose commission rate to apply — meaningless for every other metric.
    document.getElementById("aipWrap").style.display = (metric === "comm") ? "" : "none";
    // "block", not "": the stylesheet hides #formulaNote by default, so clearing
    // the inline style would fall straight back through to display:none. Each metric's
    // note lives in the markup (one .fn per metric) and only its own is switched on.
    var notes = document.querySelectorAll("#formulaNote .fn"), anyNote = false;
    Array.prototype.forEach.call(notes, function (el) {
      var on = el.getAttribute("data-m") === metric;
      el.classList.toggle("on", on);
      if (on) anyNote = true;
    });
    document.getElementById("formulaNote").style.display = anyNote ? "block" : "none";
    document.getElementById("pageTitle").textContent = m.title;
    document.getElementById("pageSub").textContent = m.sub + " Generated " + DATA.generated + ".";
    document.getElementById("pageFoot").textContent = metric === "cbv"
      ? "County Base Value is county-grain RMA data (the rainfall index and rates are grid-grain). "
        + "Counties with no CBV for the current use / organic / year selection are shown neutral."
      : metric === "comm"
      ? "Commission is a percent of TOTAL premium (subsidised portion included) on the "
        + "optimizer's recommended best-net allocation for the best-net grid in each county — "
        + "not a maximum-premium figure, and not advice on what to sell. The premium comes from "
        + "the allocation-weighted premium rate stored with each swept policy. Commission rates "
        + "are your own, hand-entered in " + COMM.path + " as an AIP x region table, and the "
        + "values shipped there are SAMPLE data, not negotiated rates. The SRA's 80%-of-A&O cap on total "
        + "agent compensation and RMA's schemes-or-devices rules bound them and are not modelled "
        + "here. Counties with no swept grid, no County Base Value, or no rate for the selected "
        + "AIP are shown neutral."
      : "Optimizer metrics are stored per $1 of protection; a county spanning several grids shows "
        + "the best grid per metric (hover for the per-grid winning allocations). Counties with no "
        + "swept grid" + (metric === "acre" ? " — or no County Base Value — " : " ")
        + "for the current selection are shown neutral.";
  }

  function applyMetric() {
    var mm = domainFor();
    hasData = mm[0] !== null && mm[0] !== undefined && mm[1] !== null && mm[1] !== undefined;
    lo = hasData ? mm[0] : 0;
    hi = hasData ? mm[1] : 1;
    if (hi <= lo) hi = lo + (metric === "win" ? 0.05 : 0.01);
    scale = d3.scaleQuantize().domain([lo, hi]).range(RAMP);
    rMin.value = 0; rMax.value = UNITS;
    rBar.style.display = hasData ? "" : "none";
    document.getElementById("rangeLabel").textContent = METRICS[metric].legend + " range";
    updateRange();
    refresh();
  }

  function unitLo() { return Math.min(+rMin.value, +rMax.value); }
  function unitHi() { return Math.max(+rMin.value, +rMax.value); }
  function toVal(u) { return lo + (hi - lo) * u / UNITS; }
  function rangeLo() { return toVal(unitLo()); }
  function rangeHi() { return toVal(unitHi()); }
  function isFullRange() { return unitLo() <= 0 && unitHi() >= UNITS; }
  function updateRange() {
    var a = unitLo() / UNITS, b = unitHi() / UNITS;
    rFill.style.left = (a * 100) + "%";
    rFill.style.width = ((b - a) * 100) + "%";
    // Value label above each thumb, offset for the 16px thumb width so it tracks the handle.
    bubbleLo.innerHTML = fmtShort(rangeLo());
    bubbleLo.style.left = "calc(" + (a * 100) + "% - " + (a * 16 - 8) + "px)";
    bubbleHi.innerHTML = fmtShort(rangeHi());
    bubbleHi.style.left = "calc(" + (b * 100) + "% - " + (b * 16 - 8) + "px)";
    rReadout.innerHTML = fmtShort(rangeLo()) + " &ndash; " + fmtShort(rangeHi()) + readoutSuffix();
  }

  function drawLegend() {
    var el = document.getElementById("legend");
    if (!hasData) { el.style.display = "none"; return; }
    el.style.display = "";
    var cells = RAMP.map(function (c) {
      return '<div class="l-cell" style="background:' + c + '"></div>';
    }).join("");
    // scaleQuantize thresholds() gives the internal breakpoints (n-1 of them).
    var thr = scale.thresholds();
    var labels = '<span>' + fmtShort(lo) + '</span>' +
      thr.map(function (t) { return '<span>' + fmtShort(t) + '</span>'; }).join("");
    el.innerHTML =
      '<div class="l-title">' + METRICS[metric].legend + '</div>' +
      '<div class="l-row">' + cells + '</div>' +
      '<div class="l-labels">' + labels + '</div>' +
      '<div class="l-none"><span class="sw"></span>' + METRICS[metric].none + '</div>';
  }

  // ---------------- hover / tooltip
  var tip = document.getElementById("tooltip");
  function nameFor(d) {
    return DATA.county_names[d.id] || (d.properties && d.properties.name) || "County";
  }
  // Winning allocations are interned in OPT.policies (see build_opt_payload):
  // policies[i] = [interval codes, % allocations]; -1 means "no policy stored".
  function comboStr(pix) {
    var p = (pix === undefined || pix === null || pix < 0) ? null : OPT.policies[pix];
    if (!p || !p[0] || !p[0].length) return "?";
    var combo = p[0], props = p[1];
    return combo.map(function (iv, i) {
      var pct = props && props[i] !== undefined ? props[i] + "%" : "?";
      return iv + " " + pct;
    }).join(" · ");
  }
  // WHICH of the two stored policies the current selection is asking about.
  //
  // The optimiser maximises win rate and net return SEPARATELY, so a grid has no single
  // "recommended policy" — it has two, and they are usually different allocations with
  // different win rates and different returns. Which one you want depends entirely on what
  // you selected: looking at the win-rate map you want the allocation that wins most often;
  // looking at a dollar map you want the one that returns most. Reporting the wrong one, or
  // mixing halves of both, is what made these tooltips read as nonsense.
  //
  // Returns every figure for ONE policy, so a caller cannot pair a rate with the wrong
  // intervals: its win rate, its net per $1, its allocation, and what to call it. Falls back
  // to the other policy if the wanted one was not stored for this grid.
  function policyFor(gd) {
    if (!gd) return null;
    var wantWin = (metric === "win");
    var pol = wantWin ? gd.wp : gd.np;
    if (pol === undefined || pol === null) {   // not stored — fall back rather than blank out
      wantWin = !wantWin;
      pol = wantWin ? gd.wp : gd.np;
    }
    if (pol === undefined || pol === null) return null;
    return {
      win:  wantWin ? gd.win : gd.net_win,
      net:  wantWin ? gd.win_net : gd.net,
      pol:  pol,
      kind: wantWin ? "chosen for win rate" : "chosen for return"
    };
  }
  // One policy's numbers, in the order the selection cares about: the selected quantity
  // first. Both are always shown — a win rate without its return, or a return without the
  // odds of getting it, is half an answer either way.
  function policyLine(gd, cbv) {
    var p = policyFor(gd);
    if (!p) return null;
    var acre = acreFrom(p.net, cbv);
    // ONE vocabulary for each quantity, in both views. The win rate used to print bare on
    // the win view ("best win rate: 84.2%") and as "68.4% of years" on the dollar view — the
    // same measure in two different phrasings, which reads as two unrelated measures and
    // hides the only thing that actually differs: they belong to different allocations.
    var wins = "wins " + fmtWin(p.win) + " of years";
    var per1 = fmtNet(p.net) + " per $1";
    var bits = (metric === "win") ? [wins, per1] : [per1, wins];
    if (acre !== null) bits.splice(metric === "win" ? 2 : 1, 0, fmtAcre(acre));  // carries /ac
    return p.kind + ": " + bits.join(" &middot; ") + " &mdash; " + esc(comboStr(p.pol));
  }
  // Same numbers as policyLine, one metric per line instead of joined with middots — used
  // only at the grid level, where a single grid is the whole tooltip and there is room to
  // give best win rate, best per-$ return, and best per-acre return each their own row
  // rather than a single line that has to be read left to right to find one of the three.
  function policyLinesHtml(gd, cbv) {
    var p = policyFor(gd);
    if (!p) return null;
    var acre = acreFrom(p.net, cbv);
    var rows = (metric === "win")
      ? [["Best win rate", fmtWin(p.win) + " of years"],
         ["Best per-$ return", fmtNet(p.net) + " per $1"]]
      : [["Best per-$ return", fmtNet(p.net) + " per $1"],
         ["Best win rate", fmtWin(p.win) + " of years"]];
    if (acre !== null) rows.splice(metric === "win" ? 2 : 1, 0, ["Best per-acre return", fmtAcre(acre)]);
    var h = '<div class="t-line">' + p.kind + ' &mdash; ' + esc(comboStr(p.pol)) + '</div>';
    rows.forEach(function (r) { h += '<div class="t-line">' + r[0] + ': ' + r[1] + '</div>'; });
    return h;
  }
  function selLabel() {
    var s = esc(fUse.value);
    if (METRICS[metric].needsOpt && coverage !== null) s += " @ " + covLabel(coverage) + " coverage";
    if (METRICS[metric].needsCbv) {
      s += " · " + esc(fOrganic.value);
      if (yearsDesc.length > 1) s += " " + esc(fYear.value);
    }
    if (METRICS[metric].needsComm && aip()) s += " · " + esc(aip().name);
    return s;
  }

  // CBV tooltip — county detail for the County Base Value metric.
  // NO PRF METRIC SUMS ACROSS COUNTIES. CBV is a per-acre county figure, win rate is a
  // share, and the three return metrics are per $1 or per acre — adding any of them up
  // produces a number with no meaning. So the state box shows the RANGE across the state's
  // counties, which is honest about the spread and invents nothing. (The row-crop map does
  // carry summable dollar totals, which is why its version has a SUMMABLE list and this
  // one does not.)
  function stateTip(fips2) {
    // At the nation level the hover outlines the STATE, because that is what a click
    // selects. Describing a county in the box beside it was the mismatch: the highlight
    // said one thing and the tooltip another, with no way to tell which the number was for.
    var vals = [], best = null, bestFips = null;
    // CBV is keyed by county in CBV.counties; every optimizer metric in OPT.counties.
    var src = (metric === "cbv") ? CBV.counties : OPT.counties;
    for (var fips in (src || {})) {
      if (String(fips).slice(0, 2) !== fips2) continue;
      var v = valFor(fips);
      if (v === null || v === undefined) continue;
      vals.push(v);
      if (best === null || v > best) { best = v; bestFips = fips; }
    }
    var h = '<div class="t-name">' + esc(stateNameOf(fips2)) + ' &mdash; ' +
            selLabel() + '</div>';
    if (!vals.length) {
      return h + '<div class="t-val">no county in this state has a value for this ' +
             'selection</div>';
    }
    h += '<div class="t-val">' + esc(METRICS[metric].legend) + ': <b>' +
         fmtFull(Math.min.apply(null, vals)) + ' &ndash; ' +
         fmtFull(Math.max.apply(null, vals)) + '</b> across counties</div>';
    h += '<div class="t-line">' + vals.length +
         (vals.length === 1 ? ' county' : ' counties') +
         ' with a value &mdash; a range, not an average: ' +
         'every PRF metric here is a rate or a per-unit figure, and a state mean would need ' +
         'weights this box cannot show</div>';
    if (bestFips) {
      h += '<div class="t-line">best county: ' +
           esc(DATA.county_names[bestFips] || bestFips) + ' at ' + fmtFull(best) + '</div>';
    }
    h += '<div class="t-more">Click to zoom in and read counties individually.</div>';
    return h;
  }

  function cbvTip(d) {
    var v = cbvFor(d.id);
    return '<div class="t-name">' + esc(nameFor(d)) + ' County &mdash; ' + selLabel() + '</div>' +
      '<div class="t-val">County Base Value ' +
      (v === null ? "no value for this selection" : fmtCbv(v)) + '</div>';
  }

  // The commission arithmetic, factor by factor: CBV, coverage, productivity, the stored
  // allocation-weighted premium rate, the resulting premium, the commission %, and the
  // commission — plus WHICH allocation (and which grid) was priced, because a county can
  // span grids and the number is only meaningful attached to one policy.
  function commMath(cell, cbv, fips) {
    var src = commSource(fips), pct = src.pct, a = aip();
    var zone = zoneFor(fips);
    var prem = premiumFrom(cell.nrs, cbv), c = commFrom(cell.nrs, cbv, fips);
    var h = '';
    if (c === null) {
      var why = pct === null
        ? 'no commission rate for ' + esc(a ? a.name : "this AIP") + ' or for the ' +
          esc(zone || "unknown") + ' zone &mdash; add one in ' + esc(COMM.path) + ' or ' +
          esc(CZONE.path)
        : (cbv === null ? 'no County Base Value for this selection'
                        : 'no stored premium rate for the recommended allocation');
      return '<div class="t-math">no commission per acre &mdash; ' + why + '</div>';
    }
    var pf = prodFactor(), cov = parseFloat(coverage);
    h += '<div class="t-math">protection: CBV ' + fmtCbv(cbv).replace("/ac", "") +
         ' &times; ' + cov.toFixed(2) + ' coverage &times; ' + pf.toFixed(2) +
         ' productivity = ' + fmtAcre(protectionFrom(cbv)) + '</div>';
    h += '<div class="t-math">premium: &times; rate ' + fmtRate(cell.nrs) +
         ' (allocation-weighted) = <b>' + fmtAcre(prem) + '</b> total premium</div>';
    // Name the SOURCE of the rate, not just the number — a regional rate and a per-AIP rate
    // are different claims, and the map is unreadable if you can't tell which one you are
    // looking at (or that the shipped zone rates are sample data).
    h += '<div class="t-math">commission: &times; ' + fmtPct(pct) +
         (src.from ? ' (' + src.from + ', ' + esc(src.path) + ')' : '') +
         ' = <b>' + fmtAcre(c) + '</b></div>';
    var g = (cell.ni !== undefined && cell.ni >= 0) ? OPT.grid_detail[cell.ni] : null;
    h += '<div class="t-math">priced on the recommended (best-net) allocation' +
         (g ? ', grid ' + esc(g.grid) + ': ' + esc(comboStr(g.np)) : '') + '</div>';
    return h;
  }

  // Optimizer tooltip — keeps the per-grid winning-allocation detail, and for
  // the per-acre metric shows the CBV and the arithmetic so the $ is traceable.
  function optTip(d) {
    var name = esc(nameFor(d)) + " County";
    var cell = optCell(d.id);
    var cbv = cbvFor(d.id);
    if (!cell) {
      return '<div class="t-name">' + name + '</div>' +
             '<div class="t-val">not swept for ' + selLabel() + '</div>';
    }
    var n = cell.g.length;
    var h = '<div class="t-name">' + name + ' &mdash; ' + selLabel() + '</div>' +
      '<div class="t-val">best win rate ' + fmtWin(cell.win) +
      ' &middot; best net ' + fmtNet(cell.net) + ' per $1 &middot; ' +
      n + ' grid' + (n === 1 ? '' : 's') + '</div>';
    if (metric === "acre") {
      var pf = prodFactor();
      var acre = acreFrom(cell.net, cbv);
      h += '<div class="t-math">CBV ' + fmtCbv(cbv) + '</div>';
      h += '<div class="t-math">' +
        (acre === null
          ? 'no return per acre &mdash; ' + (cbv === null ? 'no County Base Value' : 'no net return')
            + ' for this selection'
          : fmtNet(cell.net) + ' &times; ' + fmtCbv(cbv).replace("/ac", "") + ' &times; ' +
            parseFloat(coverage).toFixed(2) + ' &times; ' + pf.toFixed(2) + ' = <b>' +
            fmtAcre(acre) + '</b>') +
        '</div>';
    }
    // Every factor spelled out, so the dollar figure can be checked by hand against the
    // seed CSV and RMA's own rate sheet rather than taken on faith.
    if (metric === "comm") h += commMath(cell, cbv, d.id);
    cell.g.slice(0, 5).forEach(function (ix) {
      var gr = OPT.grid_detail[ix];
      h += '<div class="t-grid"><div class="t-gid">Grid ' + esc(gr.grid) + '</div>';
      // The policy the SELECTION is asking about, with its own win rate and its own return.
      // This used to list BOTH stored policies on every grid: twice the reading for one
      // answer, and an invitation to pair a rate off one line with intervals off the other —
      // which is exactly the mistake the grid tooltip made.
      var gline = policyLine(gr, cbv);
      if (gline !== null) h += '<div class="t-line">' + gline + '</div>';
      h += '</div>';
    });
    if (n > 5) h += '<div class="t-more">+' + (n - 5) + ' more grids</div>';
    return h;
  }

  function hover(ev, d) {
    // ONE HIGHLIGHT AT A TIME. At the nation level the STATE is the thing being pointed at —
    // a click selects it, the outline traces it and the tooltip describes it — so the county
    // under the cursor must NOT also be stroked. Two nested highlights read as two things
    // selected, and the inner one promises a county selection the click will not make.
    // Below the nation level the county IS the target, so it marks normally.
    d3.select(this).classed("hovered", level !== 0);
    // Outline what a CLICK would SELECT, not what the cursor is literally over. At the
    // nation level a click on a county zooms to its STATE, so tracing the county there
    // advertises a selection the click will not make; at state level the click does drill
    // into that county, so the county is right. The tooltip keeps describing the county
    // either way, because the choropleth is shaded by county at every level.
    showHover(level === 0 ? (stateById[String(d.id).slice(0, 2)] || d) : d);
    tip.style.display = "block";
    // The box must describe whatever the outline is around — see showHover above.
    tip.innerHTML = (level === 0)
      ? stateTip(String(d.id).slice(0, 2))
      : (metric === "cbv" ? cbvTip(d) : optTip(d));
    var wrap = document.getElementById("mapWrap").getBoundingClientRect();
    var x = ev.clientX - wrap.left + 14, y = ev.clientY - wrap.top + 10;
    if (x > wrap.width - 340) x -= 360;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function unhover() {
    d3.select(this).classed("hovered", false);
    showHover(null);
    tip.style.display = "none";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---------------- drill-down: nation -> state -> grid
  // Three DATA levels, because PRF has exactly three grains and no more: County Base Values
  // and product availability are county-published, and the 0.25-degree rainfall grid is the
  // finest unit RMA prices. The slider magnifies continuously, but nothing new appears below
  // a grid cell — so zoom and granularity are deliberately separate here.
  var K_MIN = 1, K_MAX = 96;
  var level = 0;            // 0 nation, 1 state, 2 county
  var focusState = null;    // 2-digit state FIPS
  var focusCounty = null;   // 5-digit county FIPS
  var curK = 1;

  var zoom = d3.zoom().scaleExtent([K_MIN, K_MAX]).on("zoom", function (ev) {
    g.attr("transform", ev.transform);
    curK = ev.transform.k;
    syncZoomUI();
  });
  svg.call(zoom).on("dblclick.zoom", null);
  svg.on("click", function () { drillOut(); });

  // Animate, EXCEPT in a hidden tab. d3's transition scheduler runs on requestAnimationFrame,
  // which browsers suspend while a tab is backgrounded, so an animated zoom started there
  // never advances past CREATED — the map freezes mid-flight and stays frozen after you
  // switch back. Applying the transform directly when document.hidden keeps it correct.
  function applyTransform(t, dur) {
    if (dur === 0 || document.hidden) svg.call(zoom.transform, t);
    else svg.transition().duration(dur).call(zoom.transform, t);
  }
  function zoomToFeature(f, dur) {
    var b = path.bounds(f);
    var dx = b[1][0] - b[0][0], dy = b[1][1] - b[0][1];
    var cx = (b[0][0] + b[1][0]) / 2, cy = (b[0][1] + b[1][1]) / 2;
    var k = Math.max(K_MIN, Math.min(K_MAX, 0.85 / Math.max(dx / 975, dy / 610)));
    applyTransform(d3.zoomIdentity.translate(975 / 2 - k * cx, 610 / 2 - k * cy).scale(k),
                   dur === undefined ? 650 : dur);
  }
  function resetZoom(dur) {
    applyTransform(d3.zoomIdentity, dur === undefined ? 650 : dur);
  }

  // A county click means "closer": from the nation it selects that county's STATE (clicking
  // a 3-pixel county to select the state is what a user actually intends at that zoom);
  // from the state it drills to the county and draws its grid cells.
  function countyClicked(d) {
    var st = String(d.id).slice(0, 2);
    if (level === 0) {
      focusState = st; focusCounty = null; level = 1;
      if (stateById[st]) zoomToFeature(stateById[st]);
    } else {
      focusState = st; focusCounty = String(d.id); level = 2;
      zoomToFeature(d);
    }
    applyLevel();
  }
  function drillOut() {
    if (level === 2) { focusCounty = null; level = 1;
                       if (stateById[focusState]) zoomToFeature(stateById[focusState]); }
    else if (level === 1) { focusState = null; level = 0; resetZoom(); }
    else return;
    applyLevel();
  }
  function drillTo(lv) {
    if (lv === 0) { level = 0; focusState = null; focusCounty = null; resetZoom(); }
    else if (lv === 1 && focusState) { level = 1; focusCounty = null;
                                       if (stateById[focusState]) zoomToFeature(stateById[focusState]); }
    applyLevel();
  }

  // Per-grid values for the focused county, from the SAME metric the counties use. CBV is
  // published per county, so every cell in a county carries its county's value — that is the
  // data, not a shortcut, and the tooltip says so.
  function gridDetailMap(fips) {
    var out = {}, cell = optCell(fips);
    if (cell && cell.g) {
      cell.g.forEach(function (i) {
        var gd = OPT.grid_detail[i];
        if (gd) out[gd.grid] = gd;
      });
    }
    return out;
  }
  function gridValue(gid, fips, gdm) {
    if (metric === "cbv") return cbvFor(fips);
    var gd = gdm[gid];
    if (!gd) return null;
    if (metric === "win") return gd.win === undefined ? null : gd.win;
    if (metric === "net") return gd.net === undefined ? null : gd.net;
    if (metric === "comm") return commFrom(gd.nrs, cbvFor(fips), fips);
    return acreFrom(gd.net, cbvFor(fips));
  }

  function renderGrids() {
    var focusFeat = focusCounty ? countyById[focusCounty] : null;
    gFocus.attr("d", (level === 2 && focusFeat) ? path(focusFeat) : null);
    if (level !== 2 || !focusCounty) { gGrids.selectAll("path").remove(); return; }
    var ids = (OPT.grids_by_county && OPT.grids_by_county[focusCounty]) || [];
    var gdm = gridDetailMap(focusCounty);
    var fips = focusCounty;
    gGrids.selectAll("path")
      .data(ids.map(gridFeature), function (d) { return d.id; })
      .join("path")
        .attr("class", "gridcell")
        .attr("d", path)
        .attr("fill", function (d) {
          var v = gridValue(d.id, fips, gdm);
          if (v === null || v === undefined) return NONE;
          var rlo = rangeLo(), rhi = rangeHi(), eps = (hi - lo) * 1e-9;
          if (v < rlo - eps || v > rhi + eps) return NONE;
          return scale(v);
        })
        .on("mousemove", function (ev, d) { hoverGrid.call(this, ev, d, fips, gdm); })
        .on("mouseout", unhover)
        // TERMINAL SHAPE: nothing below this level to drill into, so the click does
        // NOTHING — and specifically must not reach the background handler, which zooms OUT.
        // Clicking the thing you are already looking at should never throw you back up a
        // level: "no deeper" and "go back" are different intentions and only one of them was
        // expressed. stopPropagation is the whole fix.
        .on("click", function (ev) { ev.stopPropagation(); });
  }

  function hoverGrid(ev, d, fips, gdm) {
    d3.select(this).classed("hovered", true);
    showHover(d);
    var v = gridValue(d.id, fips, gdm), gd = gdm[d.id];
    var b = gridBounds(d.id);
    var h = '<div class="t-name">Grid ' + d.id + ' &mdash; ' +
            esc(DATA.county_names[fips] || "") + ' County</div>' +
            '<div class="t-val">' + esc(METRICS[metric].legend) + ': ' +
            (v === null || v === undefined ? "no value" : fmtShort(v)) + '</div>';
    if (metric === "cbv") {
      h += '<div class="t-line">County Base Value is published per COUNTY, so every grid ' +
           'in this county shares it.</div>';
    } else if (gd) {
      // One policy, chosen by the selection, every figure taken from it. See policyFor().
      var linesHtml = policyLinesHtml(gd, cbvFor(fips));
      h += linesHtml === null
        ? '<div class="t-line">no stored policy for this grid</div>'
        : linesHtml;
    } else {
      h += '<div class="t-line">No swept result for this grid at the current selection.</div>';
    }
    h += '<div class="t-math">' + b[1].toFixed(2) + '&deg;N ' + Math.abs(b[0]).toFixed(2) +
         '&deg;W &mdash; 0.25&deg; cell</div>';
    tip.style.display = "block"; tip.innerHTML = h;
    var wrap = document.getElementById("mapWrap").getBoundingClientRect();
    var x = ev.clientX - wrap.left + 14, y = ev.clientY - wrap.top + 10;
    if (x > wrap.width - 340) x -= 360;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }

  // Counties outside the focus are dimmed rather than hidden: the surrounding shading is
  // the comparison that makes a drilled county mean anything.
  function applyLevel() {
    // Drilling replaces what is under the cursor, and the shape the outline was tracing may
    // no longer exist (grid cells are removed wholesale on the way out of level 2). mouseout
    // does not reliably fire for a node that is removed, so clear it here — the single choke
    // point every level change already passes through.
    showHover(null);
    countySel
      .classed("dimmed", function (d) {
        if (level === 0) return false;
        return String(d.id).slice(0, 2) !== focusState;
      })
      .classed("focused", function (d) { return level === 2 && String(d.id) === focusCounty; });
    renderGrids();
    updateCrumb();
  }

  var crumb = document.getElementById("crumb");
  function stateNameOf(fips) {
    var s = stateById[fips];
    return (s && s.properties && s.properties.name) || "State";
  }
  function updateCrumb() {
    var h = '<span class="' + (level === 0 ? "c-here" : "c-step") + '" data-lv="0">United States</span>';
    if (focusState) {
      h += '<span class="c-sep">&rsaquo;</span><span class="' +
           (level === 1 ? "c-here" : "c-step") + '" data-lv="1">' +
           esc(stateNameOf(focusState)) + '</span>';
    }
    if (focusCounty) {
      h += '<span class="c-sep">&rsaquo;</span><span class="c-here">' +
           esc(DATA.county_names[focusCounty] || "County") + ' County</span>';
    }
    var hint = level === 0 ? "click a state to zoom"
             : level === 1 ? "click a county for its grids"
             : ((OPT.grids_by_county && OPT.grids_by_county[focusCounty] || []).length +
                " grid cells &middot; click away to go back");
    h += '<span class="c-hint">' + hint + '</span>';
    crumb.innerHTML = h;
    crumb.querySelectorAll(".c-step").forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.stopPropagation(); drillTo(parseInt(el.getAttribute("data-lv"), 10));
      });
    });
  }

  // ---------------- zoom slider
  // Logarithmic, so each step of the thumb is the same PROPORTIONAL change — a linear map
  // would spend most of its travel in the first 2x.
  var zSlider = document.getElementById("zSlider"), zLabel = document.getElementById("zLabel");
  function kToSlider(k) { return 100 * Math.log(k / K_MIN) / Math.log(K_MAX / K_MIN); }
  function sliderToK(v) { return K_MIN * Math.pow(K_MAX / K_MIN, v / 100); }
  function syncZoomUI() {
    zSlider.value = String(kToSlider(curK));
    zLabel.innerHTML = (curK < 10 ? curK.toFixed(1) : Math.round(curK)) + "&times;";
  }
  function zoomToK(k) {
    var kk = Math.max(K_MIN, Math.min(K_MAX, k));
    if (document.hidden) svg.call(zoom.scaleTo, kk);
    else svg.transition().duration(120).call(zoom.scaleTo, kk);
  }
  zSlider.addEventListener("input", function () { zoomToK(sliderToK(+zSlider.value)); });
  document.getElementById("zIn").addEventListener("click", function (ev) {
    ev.stopPropagation(); zoomToK(curK * 1.6);
  });
  document.getElementById("zOut").addEventListener("click", function (ev) {
    ev.stopPropagation(); zoomToK(curK / 1.6);
  });

  // ---------------- render / recolor
  function refresh() {
    var rlo = rangeLo(), rhi = rangeHi();
    var eps = (hi - lo) * 1e-9;
    var shaded = 0;
    countySel.attr("fill", function (d) {
      var v = valFor(d.id);
      if (v === null || v < rlo - eps || v > rhi + eps) return NONE;  // outside range -> neutral
      shaded++;
      return scale(v);
    });

    var line = document.getElementById("countLine");
    line.innerHTML = !hasData ? "" : (isFullRange()
      ? shaded + " counties with " + METRICS[metric].legend.toLowerCase() + " for " + selLabel()
      : shaded + " counties at " + fmtShort(rlo) + "&ndash;" + fmtShort(rhi) +
        readoutSuffix() + " for " + selLabel());

    var note = document.getElementById("note"), msg = "";
    if (METRICS[metric].needsCbv && CBV.row_count === 0) {
      msg = "County Base Values not loaded yet — run the prf_adm connector to populate " +
            "the prf_county table.";
    } else if (METRICS[metric].needsOpt && OPT.row_count === 0) {
      msg = "Optimizer sweep not run yet — the prf_opt_best table is empty. Run the sweep " +
            "to populate per-grid best allocations; the map will shade as results land.";
    } else if (METRICS[metric].needsOpt && OPT.mapping_rows === 0) {
      msg = "Sweep results exist for " + OPT.row_count + " grid rows, but the grid-to-county " +
            "mapping (prf_grid_county) is empty, so counties can't be shaded yet.";
    // The shipped state: every commission_pct blank, because the rate is negotiated per
    // agency and published nowhere. Say exactly which file to edit rather than showing an
    // empty map or a plausible-looking zero.
    } else if (metric === "comm" && !COMM.aips.length) {
      msg = "No AIPs listed in " + COMM.path + " — add one row per AIP you write " +
            "(aip_code, aip_name, commission_pct) and reload.";
    // A rate may come from EITHER seed, so only complain when both are empty.
    } else if (metric === "comm" && !anyRateSet()) {
      msg = "No commission rates set — fill in " + COMM.path + " (the commission_pct " +
            "column, a percent of TOTAL premium, e.g. 12.5) or " + CZONE.path +
            " for a regional rate, then reload. Commission is negotiated between you and " +
            "each AIP and is published nowhere, so it cannot be derived; nothing is shaded " +
            "until you enter it.";
    } else if (shaded === 0) {
      msg = "No counties have a value for this selection" + (isFullRange() ? "" : " and range") + ".";
    }
    note.style.display = msg ? "block" : "none";
    note.textContent = msg;
    drawLegend();
    renderGrids();   // drilled-in cells follow the same metric, scale and range as counties
  }

  // A control that feeds a DERIVED metric changes its domain, so those metrics rebuild
  // their scale/slider; the stored metrics only re-shade.
  function isDerived() { return metric === "acre" || metric === "comm"; }
  function onControlChange() {
    if (isDerived()) applyMetric(); else refresh();
  }

  // ---------------- lens: which question the Show list answers
  // BUY holds the four producer metrics. CBV belongs there because it is the producer's
  // dollar protection per acre; it also scales commission, which the Sell tooltip already
  // spells out arithmetically rather than duplicating as a metric.
  var LENS = { buy: ["cbv", "win", "net", "acre"], sell: ["comm"] };
  var lens = "buy";

  function fillMetricSelect() {
    var keys = LENS[lens];
    mSel.innerHTML = keys.map(function (k) {
      return '<option value="' + k + '">' + esc(METRICS[k].legend) + '</option>';
    }).join("");
    if (keys.indexOf(metric) < 0) {   // current metric is not in this lens: take its first
      metric = keys[0];
      mSel.value = metric;
      syncControls();
      applyMetric();
    } else {
      mSel.value = metric;
    }
  }

  document.getElementById("lensSeg").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-lens]");
    if (!b || b.dataset.lens === lens) return;
    lens = b.dataset.lens;
    this.querySelectorAll("button").forEach(function (x) {
      x.classList.toggle("on", x.dataset.lens === lens);
    });
    fillMetricSelect();
  });
  fillMetricSelect();

  // ---------------- wiring
  mSel.addEventListener("change", function () {
    if (metric === mSel.value) return;
    metric = mSel.value;
    syncControls();
    applyMetric();   // domain, slider units + reset, legend, tooltip family, reshade
  });
  fUse.addEventListener("change", function () {
    refillOrganic();
    syncControls();
    onControlChange();
  });
  [fOrganic, fYear, fAip].forEach(function (el) {
    el.addEventListener("change", onControlChange);
  });
  ["input", "change"].forEach(function (evt) {
    fProd.addEventListener(evt, function () { if (isDerived()) applyMetric(); });
  });
  [rMin, rMax].forEach(function (r) {
    r.addEventListener("input", function () { updateRange(); refresh(); });
  });
  document.getElementById("rReset").addEventListener("click", function () {
    rMin.value = 0; rMax.value = UNITS; updateRange(); refresh();
  });

  syncControls();
  applyMetric();     // must precede applyLevel(): it builds the colour scale grids also use
  applyLevel();
  syncZoomUI();
})();
</script>
</body>
</html>
"""
