"""The loopback server: routes, input checks, the Host guard, the CSV and the client deck the Save buttons write."""
from __future__ import annotations

import csv
import http.client
import io
import json
import re
import zipfile

import pytest

from prf_app import server, views


@pytest.fixture(scope="module")
def port():
    srv = server.start()
    yield srv.server_port
    srv.shutdown()


def get(port, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    c.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    r = c.getresponse()
    return r.status, r.read(), r


def test_foreign_host_refused(port):
    status, _, _ = get(port, "/api/meta", host="evil.example:80")
    assert status == 403


def test_unknown_route(port):
    assert get(port, "/nope")[0] == 404


@pytest.mark.parametrize("path", [
    "/api/vintage?use=Pasture&cov=0.9&pf=100",
    "/api/vintage?use=Grazing&cov=0.95&pf=100",
    "/api/vintage?use=Grazing&cov=0.9&pf=200",
    "/api/grids?county=abc",
])
def test_bad_input_is_400(port, path):
    assert get(port, path)[0] == 400


def test_interval_missing_grid_is_a_note(port):
    status, body, _ = get(port, "/page/interval?grid=1&county=53047&use=Grazing")
    assert status == 200 and b"no packed interval history" in body


def test_csv_matches_table(port):
    status, body, r = get(port, "/api/vintage.csv?use=Grazing&cov=0.9&pf=100&state=WA")
    assert status == 200
    assert 'filename="prf_old_vs_new_Grazing_90pct_WA.csv"' in r.getheader("Content-Disposition")
    rows = list(csv.reader(io.StringIO(body.decode())))
    t = views.vintage_table("Grazing", "0.9", 100)
    assert rows[0] == t["columns"]
    wa = [r for r in t["rows"] if r[0] == "WA"]
    assert len(rows) - 1 == len(wa) > 0
    assert all(r[0] == "WA" for r in rows[1:])


def test_pf_moves_only_per_acre(port):
    a = json.loads(get(port, "/api/vintage?use=Grazing&cov=0.9&pf=100")[1])
    b = json.loads(get(port, "/api/vintage?use=Grazing&cov=0.9&pf=150")[1])
    assert a["stats"]["win"] == b["stats"]["win"] and a["stats"]["net"] == b["stats"]["net"]
    assert a["stats"]["acre"]["new_median"] != b["stats"]["acre"]["new_median"]


# ------------------------------------------------------------------ client deck

def _deckable():
    """(grid, county) for the first grid that can have a deck — some counties publish no base value."""
    for _, fips, _ in views.interval_counties()[:80]:
        for grid in views.interval_grids(fips)[:1]:
            try:
                views.deck(grid, fips, "Grazing", "0.9", 100, None, 20, "pptx", "2026-10-13")
            except ValueError:
                continue
            return grid, fips
    pytest.skip("no grid in this database can produce a deck")


def test_deck_is_a_valid_three_slide_deck_for_the_chosen_grid(port):
    grid, fips = _deckable()
    status, body, r = get(port, f"/api/deck.pptx?grid={grid}&county={fips}&use=Grazing&cov=0.9"
                                "&pf=100&end=2020&len=15")
    assert status == 200
    assert r.getheader("Content-Type").endswith("presentationml.presentation")
    assert "filename=\"PRF_Deck_" in r.getheader("Content-Disposition")
    z = zipfile.ZipFile(io.BytesIO(body))
    assert z.testzip() is None
    slides = [n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
    assert len(slides) == 3
    text = z.read("ppt/slides/slide2.xml").decode()
    assert f"{grid} " in text and "15 Year Net Return" in text and "2006–2020" in text
    front = re.findall(r"<a:t>([^<]*)</a:t>", z.read("ppt/slides/slide1.xml").decode())
    assert len(front) == 2 and front[0].startswith("PRF Analysis ") and front[1].endswith((" County", " Parish"))
    assert not any("{{" in z.read(n).decode() for n in slides)


@pytest.mark.parametrize("query,why", [
    ("grid=1&county=53047&use=Grazing", "a grid with no history"),
    ("grid={g}&county={c}&use=Grazing&cov=0.95", "a coverage level that does not exist"),
    ("grid={g}&county={c}&use=Grazing&len=7", "a lookback that is not 5/10/15/20"),
    ("grid={g}&county={c}&use=Grazing&pf=200", "a productivity factor out of range"),
    ("grid={g}&county={c}&use=Grazing&end=1850", "a year that is not a year"),
    ("grid={g}&county={c}&use=Grazing&end=1999", "a window that cannot end there"),
    ("grid={g}&county={c}&use=Grazing&end=2002&len=20", "a lookback longer than the history"),
    ("grid={g}&county={c}&use=Pasture", "an unknown use"),
])
def test_deck_bad_requests_are_400_not_500(port, query, why):
    grid, fips = _deckable()
    assert get(port, "/api/deck.pptx?" + query.format(g=grid, c=fips))[0] == 400, why


def test_deck_can_be_saved_as_pdf_or_powerpoint(port):
    grid, fips = _deckable()
    base = f"/api/deck?grid={grid}&county={fips}&use=Grazing&cov=0.85&pf=110&end=2022&len=10"
    status, pdf, r = get(port, base + "&fmt=pdf")
    assert status == 200 and r.getheader("Content-Type") == "application/pdf"
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert 'filename="PRF_Deck_' in r.getheader("Content-Disposition")
    assert r.getheader("Content-Disposition").endswith('.pdf"')
    status, ppt, r = get(port, base + "&fmt=pptx")
    assert status == 200 and ppt[:2] == b"PK" and r.getheader("Content-Disposition").endswith('.pptx"')
    assert get(port, base)[2].getheader("Content-Type").endswith("presentationml.presentation")  # default
    assert get(port, base + "&fmt=docx")[0] == 400


def test_the_old_route_name_still_works(port):
    grid, fips = _deckable()
    assert get(port, f"/api/deck.pptx?grid={grid}&county={fips}&use=Grazing")[0] == 200


# ------------------------------------------------------------------ all allocations

def test_allocations_page_renders_for_a_real_grid(port):
    counties = views.interval_counties()
    fips = next(f for s, f, n in counties if s == "WA")
    grid = views.interval_grids(fips)[0]
    status, body, _ = get(port, f"/page/allocations?grid={grid}&county={fips}&use=Grazing")
    assert status == 200 and b"saveCsv" in body and b"__PAYLOAD__" not in body
    assert b"compositionsForCombo" in body          # the analyzer's own policy rules, Jan-Feb limit included


def test_allocations_missing_grid_is_a_note_and_bad_input_is_400(port):
    status, body, _ = get(port, "/page/allocations?grid=1&county=53047&use=Grazing")
    assert status == 200 and b"no packed interval history" in body
    assert get(port, "/page/allocations?grid=1&county=abc&use=Grazing")[0] == 400


@pytest.mark.parametrize("name,ok", [("PRF_allocations_1_X.csv", True), ("a.txt", False),
                                     ("../../evil.csv", False), (".hidden.csv", False), ("", False)])
def test_csv_names_are_plain_csv_file_names(name, ok):
    if ok:
        got = views.check_csv_name(name)
        assert got.endswith(".csv") and "/" not in got
    else:
        with pytest.raises(ValueError):
            views.check_csv_name(name)
