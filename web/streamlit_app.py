"""PRF Analytics — the web version of the Mac app, behind a passcode.

    cd web && streamlit run streamlit_app.py

Four tabs, the same as the Mac app minus Herd size: County map, Interval analyzer (with the
client-deck builder), All allocations, Old vs new index. Nothing is computed here: every view
comes from prf_app/views.py, the module the Mac app's window is served from, over the same
PRF-only database in ../engine/data. So the web app and the Mac app cannot show different numbers.

The passcode comes from Streamlit secrets (`app_passcode`, or a `[passcodes]` table) or the
APP_PASSCODE environment variable, never from source; with none configured the app refuses to
open (see auth.py, copied verbatim from aip_products).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent          # the PRF_Analytics project
for p in (str(ROOT), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import auth  # noqa: E402
from prf_app import views  # noqa: E402  (puts the engine on sys.path)
from src import prfdeck  # noqa: E402

APP_NAME = "PRF Analytics"
COVERAGES = list(views.COVERAGES)                       # "90%", "85%", ... -> engine keys
st.set_page_config(page_title=APP_NAME, page_icon="🌾", layout="wide")


# --------------------------------------------------------------------------- the gate
def _secrets() -> dict:
    try:
        return {k: st.secrets[k] for k in st.secrets}
    except Exception:          # no secrets file at all
        return {}


def _gate() -> None:
    """Stop here unless this browser session has entered a valid passcode."""
    if st.session_state.get("prf_authed"):
        return
    st.title(APP_NAME)
    allowed = auth.allowed_passcodes(_secrets())
    if not allowed:
        st.error("This app has no passcode configured, so it will not open. Set `app_passcode` in "
                 "the host's secrets (or the APP_PASSCODE environment variable).")
        st.stop()
    st.caption("Restricted access — enter the passcode to continue.")
    with st.form("prf_login"):
        code = st.text_input("Passcode", type="password")
        ok = st.form_submit_button("Enter")
    if ok:
        if auth.verify_passcode(code, allowed):
            st.session_state["prf_authed"] = True
            st.rerun()
        st.error("That passcode is not right.")
    st.stop()


# --------------------------------------------------------------------------- pickers
def _pickers(prefix: str):
    """State -> County -> Grid -> Intended use, from the shipped grid/county table."""
    counties = views.interval_counties()
    if not counties:
        st.info("No PRF grid coverage in this database.")
        return None
    states = sorted({s for s, _, _ in counties})
    c1, c2, c3, c4 = st.columns([1, 2, 1.2, 1.5])
    with c1:
        state = st.selectbox("State", states, index=states.index("WA") if "WA" in states else 0,
                             key=f"{prefix}_state")
    in_state = [(f, n) for s, f, n in counties if s == state]
    with c2:
        name = st.selectbox("County", [n for _, n in in_state], key=f"{prefix}_county")
    fips = next(f for f, n in in_state if n == name)
    grids = views.interval_grids(fips)
    with c3:
        grid = st.selectbox("Grid", grids, key=f"{prefix}_grid") if grids else None
    with c4:
        use = st.selectbox("Intended use", list(views.USES), key=f"{prefix}_use")
    if not grids:
        st.info(f"No PRF grids recorded for {name}, {state}.")
        return None
    return int(grid), fips, use


# --------------------------------------------------------------------------- tabs
def tab_map() -> None:
    st.subheader("County map")
    st.caption("One county map, five views — pick one from **Show** inside the map: County Base "
               "Value, best win rate, best return per $1, best return per acre and commission per acre.")
    st.iframe(views.prf_page_html(), height=860)


def _deck_panel(grid: int, fips: str, use: str) -> None:
    with st.expander("Build a client deck for this grid (PowerPoint or PDF)"):
        st.caption("The agency's three slides: \"PRF Analysis\" and the date with the county below, "
                   "the five most consistent and five best-return allocations, and the closing slide. "
                   "Set the coverage, productivity and window here; the basis is printed on the slide.")
        years = prfdeck.history_years(views.conn(), grid)
        if not years:
            st.info("This grid has no packed interval history.")
            return
        c1, c2, c3, c4, c5 = st.columns(5)
        cov = c1.selectbox("Coverage level", COVERAGES, key="deck_cov")
        pf = c2.number_input("Productivity (%)", min_value=views.PF_MIN, max_value=views.PF_MAX,
                             value=views.PF_DEFAULT, step=5, key="deck_pf")
        end = c3.selectbox("Window ends", prfdeck.end_year_choices(years), key="deck_end")
        backs = prfdeck.lookbacks_for(years, end)
        back = c4.selectbox("Lookback", backs, index=len(backs) - 1, key="deck_back",
                            format_func=lambda n: f"{n} years")
        fmt = {"PowerPoint": "pptx", "PDF": "pdf"}[c5.selectbox("Format", ["PowerPoint", "PDF"], key="deck_fmt")]
        sig = (grid, fips, use, cov, int(pf), int(end), int(back), fmt)
        if st.button("Build deck", key="deck_build"):
            try:
                name, body = views.deck(grid, fips, use, views.COVERAGES[cov], int(pf), int(end),
                                        int(back), fmt, dt.date.today().isoformat())
                st.session_state["deck"] = (sig, name, body)
            except ValueError as exc:
                st.session_state["deck"] = None
                st.error(str(exc))
        built = st.session_state.get("deck")
        if built and built[0] == sig:
            st.download_button(f"Download {built[1]}", built[2], file_name=built[1],
                               mime=prfdeck.FORMATS[fmt], key="deck_download")


def tab_interval() -> None:
    st.subheader("Interval analyzer — per-interval heat map and recommendation")
    st.caption("Every two-month interval scored on its own for one grid, the year-by-year matrix, "
               "and the recommended allocation. **Window ends** and **Lookback** choose the years.")
    pick = _pickers("iv")
    if not pick:
        return
    html = views.interval_page_html(*pick)
    if not html:
        st.info(f"Grid {pick[0]} has no packed interval history in this database.")
        return
    st.iframe(html, height=920)
    _deck_panel(*pick)


def tab_alloc() -> None:
    st.subheader("All allocations — every interval split for one grid, sorted")
    st.caption("Every admissible allocation (2–5 non-adjacent intervals, 5% steps, 10% to the county "
               "cap each, Jan–Feb at most 25% from crop year 2027) with average net return per acre, "
               "win rate and return per $1. **Sort by** any metric; rank #1 is the analyzer's "
               "recommendation. **Save as CSV** writes the optimizer workbook's columns.")
    pick = _pickers("al")
    if not pick:
        return
    html = views.alloc_page_html(*pick)
    if not html:
        st.info(f"Grid {pick[0]} has no packed interval history in this database.")
        return
    st.iframe(html, height=1150)


def tab_vintage() -> None:
    st.subheader("Old vs new rainfall index — how the recommendations moved")
    meta = views.vintage_meta()
    st.caption(
        "RMA replaced the PRF rainfall index for the 2027 crop year. This sets the optimizer's answer "
        f"on the **old data set** ({meta.get('old_label', 'the previous sweep')}) beside its answer on "
        f"the **new data set** ({meta.get('new_label', 'the current sweep')}) for every grid. Both are "
        "priced with the current County Base Value. The old results appear here for comparison only.")
    if not meta:
        st.info("No previous sweep ships in this database.")
        return
    c1, c2, c3, c4 = st.columns([1.4, 1, 1.3, 1.4])
    use = c1.selectbox("Intended use", list(views.USES), key="vt_use")
    cov = c2.selectbox("Coverage level", COVERAGES, key="vt_cov")
    pf = int(c3.number_input("Productivity factor (%)", min_value=views.PF_MIN, max_value=views.PF_MAX,
                             value=views.PF_DEFAULT, step=5, key="vt_pf",
                             help="Only the per-acre figures use it. RMA allows 60–150%."))
    coverage = views.COVERAGES[cov]
    t = views.vintage_table(use, coverage, pf)
    state = c4.selectbox("Table: state", ["All states"] + t["states"], key="vt_state")

    s = t["stats"]
    pct = lambda x: "—" if x is None else f"{x:.0%}"  # noqa: E731
    m = st.columns(5)
    m[0].metric("Grids in both data sets", f"{s['both']:,}",
                help=f"{s['old_only']:,} only in the old data, {s['new_only']:,} only in the new.")
    for col, key, label, fmt, dfmt in (
            (m[1], "win", "Best win rate — median grid", lambda v: f"{v:.1f}%", lambda v: f"{v:+.1f} pts"),
            (m[2], "net", "Best return per \\$1 — median grid", lambda v: f"${v:.3f}", lambda v: f"{v:+.3f}"),
            (m[3], "acre", "Best return per acre — median county", lambda v: f"${v:.2f}", lambda v: f"{v:+.2f}")):
        d = s[key]
        col.metric(label, "—" if d["new_median"] is None else fmt(d["new_median"]),
                   delta=None if d["median_change"] is None else dfmt(d["median_change"]),
                   help=f"Higher on {d['better']:,}, lower on {d['worse']:,}, the same on {d['same']:,}.")
    m[4].metric("Best-return allocation changed", pct(s["net_alloc_changed"]),
                help=f"The best win-rate allocation changed on {pct(s['win_alloc_changed'])}.")

    st.iframe(views.vintage_page_html(use, coverage, pf), height=720)

    cols = t["columns"]
    si = cols.index("State") if "State" in cols else None
    rows = [r for r in t["rows"] if state == "All states" or r[si] == state]
    st.dataframe([dict(zip(cols, r)) for r in rows], hide_index=True, height=460)
    name, body = views.vintage_csv(use, coverage, pf, state)
    st.download_button("Download this comparison (CSV)", body, file_name=name, mime="text/csv",
                       key="vt_csv")


def main() -> None:
    _gate()
    # No sidebar: the data stamp and Log out sit at the top right, as in the Mac app's header.
    left, right = st.columns([3, 1], vertical_alignment="bottom")
    with left:
        st.title(APP_NAME)
        st.caption("Pasture, Rangeland, Forage — rainfall index")
    with right:
        sync = views.meta().get("sync", {})
        if sync.get("source_db_modified"):
            st.caption(f"Data built {sync['source_db_modified'][:10]} · "
                       f"synced {str(sync.get('synced_at', ''))[:10]}")
        if st.button("Log out", key="logout"):
            st.session_state.clear()
            st.rerun()
    tabs = st.tabs(["County map", "Interval analyzer", "All allocations", "Old vs new index"])
    with tabs[0]:
        tab_map()
    with tabs[1]:
        tab_interval()
    with tabs[2]:
        tab_alloc()
    with tabs[3]:
        tab_vintage()


main()
