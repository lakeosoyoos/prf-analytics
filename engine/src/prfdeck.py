"""A client deck for ONE PRF grid, built from the same analysis the interval analyzer shows.

THE DECK is the agency's own three-slide design (modelled on its hand-made PRF decks):

  1. title   -- "PRF Analysis <date>" on one line, the county below it
  2. one table slide, "<grid> <County>, <ST> <Use>": the five most CONSISTENT interval
     allocations (highest success rate, ties broken by return) and the five best RETURN
     allocations (highest net return per acre, ties broken by success rate), each with the
     intervals it uses and the percentage on each
  3. closing slide

The look is not re-created here. data/assets/prf_deck_template.pptx is that deck with its
grid-specific text swapped for {{TOKENS}} (scripts/make_prf_deck_template.py), and this module
fills the tokens, so fonts, colours, images and positions are exactly the hand-made deck's. No
PowerPoint library is needed -- a .pptx is a zip of XML and only text changes -- which keeps the
offline Mac app free of a dependency it would otherwise have to bundle.

THE NUMBERS are not a second analysis. Inputs come from prfintervalpage.build_interval_payload,
the same packed history, rates, subsidy, county cap and county base value the page draws; the
window is the page's own (where it ENDS, and 5/10/15/20 years back from there); the search is
the same one the page and the sweep run (2-5 non-adjacent intervals, 5% steps, 10%..cap each,
summing to 100, and JAN-FEB no more than its own lower cap where RMA sets one) and the ranking rules are prfopt.rank_by_win_rate / rank_by_avg_net_return,
tie-breaks included -- tests/test_prfdeck.py checks the policy set, the per-year arithmetic and the
ranking against prfopt itself. What the columns mean:

  Success Rate        share of window years the allocation's net return was above zero
  Net Return Per Acre average net return per $1 of protection x protection per acre
                      (County Base Value x coverage x productivity) -- one year's worth, on average
  Intervals/Alloc.    the allocation, intervals in month order

TWO LIMITS worth knowing. The index is stored one byte a cell (src/prfintervals), so where two
allocations are nearly tied on success rate the pick can differ from an exact calculation by about
a year in twenty; return per $1 almost never does. And the premium rates are the current rate
year's applied to every year in the window: "what would today's price have paid".
"""
from __future__ import annotations

import datetime as _dt
import functools
import heapq
import io
import itertools
import re
import sqlite3
import zipfile
from operator import itemgetter, mul
from pathlib import Path
from typing import Sequence
from xml.sax.saxutils import escape

from . import config, prfdeckpdf
from .prfintervalpage import build_interval_payload
from .prfintervals import COVERAGES, USES
from .prfopt import INTERVALS, MAX_INTERVALS, MIN_INTERVALS, MIN_PCT, STEP_PCT

TEMPLATE = config.ROOT / "data" / "assets" / "prf_deck_template.pptx"
TOP_N = 5
FORMATS = {"pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
           "pdf": "application/pdf"}
PRODUCTIVITY_MIN, PRODUCTIVITY_MAX = 0.60, 1.50
LOOKBACKS = (5, 10, 15, 20)

USE_LABELS = {"Grazing": "Grazing", "Haying": "Haying", "Haying-Irrigated": "Irrigated Haying"}
PROVIDER_LABELS = {"ncei": "NOAA NCEI", "cpc": "NOAA CPC"}
# Louisiana is the one state whose counties are parishes.
_PARISH_STATE_FIPS = "22"


# --------------------------------------------------------------------------- the search

def _compositions(total: int, parts: int, lo: int, hi: int):
    """All ordered tuples of `parts` ints in [lo, hi] summing to `total` (prfopt's, verbatim)."""
    if parts == 1:
        if lo <= total <= hi:
            yield (total,)
        return
    lo_rest, hi_rest = lo * (parts - 1), hi * (parts - 1)
    for first in range(max(lo, total - hi_rest), min(hi, total - lo_rest) + 1):
        for rest in _compositions(total - first, parts - 1, lo, hi):
            yield (first,) + rest


@functools.lru_cache(maxsize=8)
def policy_space(usable: tuple[int, ...], cap: int, janfeb_cap: int | None = None) -> tuple:
    """Every admissible (interval-index combo, whole-percent allocation, fractions) policy.

    The same set, in the same order, as prfopt.enumerate_policies restricted to `usable`
    intervals: month-ordered subsets of MIN..MAX_INTERVALS with no two adjacent, each
    allocation a multiple of STEP_PCT in [MIN_PCT, cap] summing to 100. The fractions are
    stored beside the percentages (p / 100.0, as prfopt.score_policies computes them) so the
    scoring loop does no division. `janfeb_cap` is the separate, lower limit on JAN-FEB (RMA
    PM-26-053: 25% from crop year 2027), None where it does not apply -- prfopt's rule exactly.
    """
    ok = set(usable)
    lo, hi, units = MIN_PCT // STEP_PCT, cap // STEP_PCT, 100 // STEP_PCT
    alloc: dict[int, list] = {}
    out = []
    for k in range(MIN_INTERVALS, MAX_INTERVALS + 1):
        for ii in itertools.combinations(range(len(INTERVALS)), k):
            if not all(b - a >= 2 for a, b in zip(ii, ii[1:])) or not ok.issuperset(ii):
                continue
            if k not in alloc:
                alloc[k] = [tuple(u * STEP_PCT for u in c) for c in _compositions(units, k, lo, hi)]
            jf = ii.index(0) if (janfeb_cap is not None and ii[0] == 0) else None
            for props in alloc[k]:
                if jf is not None and props[jf] > janfeb_cap:
                    continue
                out.append((ii, props, tuple(p / 100.0 for p in props)))
    return tuple(out)


def interval_year_nets(index: Sequence[float], rate: float, subsidy: float,
                       coverage: float) -> list[float]:
    """Net return per $1 of protection, one interval, one value per year.

    prfopt.interval_year_nets(round_cents=False, protection=1.0) written out, including its
    base-100 index and trigger, so the floats are identical and ties resolve the way the sweep's do.
    """
    trigger = coverage * 100.0
    net_prem = rate * 1.0 * (1.0 - subsidy)
    return [(max(0.0, (trigger - v * 100.0) / trigger) * 1.0) - net_prem for v in index]


def rank_policies(nets: Sequence[Sequence[float] | None], cap: int, top: int = TOP_N,
                  janfeb_cap: int | None = None):
    """The `top` most consistent and `top` best-return policies.

    `nets[i]` is interval i's per-year net per $1, or None when the interval cannot be used
    (not offered for this use, unrated, or missing a year in the window). Returns
    (by_win, by_net), each a list of dicts {idx, props, win, avg}: win = share of years with
    net > 0, avg = mean net per $1. Ranking is prfopt.rank_by_win_rate (win, then avg) and
    rank_by_avg_net_return (avg, then win), both descending, ties left in enumeration order.
    """
    usable = tuple(i for i, s in enumerate(nets) if s is not None)
    policies = policy_space(usable, cap, janfeb_cap)
    if not policies:
        return [], []
    n_years = len(nets[usable[0]])
    wins: list[int] = []
    avgs: list[float] = []
    for ii, group in itertools.groupby(policies, key=itemgetter(0)):
        per_year = list(zip(*(nets[i] for i in ii)))
        for _, _props, ps in group:
            total, w = 0.0, 0
            for yv in per_year:
                net = sum(map(mul, ps, yv))
                total += net
                if net > 0:
                    w += 1
            wins.append(w)
            avgs.append(total / n_years)

    idxs = range(len(policies))
    # nsmallest(key=...) is equivalent to sorted(key=...)[:n], so ties keep enumeration order.
    by_win = heapq.nsmallest(top, idxs, key=lambda i: (-wins[i], -avgs[i]))
    by_net = heapq.nsmallest(top, idxs, key=lambda i: (-avgs[i], -wins[i]))

    def row(i: int) -> dict:
        return {"idx": policies[i][0], "props": policies[i][1],
                "win": wins[i] / n_years, "avg": avgs[i]}
    return [row(i) for i in by_win], [row(i) for i in by_net]


# --------------------------------------------------------------------------- the analysis

def analyse(conn: sqlite3.Connection, grid_id: int, county_fips: str | None, use: str, *,
            coverage: float = 0.90, productivity: float = 1.0,
            end_year: int | None = None, years_back: int = 20) -> dict:
    """Everything the table slide shows, as plain values. Raises ValueError, with a message
    that says what to change, when the grid/county/use cannot produce a deck."""
    if use not in USES:
        raise ValueError(f"unknown intended use {use!r}")
    cov_key = f"{coverage:g}"
    if round(coverage, 2) not in COVERAGES:
        raise ValueError("coverage level must be one of "
                         + ", ".join(f"{int(c * 100)}%" for c in COVERAGES))
    if not PRODUCTIVITY_MIN <= productivity <= PRODUCTIVITY_MAX:
        raise ValueError(f"productivity factor must be {PRODUCTIVITY_MIN:.2f}-{PRODUCTIVITY_MAX:.2f}")

    pl = build_interval_payload(conn, int(grid_id), county_fips=county_fips, use=use)
    if not pl:
        raise ValueError(f"grid {grid_id} has no packed interval history in this database")
    # A client deck on the retired index is advice about numbers that no longer settle anything.
    if pl.get("index_provider") and pl.get("current_provider") \
            and pl["index_provider"] != pl["current_provider"]:
        raise ValueError(
            f"this database holds the retired NOAA {pl['index_provider'].upper()} index; RMA "
            f"now settles on NOAA {pl['current_provider'].upper()}. "
            "Reload the current index before building a deck.")
    if pl["cbv"] is None:
        raise ValueError(f"{pl.get('county_name') or 'this county'} publishes no County Base "
                         f"Value for {USE_LABELS[use]}, and the table is in dollars per acre")
    sub = (pl["subsidy"] or {}).get(cov_key)
    if sub is None:
        raise ValueError(f"no premium subsidy on file for {int(coverage * 100)}% coverage")

    years = pl["years"]
    end = years[-1] if end_year is None else int(end_year)
    if end not in years:
        raise ValueError(f"the window cannot end in {end}; history covers {years[0]}-{years[-1]}")
    hi = years.index(end)
    lo = hi - int(years_back) + 1
    if years_back < 1 or lo < 0:
        raise ValueError(f"a {years_back}-year lookback ending {end} would start before "
                         f"{years[0]}, where the history begins")
    window = years[lo:hi + 1]

    offered = set(pl["offered"].get(use, []))
    nets: list[list[float] | None] = []
    for iv in INTERVALS:
        rate = pl["rates"].get(iv, {}).get(cov_key)
        series = pl["index"].get(iv, [])[lo:hi + 1]
        if iv not in offered or rate is None or len(series) != len(window) \
                or any(v is None for v in series):
            nets.append(None)
        else:
            nets.append(interval_year_nets(series, rate, sub, coverage))
    if sum(s is not None for s in nets) < MIN_INTERVALS:
        raise ValueError(f"fewer than {MIN_INTERVALS} rated {USE_LABELS[use]} intervals on "
                         f"grid {grid_id}, so no allocation can be built")

    cap = int(pl["cap"] or 60)
    janfeb = int(pl["janfeb_cap"]) if pl.get("janfeb_cap") is not None else None
    by_win, by_net = rank_policies(nets, cap, janfeb_cap=janfeb)
    protection = pl["cbv"] * coverage * productivity

    def fmt(r: dict) -> dict:
        return {"win": r["win"], "net_per_1": r["avg"], "net_per_acre": r["avg"] * protection,
                "intervals": [INTERVALS[i] for i in r["idx"]], "props": list(r["props"])}
    return {
        "grid_id": int(grid_id), "county_fips": pl["county_fips"], "county": pl["county_name"],
        "state": pl["state"], "use": use, "coverage": coverage, "productivity": productivity,
        "cbv": pl["cbv"], "protection": protection, "cap": cap, "janfeb_cap": janfeb,
        "rate_year": pl["rate_year"],
        "window": (window[0], window[-1]), "years": len(window),
        "index_provider": pl.get("index_provider"), "index_source": pl.get("index_source"),
        "consistent": [fmt(r) for r in by_win], "return": [fmt(r) for r in by_net],
    }


# --------------------------------------------------------------------------- window choices

def history_years(conn: sqlite3.Connection, grid_id: int) -> list[int]:
    """The years a grid's packed history covers (empty when it has none)."""
    row = conn.execute("SELECT year_min, year_max FROM prf_interval_history WHERE grid_id = ?",
                       (int(grid_id),)).fetchone()
    return list(range(int(row[0]), int(row[1]) + 1)) if row else []


def end_year_choices(years: Sequence[int]) -> list[int]:
    """Years a window may end in, newest first: any that leaves room for the shortest lookback."""
    return list(reversed(list(years)[LOOKBACKS[0] - 1:]))


def lookbacks_for(years: Sequence[int], end_year: int) -> list[int]:
    """5/10/15/20-year lookbacks that fit before `end_year` (the page's own rule)."""
    n = list(years).index(end_year) + 1 if end_year in years else 0
    return [b for b in LOOKBACKS if b <= n] or ([n] if n else [])


# --------------------------------------------------------------------------- formatting

def _pct(x: float) -> str:
    return f"{round(x * 100):d}%"


def _money(x: float) -> str:
    r = round(x, 2)
    return f"-${abs(r):,.2f}" if r < 0 else f"${r:,.2f}"


def _title_size(text: str) -> int:
    """36 pt for a title that fits the (widened) title box on one line; smaller for long ones."""
    return 3600 if len(text) <= 40 else max(2000, int(3600 * 40 / len(text)))


def _intervals_text(names: Sequence[str]) -> str:
    """Intervals as the table prints them. Up to three fit the column on one line, as in the
    hand-made deck; four or five always wrap, and left to PowerPoint the wrap lands on a hyphen
    ("JUN-" / "JUL"), so break after the second interval instead ("\n" -> a line break)."""
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:2]) + ",\n" + ", ".join(names[2:])


FRONT_BOX_PT = 10500000 / 12700        # the front-page text box, in points
FRONT_SIZE_PT, FRONT_FLOOR_PT = 45.0, 28.0


def front_size(lines: Sequence[str]) -> float:
    """Point size for the two front-page lines: the template's 45 pt, or smaller when the
    longest line would not fit the box on one line (a long county name), never below 28."""
    widest = max(prfdeckpdf._width(s, True, FRONT_SIZE_PT) for s in lines)
    return FRONT_SIZE_PT if widest <= FRONT_BOX_PT else max(
        FRONT_FLOOR_PT, FRONT_SIZE_PT * FRONT_BOX_PT / widest)


def _area(a: dict) -> str:
    word = "Parish" if (a["county_fips"] or "")[:2] == _PARISH_STATE_FIPS else "County"
    return f"{a['county']} {word}" if a["county"] else f"Grid {a['grid_id']}"


def _basis(a: dict) -> str:
    provider = PROVIDER_LABELS.get(a["index_provider"] or "", "RMA")
    return (f"{round(a['coverage'] * 100)}% coverage · productivity {a['productivity']:.2f} · "
            f"County Base Value ${a['cbv']:.2f}/acre · {provider} index {a['window'][0]}–"
            f"{a['window'][1]} · CY{a['rate_year']} rates · {a['cap']}% maximum per interval"
            + (f" (Jan–Feb {a['janfeb_cap']}%)" if a.get("janfeb_cap") else ""))


def deck_values(a: dict, *, today: _dt.date) -> dict[str, str]:
    """Token -> text for the template, from analyse()'s result."""
    title = " ".join(x for x in (
        str(a["grid_id"]), f"{a['county']}, {a['state']}" if a["county"] else (a["state"] or ""),
        USE_LABELS[a["use"]]) if x)
    date = f"{today.month}/{today.day}/{today:%y}"
    front = front_size([f"PRF Analysis {date}", _area(a)])
    vals = {
        "DATE": date,
        "AREA": _area(a),
        "FRONT_SZ": str(int(front * 100)),
        "FRONT_LS": str(int((front + 1) * 100)),
        "CROP_YEAR": str(a["rate_year"]),
        "GRID_TITLE": title,
        "TITLE_SZ": str(_title_size(title)),
        "YEARS": str(a["years"]),
        "BASIS": _basis(a),
    }
    for prefix, rows in (("C", a["consistent"]), ("R", a["return"])):
        for n in range(1, TOP_N + 1):
            r = rows[n - 1] if n <= len(rows) else None
            vals[f"{prefix}{n}_WIN"] = _pct(r["win"]) if r else "—"
            vals[f"{prefix}{n}_NET"] = _money(r["net_per_acre"]) if r else "—"
            vals[f"{prefix}{n}_IV"] = _intervals_text(r["intervals"]) if r else "—"
            vals[f"{prefix}{n}_AL"] = ", ".join(str(p) for p in r["props"]) if r else "—"
    return vals


# A table cell whose text is one token, with the run properties captured so a line break can
# be written in the same style.
_IV_RUN = re.compile(r"<a:r>(<a:rPr\b[^>]*>(?:(?!</a:rPr>).)*</a:rPr>)<a:t>\{\{(\w+)\}\}</a:t></a:r>", re.S)


def _break_lines(xml: str, values: dict[str, str]) -> str:
    """Turn a value containing "\n" into runs separated by <a:br>, keeping the run style."""
    def repl(m: re.Match) -> str:
        rpr, token = m.group(1), m.group(2)
        text = values.get(token, "")
        if "\n" not in text:
            return m.group(0)
        br_rpr = rpr.replace("<a:rPr", "<a:rPr", 1)
        runs = [f"<a:r>{rpr}<a:t>{escape(line)}</a:t></a:r>" for line in text.split("\n")]
        return f"<a:br>{br_rpr}</a:br>".join(runs)
    return _IV_RUN.sub(repl, xml)


def _fill(xml: str, values: dict[str, str], name: str) -> str:
    xml = _break_lines(xml, values)
    for token, text in values.items():
        xml = xml.replace("{{" + token + "}}", escape(text))
    left = re.findall(r"\{\{\w+\}\}", xml)
    if left:
        raise ValueError(f"{name}: template fields not filled: {sorted(set(left))}")
    return xml


def fill_template(values: dict[str, str] | Sequence[dict[str, str]],
                  template: Path | None = None) -> bytes:
    """The template with every {{TOKEN}} replaced. Refuses to return a deck with one left.

    `values` is one token -> text mapping for a one-grid deck, or a list of them for a deck of
    several grids: slide 1 takes the title fields from the first, and the table slide is repeated
    once per mapping (the hand-made 11/18 deck does the same), ahead of the closing slide.
    """
    decks = [values] if isinstance(values, dict) else list(values)
    if not decks:
        raise ValueError("a deck needs at least one grid")
    n = len(decks)
    src = Path(template or TEMPLATE)
    if not src.exists():
        raise FileNotFoundError(f"deck template missing: {src}")
    slide_ct = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
    slide_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
    # slide k (1-based) lives in slideK.xml: 1 title, 2..n+1 tables, n+2 closing
    rids = ["rId8"] + [f"rId{100 + k}" for k in range(1, n + 2)]
    buf = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        rels2 = zin.read("ppt/slides/_rels/slide2.xml.rels")
        rels3 = zin.read("ppt/slides/_rels/slide3.xml.rels")
        for info in zin.infolist():
            name, data = info.filename, zin.read(info.filename)
            if name == "ppt/slides/slide1.xml":
                data = _fill(data.decode("utf-8"), decks[0], name).encode("utf-8")
            elif name == "ppt/slides/slide2.xml":
                for k, v in enumerate(decks):
                    part = f"ppt/slides/slide{2 + k}.xml"
                    zout.writestr(part, _fill(data.decode("utf-8"), v, part).encode("utf-8"))
                    zout.writestr(f"ppt/slides/_rels/slide{2 + k}.xml.rels", rels2)
                continue
            elif name == "ppt/slides/slide3.xml":
                zout.writestr(f"ppt/slides/slide{n + 2}.xml", _fill(data.decode("utf-8"), decks[0], name).encode("utf-8"))
                zout.writestr(f"ppt/slides/_rels/slide{n + 2}.xml.rels", rels3)
                continue
            elif name in ("ppt/slides/_rels/slide2.xml.rels", "ppt/slides/_rels/slide3.xml.rels"):
                continue
            elif name == "ppt/presentation.xml":
                ids = "".join(f'<p:sldId id="{256 + i}" r:id="{rid}"/>' for i, rid in enumerate(rids))
                data = re.sub(r"<p:sldIdLst>.*?</p:sldIdLst>", f"<p:sldIdLst>{ids}</p:sldIdLst>",
                              data.decode("utf-8"), flags=re.S).encode("utf-8")
            elif name == "ppt/_rels/presentation.xml.rels":
                x = data.decode("utf-8")
                x = re.sub(r'<Relationship Id="rId(?:9|10)"[^>]*slides/slide[23]\.xml"/>', "", x)
                new = "".join(f'<Relationship Id="{rids[k]}" Type="{slide_rel}" Target="slides/slide{k + 1}.xml"/>'
                              for k in range(1, n + 2))
                data = x.replace("</Relationships>", new + "</Relationships>").encode("utf-8")
            elif name == "[Content_Types].xml":
                x = data.decode("utf-8")
                x = re.sub(r'<Override PartName="/ppt/slides/slide[23]\.xml"[^>]*/>', "", x)
                new = "".join(f'<Override PartName="/ppt/slides/slide{k}.xml" ContentType="{slide_ct}"/>'
                              for k in range(2, n + 3))
                data = x.replace("</Types>", new + "</Types>").encode("utf-8")
            zout.writestr(name, data)
    return buf.getvalue()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def _areas(analyses: Sequence[dict]) -> str:
    """The title slide's second line: one county, or every county named, in list order."""
    names = list(dict.fromkeys(a["county"] for a in analyses if a["county"]))
    if len(names) <= 1:
        return _area(analyses[0])
    return ", ".join(names[:-1]) + " & " + names[-1] + " Counties"


def build_deck_multi(conn: sqlite3.Connection, specs: Sequence[tuple], *, coverage: float = 0.90,
                     productivity: float = 1.0, end_year: int | None = None, years_back: int = 20,
                     today: _dt.date | None = None, fmt: str = "pptx",
                     label: str | None = None) -> tuple[str, bytes]:
    """One deck for several grids: a title slide, one table slide per (grid_id, county_fips, use)
    in `specs`, and the closing slide -- as PowerPoint ("pptx") or PDF ("pdf"). `label` (a client
    name, say) goes in the file name. Every grid is analysed on the same coverage, productivity
    and window; a grid that cannot produce a table stops the whole deck with its reason."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if not specs:
        raise ValueError("a deck needs at least one grid")
    today = today or _dt.date.today()
    analyses = []
    for grid_id, county_fips, use in specs:
        try:
            analyses.append(analyse(conn, grid_id, county_fips, use, coverage=coverage,
                                    productivity=productivity, end_year=end_year,
                                    years_back=years_back))
        except ValueError as exc:
            raise ValueError(f"grid {grid_id}: {exc}") from exc
    decks = [deck_values(a, today=today) for a in analyses]
    area = _areas(analyses)
    date = decks[0]["DATE"]
    front = front_size([f"PRF Analysis {date}", area])
    for d in decks:                     # the title slide is built from the first; keep them equal
        d["AREA"], d["FRONT_SZ"], d["FRONT_LS"] = area, str(int(front * 100)), str(int((front + 1) * 100))
    if fmt == "pdf":
        body = prfdeckpdf.render(decks)
    else:
        body = fill_template(decks)
    pct = f"{round(coverage * 100)}pct"
    if len(analyses) == 1 and not label:
        a = analyses[0]
        parts = (a["grid_id"], a["county"] or "", a["state"] or "", USE_LABELS[a["use"]], pct)
    else:
        parts = (label or "Multi", f"{len(analyses)}_grids", pct)
    name = "PRF_Deck_" + "_".join(_slug(str(x)) for x in parts if x) + f"_{today.isoformat()}.{fmt}"
    return name, body


def build_deck(conn: sqlite3.Connection, grid_id: int, county_fips: str | None, use: str, *,
               coverage: float = 0.90, productivity: float = 1.0, end_year: int | None = None,
               years_back: int = 20, today: _dt.date | None = None,
               fmt: str = "pptx") -> tuple[str, bytes]:
    """(file name, bytes) for one grid, as a PowerPoint deck ("pptx") or a PDF ("pdf") of the
    same three slides. See the module docstring; several grids: build_deck_multi."""
    return build_deck_multi(conn, [(grid_id, county_fips, use)], coverage=coverage,
                            productivity=productivity, end_year=end_year, years_back=years_back,
                            today=today, fmt=fmt)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Write a PRF deck for one grid.")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--grid", type=int, required=True)
    ap.add_argument("--county", help="county FIPS (default: the grid's lowest)")
    ap.add_argument("--use", default="Grazing", choices=USES)
    ap.add_argument("--coverage", type=float, default=0.90)
    ap.add_argument("--productivity", type=float, default=1.0)
    ap.add_argument("--end-year", type=int)
    ap.add_argument("--years", type=int, default=20, choices=LOOKBACKS)
    ap.add_argument("--format", default="pptx", choices=tuple(FORMATS))
    ap.add_argument("--out", help="output path (default: the generated file name, here)")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    name, body = build_deck(conn, args.grid, args.county, args.use, coverage=args.coverage,
                            productivity=args.productivity, end_year=args.end_year,
                            years_back=args.years, fmt=args.format)
    out = Path(args.out or name)
    out.write_bytes(body)
    print(f"wrote {out} ({len(body) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
