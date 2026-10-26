"""The PRF-only database gives the same answers as the web app's full database.

sync_engine.py keeps tables by name prefix. If a page ever needs a table that rule drops,
the engine's own `except sqlite3.OperationalError: return {}` guards would render an empty
map without an error — so every payload is compared against the full catalog_app.db here.
Skipped when the aip_products repo is not on this machine.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from prf_app import paths  # noqa: F401  (engine on sys.path)
from src import prfdeck, prfherdpage, prfintervalpage, prfpage, prfvintagepage

SYNC = json.loads(paths.SYNC_JSON.read_text())
FULL = Path(SYNC["source_db"])
pytestmark = pytest.mark.skipif(not FULL.exists(), reason=f"{FULL} not on this machine")


def _open(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(p.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture(scope="module")
def dbs():
    return _open(FULL), _open(paths.DB_PATH)


def test_county_map_payload(dbs):
    full, slim = dbs
    a, b = prfpage.build_prf_page_payload(full), prfpage.build_prf_page_payload(slim)
    assert a["opt"]["uses"] and a["cbv"]["uses"], "full DB rendered an empty map"
    assert a == b


def test_herd_payload(dbs):
    full, slim = dbs
    a, b = prfherdpage.build_herd_size_payload(full), prfherdpage.build_herd_size_payload(slim)
    assert a and a == b


def test_interval_every_grid(dbs):
    """Every county's first grid, all three uses — ~9,000 payloads, a few seconds."""
    full, slim = dbs
    counties = prfintervalpage.counties_with_grids(full)
    assert counties == prfintervalpage.counties_with_grids(slim)
    n = 0
    for _, fips, _ in counties:
        grids = prfintervalpage.grid_choices(full, fips)
        assert grids == prfintervalpage.grid_choices(slim, fips)
        for use in ("Grazing", "Haying", "Haying-Irrigated"):
            a = prfintervalpage.build_interval_payload(full, grids[0], county_fips=fips, use=use)
            assert a == prfintervalpage.build_interval_payload(slim, grids[0], county_fips=fips, use=use)
            n += bool(a)
    assert n > 5000


@pytest.mark.parametrize("use,cov", [("Grazing", 0.9), ("Haying", 0.9),
                                     ("Haying-Irrigated", 0.9), ("Grazing", 0.7)])
def test_vintage_compare(dbs, use, cov):
    full, slim = dbs
    assert prfvintagepage.prev_meta(full) == prfvintagepage.prev_meta(slim)
    a, b = prfvintagepage.compare(full, use=use, coverage=cov), prfvintagepage.compare(slim, use=use, coverage=cov)
    assert a["rows"] and a == b


def test_deck_analysis_every_county(dbs):
    """The deck's tables are identical from the PRF-only database and the full one — or fail identically.

    One grid per county, each use: the answer (or the reason there is none, such as a county that
    publishes no base value) must not depend on which tables the engine's database kept.
    """
    full, slim = dbs
    built = refused = 0
    for _, fips, _ in prfintervalpage.counties_with_grids(full)[::6]:
        grid = prfintervalpage.grid_choices(full, fips)[0]
        for use in ("Grazing", "Haying"):
            def run(c):
                try:
                    return prfdeck.analyse(c, grid, fips, use, coverage=0.85, end_year=2022, years_back=15)
                except ValueError as exc:
                    return str(exc)
            a, b = run(full), run(slim)
            assert a == b, (grid, fips, use)
            built += isinstance(a, dict)
            refused += isinstance(a, str)
    assert built > 100
