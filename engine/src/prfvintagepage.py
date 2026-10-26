"""PRF — old vs new rainfall index: how the optimizer's recommendations moved, grid by grid.

RMA replaced the PRF rainfall index for the 2027 crop year (NOAA CPC -> NOAA NCEI nClimGrid,
contract change date 2026-08-31) and the optimizer was re-swept on the new series. Every
recommendation the app makes -- the county map, the interval analyzer -- reads prf_opt_best,
which is the NEW index only. The sweep it replaced survives in the working DB as
prf_opt_best_pre_ncei, and "what did the switch do to this grid?" is the first question a
producer or agent asks about it.

This module answers that question and is the ONLY reader of the old results:

* pack_prev_vintage() -- run by scripts/build_app_db.py against the working DB, before the old
  table is dropped -- packs the old sweep into prf_opt_prev: integers only (win years out of
  n years, net per $1 x 1e5, each winning allocation as ONE base-166 integer), Grazing stored
  once and every other intended use stored only where it differs from Grazing (or was not
  offered). ~2.5 MB for 70k rows, where the source table would cost the shipped DB ~40 MB.
  It re-reads everything it wrote and refuses to ship a copy that does not reproduce the source.
* compare() -- old vs new for one intended use x coverage level, per grid (for the map) and per
  grid x county (for the table), on the PRF page's three metrics:
      win   best win rate
      net   best average net return per $1 of protection
      acre  best return per acre = net x County Base Value x coverage x productivity factor
* stats(), table_records(), build_vintage_payload(), render_vintage_page_html().

PER ACRE, BOTH VINTAGES ARE PRICED WITH THE CURRENT County Base Value (prf_county ships the
current year only), so a per-acre change is the index-and-rate change alone, never a CBV
revision. Each county reads the row swept under its own actuarial cap, exactly as
src/prfpage.py does; the grid map, which has one cell per grid, uses the grid's lowest cap (the
conservative one, matching prfsweep.grid_caps) and the average CBV of the counties it touches.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from collections import defaultdict

from src.prfpage import _cov_key, _fips5, _parse_list, use_key

INTERVALS = ("JAN-FEB", "FEB-MAR", "MAR-APR", "APR-MAY", "MAY-JUN", "JUN-JUL",
             "JUL-AUG", "AUG-SEP", "SEP-OCT", "OCT-NOV", "NOV-DEC")
_BASE = 166        # one digit per interval: index x 15 + pct/5, plus 1 so a digit is never 0
_E5 = 100_000      # net per $1 is stored to 5 decimals, the precision every PRF view displays at
PROVIDER_NAMES = {"cpc": "NOAA CPC", "ncei": "NOAA NCEI nClimGrid"}
SOURCE_TABLE = "prf_opt_best_pre_ncei"

PREV_DDL = """
DROP TABLE IF EXISTS prf_opt_prev;
DROP TABLE IF EXISTS prf_opt_prev_meta;
-- The optimizer's results on the PREVIOUS rainfall-index vintage, packed from the working DB's
-- prf_opt_best_pre_ncei by src/prfvintagepage.pack_prev_vintage(). Read ONLY by the PRF
-- old-vs-new comparison section; no recommendation reads it.
--   * intended_use: every grid carries its Grazing row; another use has a row only where its
--     result differs from Grazing (offered=1) or where that use was not offered (offered=0,
--     every metric NULL). No row for a non-Grazing use means "identical to Grazing".
--   * win_years / net_win_years: the win rate x n_years (a whole number of years, exactly).
--   * *_e5: average net return per $1 of protection x 100,000.
--   * *_alloc: the winning allocation as one integer (see encode_alloc()).
CREATE TABLE prf_opt_prev (
    grid_id       INTEGER NOT NULL,
    intended_use  TEXT    NOT NULL,
    coverage_pct  INTEGER NOT NULL,
    max_pct       INTEGER NOT NULL,
    offered       INTEGER NOT NULL,
    n_years       INTEGER,
    win_years     INTEGER,
    win_net_e5    INTEGER,
    win_alloc     INTEGER,
    net_e5        INTEGER,
    net_win_years INTEGER,
    net_alloc     INTEGER,
    PRIMARY KEY (grid_id, intended_use, coverage_pct, max_pct)
) WITHOUT ROWID;
CREATE TABLE prf_opt_prev_meta (key TEXT PRIMARY KEY, value TEXT);
"""


# --------------------------------------------------------------------------- allocation codec
def encode_alloc(combo, props) -> int | None:
    """One integer for an interval allocation; None when there is no allocation.

    Each interval becomes one base-166 digit, `interval_index x 15 + percent/5 + 1`, most
    significant first. PRF percentages are 5% steps no larger than the 70% cap, so a digit never
    exceeds 165, and the +1 keeps any digit from being 0 -- which is what makes the decode
    unambiguous without storing a length. Five intervals fit in 37 bits.
    """
    combo, props = list(combo or []), list(props or [])
    if not combo:
        return None
    if len(combo) != len(props):
        raise ValueError(f"{len(combo)} intervals but {len(props)} percentages")
    value = 0
    for interval, pct in zip(combo, props):
        if interval not in INTERVALS:
            raise ValueError(f"unknown interval {interval!r}")
        p = float(pct)
        if p != int(p) or int(p) % 5 or not 5 <= int(p) <= 70:
            raise ValueError(f"{pct}% is not a 5% step between 5 and 70")
        value = value * _BASE + INTERVALS.index(interval) * 15 + int(p) // 5 + 1
    return value


def decode_alloc(value) -> tuple[list[str], list[int]] | None:
    """Inverse of encode_alloc(): ([interval codes], [percentages]), or None."""
    if value is None:
        return None
    v = int(value)
    if v <= 0:
        raise ValueError(f"{value} is not an encoded allocation")
    intervals, pcts = [], []
    while v:
        digit = v % _BASE
        if digit == 0:
            raise ValueError(f"{value} is not an encoded allocation")
        digit -= 1
        intervals.append(INTERVALS[digit // 15])
        pcts.append((digit % 15) * 5)
        v //= _BASE
    return intervals[::-1], pcts[::-1]


def alloc_text(value) -> str:
    """'FEB-MAR 20% · APR-MAY 40% · …' for an encoded allocation; '' for none."""
    a = decode_alloc(value)
    return " · ".join(f"{iv} {p}%" for iv, p in zip(*a)) if a else ""


def vintage_label(sources) -> str:
    """'CY2026 · NOAA CPC index' from sweep source stamps such as 'prfbulk_2026'.

    The provider follows the crop year exactly as src/prfbulk.PROVIDER_BY_CROP_YEAR does
    (2027 and later: NCEI; earlier: CPC) -- repeated rather than imported, so the app does not
    load the bulk loader and its network stack just to print a label."""
    years = sorted({int(m.group(1)) for s in sources or () if s
                    for m in [re.search(r"(20\d\d)", str(s))] if m})
    if not years:
        return "earlier sweep"
    crop_year = years[-1]
    provider = "ncei" if crop_year >= 2027 else "cpc"
    return f"CY{crop_year} · {PROVIDER_NAMES[provider]} index"


# --------------------------------------------------------------------------- packing (build time)
_SRC_COLS = ("grid_id, intended_use, coverage_level, max_pct, year_min, year_max, best_win_rate, "
             "best_win_combo, best_win_props, best_win_avg_net, best_net, best_net_combo, "
             "best_net_props, best_net_win_rate, source, fetched_at")


def _whole_years(rate, n_years, what, key):
    if rate is None or not n_years:
        return None
    k = round(rate * n_years)
    if abs(k / n_years - rate) > 1e-9:
        raise ValueError(f"{key}: {what} {rate} is not a whole number of years out of {n_years}")
    return k


def _e5(x):
    return None if x is None else round(x * _E5)


def _pack_row(r, key) -> tuple:
    n_years = (r[5] - r[4] + 1) if r[4] is not None and r[5] is not None else None
    win_combo, win_props = _parse_list(r[7]), _parse_list(r[8])
    net_combo, net_props = _parse_list(r[11]), _parse_list(r[12])
    wa, na = encode_alloc(win_combo, win_props), encode_alloc(net_combo, net_props)
    # the codec is exact or it is useless -- check it on every row rather than trust it
    for enc, combo, props in ((wa, win_combo, win_props), (na, net_combo, net_props)):
        if enc is not None and decode_alloc(enc) != (list(combo), [int(p) for p in props]):
            raise ValueError(f"{key}: allocation {combo} {props} does not survive encoding")
    return (n_years, _whole_years(r[6], n_years, "best_win_rate", key), _e5(r[9]), wa,
            _e5(r[10]), _whole_years(r[13], n_years, "best_net_win_rate", key), na)


def _resolve(stored: dict, uses: set, grid, use, cov_pct, cap):
    """The packed row a reader sees for one key: the use's own row, else Grazing's."""
    if use not in uses:
        return None                        # never alias a use the old sweep never had
    row = stored.get((grid, use, cov_pct, cap))
    if row is None and use != "Grazing":
        row = stored.get((grid, "Grazing", cov_pct, cap))
    return row


def pack_prev_vintage(conn: sqlite3.Connection, src: str = SOURCE_TABLE,
                      current: str = "prf_opt_best") -> dict:
    """Pack the previous sweep into prf_opt_prev (+ prf_opt_prev_meta). Returns a summary.

    Skips (and says so) when `src` is absent. Raises rather than ship a lossy copy: every
    source row is re-resolved through the reader's own aliasing rule and must match."""
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if src not in have:
        return {"skipped": f"{src} is not in this database"}

    packed: dict = {}
    sources, swept, year_min, year_max = set(), "", None, None
    for r in conn.execute(f"SELECT {_SRC_COLS} FROM {src}"):
        key = (int(r[0]), str(r[1]), int(round(float(r[2]) * 100)), int(r[3]))
        packed[key] = _pack_row(r, key)
        sources.add(r[14])
        swept = max(swept, r[15] or "")
        if r[4] is not None:
            year_min = r[4] if year_min is None else min(year_min, r[4])
        if r[5] is not None:
            year_max = r[5] if year_max is None else max(year_max, r[5])

    uses = {k[1] for k in packed}
    rows = []
    for key, p in packed.items():
        if key[1] == "Grazing" or packed.get((key[0], "Grazing", key[2], key[3])) != p:
            rows.append(key + (1,) + p)
    for use in sorted(uses - {"Grazing"}):
        for key in packed:
            if key[1] == "Grazing" and (key[0], use, key[2], key[3]) not in packed:
                # NOT OFFERED is stored explicitly: without it the reader would alias this use to
                # Grazing and show a result for a policy nobody could buy.
                rows.append((key[0], use, key[2], key[3], 0) + (None,) * 7)

    stored = {r[:4]: r[4:] for r in rows}
    for key, p in packed.items():
        if _resolve(stored, uses, *key) != (1,) + p:
            raise ValueError(f"REFUSING: packed prf_opt_prev does not reproduce {src} at {key}")

    current_sources = set()
    if current in have:
        try:
            current_sources = {r[0] for r in conn.execute(f"SELECT DISTINCT source FROM {current}")}
        except sqlite3.OperationalError:
            pass
    meta = {
        "old_label": vintage_label(sources),
        "new_label": vintage_label(current_sources) if any(current_sources) else "current sweep",
        "old_source": ", ".join(sorted(s for s in sources if s)),
        "old_swept_through": swept[:10],
        "years": f"{year_min}–{year_max}" if year_min is not None else "",
        "uses": json.dumps(sorted(uses)),
        "source_rows": str(len(packed)),
        "packed_rows": str(len(rows)),
        "grids": str(len({k[0] for k in packed})),
        "packed_on": _dt.date.today().isoformat(),
    }
    conn.executescript(PREV_DDL)
    conn.executemany("INSERT INTO prf_opt_prev VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO prf_opt_prev_meta VALUES (?,?)", meta.items())
    conn.commit()
    return {"source_rows": len(packed), "packed_rows": len(rows), "grids": int(meta["grids"]),
            "old_label": meta["old_label"], "new_label": meta["new_label"]}


# --------------------------------------------------------------------------- comparison (read time)
def prev_meta(conn: sqlite3.Connection) -> dict | None:
    """prf_opt_prev_meta as a dict, or None when no previous sweep ships in this DB."""
    try:
        rows = conn.execute("SELECT key, value FROM prf_opt_prev_meta").fetchall()
    except sqlite3.OperationalError:
        return None
    return {r[0]: r[1] for r in rows} or None


def county_cbv(conn: sqlite3.Connection, use: str) -> dict[str, float]:
    """County FIPS -> the newest Conventional County Base Value for this intended use."""
    out: dict[str, float] = {}
    newest: dict[str, int] = {}
    try:
        rows = conn.execute(
            "SELECT year, county_fips, intended_use, irrigation_practice, county_base_value "
            "FROM prf_county WHERE organic_practice = 'Conventional' "
            "AND county_base_value IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return out
    for year, fips, intended_use, irrigation, value in rows:
        if use_key(intended_use, irrigation) != use:
            continue
        f = _fips5(fips)
        if f and int(year) >= newest.get(f, -1):
            newest[f] = int(year)
            out[f] = float(value)
    return out


def _from_prev(row) -> dict | None:
    if row is None or not row[0]:
        return None
    _, n_years, win_years, win_net, win_alloc, net, net_win_years, net_alloc = row
    rate = (lambda k: None if k is None or not n_years else round(k / n_years, 4))
    e5 = (lambda x: None if x is None else x / _E5)
    return {"win": rate(win_years), "win_net": e5(win_net), "wa": win_alloc,
            "net": e5(net), "net_win": rate(net_win_years), "na": net_alloc}


def _safe_encode(combo_text, props_text):
    try:
        return encode_alloc(_parse_list(combo_text), _parse_list(props_text))
    except ValueError:
        return None


def _r(x, nd):
    return None if x is None else round(float(x), nd)


def compare(conn: sqlite3.Connection, use: str = "Grazing", coverage=0.9) -> dict:
    """Old vs new optimizer results for one intended use x coverage level. Pure read.

    Returns grid-level parallel arrays for the map (`grids`), one record per grid x county for
    the table (`rows`), the previous sweep's metadata and availability counts."""
    meta = prev_meta(conn) or {}
    cov_pct = int(round(float(coverage) * 100))

    grid_counties: dict[int, list[str]] = defaultdict(list)
    names: dict[str, tuple[str, str]] = {}
    try:
        for grid_id, state, fips, name in conn.execute(
                "SELECT grid_id, state, county_fips, county_name FROM prf_grid_county"):
            f = _fips5(fips)
            if f:
                grid_counties[int(grid_id)].append(f)
                names[f] = (str(state or ""), str(name or f).strip())
    except sqlite3.OperationalError:
        pass
    caps: dict[str, int] = {}
    try:
        for fips, cap in conn.execute("SELECT state_code || county_code, MIN(max_pct) "
                                      "FROM prf_max_pct GROUP BY state_code, county_code"):
            caps[str(fips)] = int(cap)
    except sqlite3.OperationalError:
        pass
    cbv = county_cbv(conn, use)

    old: dict[tuple[int, int], dict] = {}
    if meta:
        uses_old = set(json.loads(meta.get("uses") or "[]"))
        stored = {}
        for r in conn.execute(
                "SELECT grid_id, intended_use, coverage_pct, max_pct, offered, n_years, win_years, "
                "win_net_e5, win_alloc, net_e5, net_win_years, net_alloc FROM prf_opt_prev "
                "WHERE coverage_pct = ? AND intended_use IN (?, 'Grazing')", (cov_pct, use)):
            stored[(int(r[0]), r[1], int(r[2]), int(r[3]))] = tuple(r[4:])
        for grid_id, _, _, cap in {(k[0], None, None, k[3]) for k in stored}:
            rec = _from_prev(_resolve(stored, uses_old, grid_id, use, cov_pct, cap))
            if rec:
                old[(grid_id, cap)] = rec

    new: dict[tuple[int, int], dict] = {}
    try:
        for r in conn.execute(
                "SELECT grid_id, max_pct, best_win_rate, best_win_combo, best_win_props, "
                "best_win_avg_net, best_net, best_net_combo, best_net_props, best_net_win_rate "
                "FROM prf_opt_best WHERE intended_use = ? AND abs(coverage_level - ?) < 1e-6",
                (use, float(coverage))):
            new[(int(r[0]), int(r[1]))] = {
                "win": _r(r[2], 4), "win_net": _r(r[5], 5), "wa": _safe_encode(r[3], r[4]),
                "net": _r(r[6], 5), "net_win": _r(r[9], 4), "na": _safe_encode(r[7], r[8])}
    except sqlite3.OperationalError:
        pass

    caps_in_data: dict[int, set] = defaultdict(set)
    for g, cap in list(old) + list(new):
        caps_in_data[g].add(cap)

    def grid_cap(g: int) -> int | None:
        county_caps = sorted({caps[f] for f in grid_counties.get(g, []) if f in caps})
        pool = county_caps or sorted(caps_in_data.get(g, ()))
        return pool[0] if pool else None

    arrays = {k: [] for k in ("g", "ow", "nw", "on", "nn", "own", "nwn", "onw", "nnw",
                              "owa", "nwa", "ona", "nna", "cbv", "cty")}
    rows = []
    for g in sorted(caps_in_data):
        cap = grid_cap(g)
        o, n = old.get((g, cap)), new.get((g, cap))
        fl = grid_counties.get(g, [])
        vals = [cbv[f] for f in fl if f in cbv]
        arrays["g"].append(g)
        for side, rec in (("o", o), ("n", n)):
            arrays[side + "w"].append(rec["win"] if rec else None)
            arrays[side + "n"].append(rec["net"] if rec else None)
            arrays[side + "wn"].append(rec["win_net"] if rec else None)
            arrays[side + "nw"].append(rec["net_win"] if rec else None)
            arrays[side + "wa"].append(rec["wa"] if rec else None)
            arrays[side + "na"].append(rec["na"] if rec else None)
        arrays["cbv"].append(round(sum(vals) / len(vals), 2) if vals else None)
        if fl:
            st, nm = names.get(fl[0], ("", fl[0]))
            arrays["cty"].append(f"{nm}, {st}" + (f" +{len(fl) - 1} more" if len(fl) > 1 else ""))
        else:
            arrays["cty"].append("no county mapping in the current grid roster")
        for f in fl or [None]:
            county_cap = caps.get(f, cap) if f else cap
            st, nm = names.get(f, ("", "")) if f else ("", "")
            rows.append({"state": st, "county": nm, "fips": f or "", "grid": g, "cap": county_cap,
                         "cbv": cbv.get(f) if f else None,
                         "old": old.get((g, county_cap)), "new": new.get((g, county_cap))})

    return {"use": use, "coverage": _cov_key(coverage), "cov_pct": cov_pct, "meta": meta,
            "available": bool(meta), "grids": arrays, "rows": rows,
            "old_grids": len({g for g, _ in old}), "new_grids": len({g for g, _ in new})}


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _summary(pairs):
    diffs = [b - a for a, b in pairs]
    return {"old_median": _median([a for a, _ in pairs]), "new_median": _median([b for _, b in pairs]),
            "median_change": _median(diffs), "better": sum(1 for d in diffs if d > 1e-9),
            "worse": sum(1 for d in diffs if d < -1e-9),
            "same": sum(1 for d in diffs if abs(d) <= 1e-9), "n": len(diffs)}


def stats(result: dict, pf_pct: float = 100) -> dict:
    """Headline numbers: medians, better/worse counts and how often the recommendation moved.

    Win rate and return per $1 are counted per GRID; return per acre per grid x COUNTY, because
    that is the grain a County Base Value exists at."""
    G = result["grids"]
    idx = range(len(G["g"]))
    both = [i for i in idx if (G["ow"][i] is not None or G["on"][i] is not None)
            and (G["nw"][i] is not None or G["nn"][i] is not None)]
    out = {
        "grids": len(G["g"]), "both": len(both),
        "old_only": sum(1 for i in idx if (G["ow"][i] is not None or G["on"][i] is not None) and i not in set(both)),
        "new_only": sum(1 for i in idx if (G["nw"][i] is not None or G["nn"][i] is not None) and i not in set(both)),
        "win": _summary([(G["ow"][i] * 100, G["nw"][i] * 100) for i in both
                         if G["ow"][i] is not None and G["nw"][i] is not None]),
        "net": _summary([(G["on"][i], G["nn"][i]) for i in both
                         if G["on"][i] is not None and G["nn"][i] is not None]),
    }
    scale = result["cov_pct"] / 100 * float(pf_pct) / 100
    out["acre"] = _summary([(r["old"]["net"] * r["cbv"] * scale, r["new"]["net"] * r["cbv"] * scale)
                            for r in result["rows"]
                            if r["old"] and r["new"] and r["cbv"] is not None
                            and r["old"]["net"] is not None and r["new"]["net"] is not None])
    moved = lambda a, b: [i for i in both if G[a][i] is not None and G[b][i] is not None]
    wa, na = moved("owa", "nwa"), moved("ona", "nna")
    out["win_alloc_changed"] = (sum(1 for i in wa if G["owa"][i] != G["nwa"][i]) / len(wa)) if wa else None
    out["net_alloc_changed"] = (sum(1 for i in na if G["ona"][i] != G["nna"][i]) / len(na)) if na else None
    return out


def table_records(result: dict, pf_pct: float = 100) -> list[dict]:
    """One flat record per grid x county, the three metrics old / new / change. Pure."""
    scale = result["cov_pct"] / 100 * float(pf_pct) / 100
    out = []
    for r in result["rows"]:
        o, n, cbv = r["old"], r["new"], r["cbv"]

        def pick(rec, k):
            return None if not rec or rec.get(k) is None else rec[k]

        def acre(rec):
            v = pick(rec, "net")
            return None if v is None or cbv is None else round(v * cbv * scale, 2)

        def change(a, b, nd):
            return None if a is None or b is None else round(b - a, nd)

        ow = None if pick(o, "win") is None else round(o["win"] * 100, 1)
        nw = None if pick(n, "win") is None else round(n["win"] * 100, 1)
        on, nn = pick(o, "net"), pick(n, "net")
        oa, na = acre(o), acre(n)
        status = ("" if o and n else "only in the old data" if o
                  else "only in the new data" if n else "not offered in either")
        moved = (lambda k: "" if not o or not n or o.get(k) is None or n.get(k) is None
                 else ("changed" if o[k] != n[k] else "same"))
        out.append({
            "State": r["state"], "County": r["county"], "FIPS": r["fips"], "Grid": r["grid"],
            "Cap %": r["cap"], "CBV $/ac": cbv,
            "Win rate old %": ow, "Win rate new %": nw, "Win rate change (pts)": change(ow, nw, 1),
            "Return per $1 old": on, "Return per $1 new": nn, "Return per $1 change": change(on, nn, 5),
            "Return per acre old $": oa, "Return per acre new $": na,
            "Return per acre change $": change(oa, na, 2),
            "Best-win allocation old": alloc_text(pick(o, "wa")),
            "Best-win allocation new": alloc_text(pick(n, "wa")),
            "Best-win allocation": moved("wa"),
            "Best-return allocation old": alloc_text(pick(o, "na")),
            "Best-return allocation new": alloc_text(pick(n, "na")),
            "Best-return allocation": moved("na"),
            "Status": status,
        })
    return out


# --------------------------------------------------------------------------- page
def build_vintage_payload(result: dict, pf_pct: float = 100) -> dict:
    """What the grid map embeds: the grid arrays plus labels. Productivity is applied in-page."""
    return {"generated": _dt.date.today().isoformat(), "use": result["use"],
            "coverage": result["coverage"], "pf": float(pf_pct), "meta": result["meta"],
            **result["grids"]}


def _js_embed_json(obj) -> str:
    """JSON serialized for safe inline embedding inside a <script> block."""
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def render_vintage_page_html(payload: dict, d3_js: str, topojson_js: str, atlas: dict) -> str:
    """Self-contained national grid map: change / old / new, for each of the three metrics."""
    for blob in (d3_js, topojson_js):
        if "</script" in blob.lower():
            raise ValueError("asset JS contains '</script'; cannot inline safely")
    html = _TEMPLATE
    html = html.replace("__D3__", d3_js)
    html = html.replace("__TOPOJSON__", topojson_js)
    html = html.replace("__ATLAS__", _js_embed_json(atlas))
    html = html.replace("__PAYLOAD__", _js_embed_json(payload))
    return html


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRF — old vs new rainfall index</title>
<style>
  :root { color-scheme: light; --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink-2:#52514e;
          --muted:#898781; --grid:#e1e0d9; --baseline:#c3c2b7; }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--page);
         color: var(--ink); font-size: 14px; display: flex; flex-direction: column; }
  header { padding: 10px 16px 6px; }
  header h1 { font-size: 15px; font-weight: 650; }
  header .sub { color: var(--ink-2); font-size: 12.5px; margin-top: 2px; }
  .filters { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; padding: 7px 16px 9px;
             border-bottom: 1px solid var(--grid); background: var(--surface); }
  .filters label { font-size: 12px; color: var(--ink-2); display: inline-flex; gap: 6px; align-items: center; }
  .seg { display: inline-flex; border: 1px solid var(--baseline); border-radius: 7px; overflow: hidden; }
  .seg button { font: inherit; font-size: 12.5px; border: none; border-right: 1px solid var(--grid);
                background: var(--surface); color: var(--ink); padding: 4px 10px; cursor: pointer; }
  .seg button:last-child { border-right: none; }
  .seg button.on { background: #238b45; color: #fff; font-weight: 650; }
  .count { margin-left: auto; font-size: 12px; color: var(--ink-2); }
  #wrap { position: relative; flex: 1; min-height: 0; }
  svg { width: 100%; height: 100%; display: block; }
  .nation { fill: #efeee9; }
  .cell:hover { stroke: #0b0b0b; stroke-width: 1px; vector-effect: non-scaling-stroke; }
  .states { fill: none; stroke: #fff; stroke-width: 0.9px; vector-effect: non-scaling-stroke; pointer-events: none; }
  .legend { position: absolute; left: 12px; bottom: 12px; background: var(--surface); border: 1px solid rgba(11,11,11,.1);
            border-radius: 8px; padding: 8px 10px; font-size: 11.5px; box-shadow: 0 1px 4px rgba(11,11,11,.06); }
  .legend .t { font-weight: 600; color: var(--ink-2); margin-bottom: 5px; }
  .legend .row { display: flex; }
  .legend .c { width: 26px; height: 10px; }
  .legend .lb { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--muted); margin-top: 2px; }
  .legend .k { display: flex; align-items: center; gap: 6px; margin-top: 5px; }
  .legend .sw { width: 14px; height: 10px; display: inline-block; }
  .tip { position: absolute; pointer-events: none; background: #fff; border: 1px solid rgba(11,11,11,.14); border-radius: 8px;
         padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 14px rgba(11,11,11,.12); max-width: 420px; display: none; line-height: 1.45; }
  .tip table { border-collapse: collapse; margin: 5px 0 4px; }
  .tip td { padding: 1px 10px 1px 0; white-space: nowrap; }
  .tip .al { margin-top: 5px; }
  .m { color: var(--muted); }
  .up { color: #542788; } .dn { color: #b35806; }
  .chip { font-family: ui-monospace, Menlo, monospace; font-size: 10.5px; background: #eef6ee; border: 1px solid #cfe3cf;
          border-radius: 4px; padding: 0 4px; margin-right: 2px; white-space: nowrap; }
</style>
</head>
<body>
<header><h1 id="title"></h1><div class="sub" id="sub"></div></header>
<div class="filters">
  <label>Metric <span class="seg" id="mSeg"><button data-m="win" class="on">Best win rate</button><button data-m="net">Best return per $1</button><button data-m="acre">Best return per acre</button></span></label>
  <label>Show <span class="seg" id="vSeg"><button data-v="diff" class="on">Change (new − old)</button><button data-v="old">Old data</button><button data-v="new">New data</button></span></label>
  <span class="count" id="count"></span>
</div>
<div id="wrap">
  <svg id="map" viewBox="0 0 975 610" preserveAspectRatio="xMidYMid meet"></svg>
  <div class="legend" id="legend"></div>
  <div class="tip" id="tip"></div>
</div>
<script>__D3__</script>
<script>__TOPOJSON__</script>
<script>
(function () {
  var US_ATLAS = __ATLAS__;
  var P = __PAYLOAD__;
  var IV = ["JAN-FEB","FEB-MAR","MAR-APR","APR-MAY","MAY-JUN","JUN-JUL","JUL-AUG","AUG-SEP","SEP-OCT","OCT-NOV","NOV-DEC"];
  // Mirror of src/prfvintagepage.decode_alloc: one base-166 digit per interval.
  function dec(v) {
    if (v === null || v === undefined) return null;
    var ivs = [], ps = [];
    while (v > 0) { var d = v % 166; if (!d) return null; d -= 1;
      ivs.unshift(IV[Math.floor(d / 15)]); ps.unshift((d % 15) * 5); v = Math.floor(v / 166); }
    return [ivs, ps];
  }
  function allocHtml(v) {
    var a = dec(v);
    return a ? a[0].map(function (iv, k) { return "<span class='chip'>" + iv + " " + a[1][k] + "%</span>"; }).join("")
             : "<span class='m'>—</span>";
  }
  var N = P.g.length, COV = parseFloat(P.coverage), PF = P.pf / 100;
  var META = P.meta || {}, OLD = META.old_label || "Old data", NEW = META.new_label || "New data";
  var metric = "win", view = "diff";
  function money(v, nd) { return (v < 0 ? "−$" : "$") + Math.abs(v).toFixed(nd); }
  function signed(v, nd, pre, post) { return (v >= 0 ? "+" : "−") + pre + Math.abs(v).toFixed(nd) + post; }
  var M = {
    win:  { name: "Best win rate", legend: "Best win rate (%)",
            fmt: function (v) { return v.toFixed(1) + "%"; }, dfmt: function (v) { return signed(v, 1, "", " pts"); } },
    net:  { name: "Best return per $1", legend: "Best return per $1 of protection",
            fmt: function (v) { return money(v, 3); }, dfmt: function (v) { return signed(v, 3, "$", ""); } },
    acre: { name: "Best return per acre", legend: "Best return per acre ($)",
            fmt: function (v) { return money(v, 2); }, dfmt: function (v) { return signed(v, 2, "$", ""); } }
  };
  function mval(side, m, i) {
    if (m === "win") { var w = P[side + "w"][i]; return w === null ? null : w * 100; }
    var net = P[side + "n"][i]; if (net === null) return null;
    if (m === "net") return net;
    var c = P.cbv[i]; return c === null ? null : net * c * COV * PF;
  }
  function shown(i) {
    var o = mval("o", metric, i), n = mval("n", metric, i);
    if (view === "old") return o; if (view === "new") return n;
    return (o === null || n === null) ? null : n - o;
  }
  // Diverging for change (orange = the new data scores it lower, purple = higher); one green ramp
  // shared by Old and New so the two views are directly comparable.
  var DIV = ["#b35806","#e08214","#fdb863","#fee0b6","#f7f7f7","#d8daeb","#b2abd2","#8073ac","#542788"];
  var SEQ = ["#e5f5e0","#c7e9c0","#a1d99b","#74c476","#41ab5d","#238b45","#005a32"];
  var NONE = "#d4d3cd";
  var lo = 0, hi = 1, span = 1;
  function domain() {
    var i;
    if (view === "diff") {
      var a = []; for (i = 0; i < N; i++) { var v = shown(i); if (v !== null) a.push(Math.abs(v)); }
      a.sort(d3.ascending);
      span = a.length ? (d3.quantileSorted(a, 0.95) || a[a.length - 1]) : 1;
      if (!(span > 0)) span = 1;
    } else {
      var b = []; for (i = 0; i < N; i++) { var x = mval("o", metric, i), y = mval("n", metric, i);
        if (x !== null) b.push(x); if (y !== null) b.push(y); }
      lo = b.length ? d3.min(b) : 0; hi = b.length ? d3.max(b) : 1; if (!(hi > lo)) hi = lo + 1;
    }
  }
  function color(v) {
    if (v === null) return NONE;
    if (view === "diff") {
      if (Math.abs(v) < 1e-9) return DIV[4];
      var k = Math.floor((v + span) / (2 * span) * DIV.length);
      k = Math.max(0, Math.min(DIV.length - 1, k));
      if (k === 4) k = v > 0 ? 5 : 3;           // any real change shows its direction
      return DIV[k];
    }
    var s = Math.floor((v - lo) / (hi - lo) * SEQ.length);
    return SEQ[Math.max(0, Math.min(SEQ.length - 1, s))];
  }

  var proj = d3.geoAlbersUsa().scale(1300).translate([487.5, 305]), path = d3.geoPath(proj);
  var svg = d3.select("#map"), g = svg.append("g");
  g.append("path").attr("class", "nation").attr("d", path(topojson.feature(US_ATLAS, US_ATLAS.objects.nation)));
  // Cell corners from grid_id arithmetic -- mirror of src/prfgrid.py (300 per row, 0.25 degrees,
  // origin 130W 20N). Projected corner by corner, so no spherical ring winding is involved.
  function cellPath(id) {
    var k = id - 1, row = Math.floor(k / 300), col = k - row * 300, w = -130 + col * 0.25, s = 20 + row * 0.25;
    var pts = [[w, s], [w, s + 0.25], [w + 0.25, s + 0.25], [w + 0.25, s]].map(function (c) { return proj(c); });
    for (var j = 0; j < 4; j++) if (!pts[j]) return null;
    return "M" + pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join("L") + "Z";
  }
  var tipEl = document.getElementById("tip"), wrap = document.getElementById("wrap");
  var cells = g.append("g").selectAll("path").data(d3.range(N)).join("path")
      .attr("class", "cell").attr("d", function (i) { return cellPath(P.g[i]); })
      .on("mousemove", function (ev, i) { showTip(ev, i); })
      .on("mouseout", function () { tipEl.style.display = "none"; });
  g.append("path").attr("class", "states")
      .attr("d", path(topojson.mesh(US_ATLAS, US_ATLAS.objects.states, function (a, b) { return a !== b; })));
  svg.call(d3.zoom().scaleExtent([1, 30]).on("zoom", function (ev) { g.attr("transform", ev.transform); }));

  function row(m, i) {
    var o = mval("o", m, i), n = mval("n", m, i), d = (o === null || n === null) ? null : n - o;
    var cls = d === null || Math.abs(d) < 1e-9 ? "m" : (d > 0 ? "up" : "dn");
    return "<tr" + (m === metric ? " style='font-weight:650'" : "") + "><td>" + M[m].name + "</td><td>" +
      (o === null ? "—" : M[m].fmt(o)) + "</td><td>" + (n === null ? "—" : M[m].fmt(n)) + "</td><td class='" + cls + "'>" +
      (d === null ? "" : Math.abs(d) < 1e-9 ? "same" : M[m].dfmt(d)) + "</td></tr>";
  }
  function allocBlock(title, a, b, oStat, nStat) {
    var moved = (a === null || b === null) ? "" : (a === b ? " <span class='m'>unchanged</span>" : " <b class='dn'>changed</b>");
    return "<div class='al'><b>" + title + "</b>" + moved + "<br><span class='m'>old</span> " + allocHtml(a) +
      (oStat ? " <span class='m'>" + oStat + "</span>" : "") + "<br><span class='m'>new</span> " + allocHtml(b) +
      (nStat ? " <span class='m'>" + nStat + "</span>" : "") + "</div>";
  }
  function showTip(ev, i) {
    var hasO = P.ow[i] !== null || P.on[i] !== null, hasN = P.nw[i] !== null || P.nn[i] !== null;
    var note = !hasO ? "<div class='m'>No result in the old data for this use and coverage.</div>"
             : !hasN ? "<div class='m'>No result in the new data (grid retired from the roster, or this use is not offered).</div>" : "";
    tipEl.innerHTML = "<b>Grid " + P.g[i] + "</b> <span class='m'>" + P.cty[i] + "</span>" + note +
      "<table><tr class='m'><td></td><td>" + OLD + "</td><td>" + NEW + "</td><td>change</td></tr>" +
      row("win", i) + row("net", i) + row("acre", i) + "</table>" +
      (P.cbv[i] !== null ? "<div class='m'>Per acre: net × $" + P.cbv[i].toFixed(2) + " County Base Value (average of the counties it touches) × " +
        Math.round(COV * 100) + "% × " + P.pf + "% productivity</div>" : "<div class='m'>No County Base Value for this use, so no per-acre figure.</div>") +
      allocBlock("Best win-rate allocation", P.owa[i], P.nwa[i],
                 P.own[i] !== null ? "net " + money(P.own[i], 3) : "", P.nwn[i] !== null ? "net " + money(P.nwn[i], 3) : "") +
      allocBlock("Best return allocation", P.ona[i], P.nna[i],
                 P.onw[i] !== null ? "wins " + (P.onw[i] * 100).toFixed(1) + "%" : "", P.nnw[i] !== null ? "wins " + (P.nnw[i] * 100).toFixed(1) + "%" : "");
    tipEl.style.display = "block";
    var r = wrap.getBoundingClientRect(), x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
    var w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    if (x + w > r.width - 6) x = Math.max(6, ev.clientX - r.left - w - 14);
    if (y + h > r.height - 6) y = Math.max(6, r.height - h - 6);
    tipEl.style.left = x + "px"; tipEl.style.top = y + "px";
  }

  function legend() {
    var L = document.getElementById("legend"), sw = function (c) { return "<div class='c' style='background:" + c + "'></div>"; };
    if (view === "diff") {
      L.innerHTML = "<div class='t'>" + M[metric].name + " — change, new minus old</div><div class='row'>" + DIV.map(sw).join("") + "</div>" +
        "<div class='lb'><span>" + M[metric].dfmt(-span) + " or lower</span><span>no change</span><span>" + M[metric].dfmt(span) + " or higher</span></div>" +
        "<div class='k'><span class='sw' style='background:" + NONE + "'></span>not in both data sets</div>";
    } else {
      L.innerHTML = "<div class='t'>" + M[metric].legend + " — " + (view === "old" ? OLD : NEW) + "</div><div class='row'>" + SEQ.map(sw).join("") + "</div>" +
        "<div class='lb'><span>" + M[metric].fmt(lo) + "</span><span>" + M[metric].fmt(hi) + "</span></div>" +
        "<div class='k'><span class='sw' style='background:" + NONE + "'></span>no result</div>" +
        "<div class='m' style='margin-top:4px;font-size:10.5px'>Old and new share this scale.</div>";
    }
  }
  function recolor() {
    domain();
    var better = 0, worse = 0, same = 0, have = 0;
    cells.attr("fill", function (i) { var v = shown(i);
      if (v !== null) { have++; if (view === "diff") { if (v > 1e-9) better++; else if (v < -1e-9) worse++; else same++; } }
      return color(v); });
    document.getElementById("count").textContent = view === "diff"
      ? have.toLocaleString() + " grids in both · " + better.toLocaleString() + " higher · " + worse.toLocaleString() + " lower · " + same.toLocaleString() + " unchanged"
      : have.toLocaleString() + " grids with a result";
    document.getElementById("title").textContent = "Old vs new rainfall index — " + M[metric].name + " · " + P.use + " · " + Math.round(COV * 100) + "% coverage";
    document.getElementById("sub").textContent = OLD + " → " + NEW + (META.years ? " · years " + META.years : "") +
      (metric === "acre" ? " · " + P.pf + "% productivity · both priced with the current County Base Value" : "") + " · scroll to zoom, hover a grid for the detail";
    legend();
  }
  function seg(id, attr, set) {
    document.querySelectorAll("#" + id + " button").forEach(function (b) {
      b.onclick = function () { document.querySelectorAll("#" + id + " button").forEach(function (x) { x.classList.toggle("on", x === b); });
        set(b.getAttribute(attr)); recolor(); };
    });
  }
  seg("mSeg", "data-m", function (v) { metric = v; });
  seg("vSeg", "data-v", function (v) { view = v; });
  recolor();
})();
</script>
</body>
</html>
"""
