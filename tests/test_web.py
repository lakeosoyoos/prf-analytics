"""The web app (web/streamlit_app.py): the passcode gate holds, and behind it every tab renders.

Skipped where Streamlit is not installed (the Mac app's build environment does not need it);
run with the web environment:  ~/.prf_web_venv/bin/python -m pytest tests/test_web.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "web" / "streamlit_app.py")
PASS = "test-passcode-for-apptest"


def _app(monkeypatch, passcode=PASS):
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    at = st_testing.AppTest.from_file(APP, default_timeout=180)
    if passcode:
        at.secrets["app_passcode"] = passcode
    return at


def _inside(monkeypatch):
    """Already past the gate (the login itself is tested on its own below)."""
    at = _app(monkeypatch)
    at.session_state["prf_authed"] = True
    return at.run()


def _login(at, code):
    at.text_input[0].input(code)
    at.button[0].click()
    return at.run()


def test_no_passcode_configured_means_the_app_does_not_open(monkeypatch):
    at = _app(monkeypatch, passcode=None).run()
    assert any("no passcode configured" in e.value for e in at.error)
    assert not at.tabs


def test_a_wrong_passcode_is_refused(monkeypatch):
    at = _login(_app(monkeypatch).run(), "not-it")
    assert any("not right" in e.value for e in at.error)
    assert not at.tabs


def test_the_right_passcode_opens_the_app(monkeypatch):
    at = _login(_app(monkeypatch).run(), PASS)
    assert not at.exception and [t.label for t in at.tabs][:1] == ["County Map"]


def test_behind_the_gate_every_tab_renders_and_herd_size_is_gone(monkeypatch):
    at = _inside(monkeypatch)
    assert not at.exception, [e.message for e in at.exception]
    labels = [t.label for t in at.tabs]
    assert labels == ["County Map", "Interval Analyzer", "All Allocations", "Old vs New Index"]
    assert "Herd size" not in labels
    assert not at.sidebar.children, "the app has no left column"
    assert any(b.label == "Log out" for b in at.button)
    # the deck builder and the comparison download are present
    assert any(b.label == "Build client deck" for b in at.button)
    # every tab label and section header is in Title Case
    assert all(w[0].isupper() for t in labels for w in t.split() if w not in ("vs",))
    for h in at.subheader:
        words = [w for w in h.value.split() if w[0].isalpha() and w not in ("per", "and", "for", "vs", "the", "of")]
        assert all(w[0].isupper() for w in words), h.value
    assert len(at.metric) >= 5


def test_the_deck_builds_in_the_web_app(monkeypatch):
    at = _inside(monkeypatch)
    next(b for b in at.button if b.label == "Build client deck").click()
    at = at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def test_auth_module_is_the_web_catalog_one():
    src = Path(json.loads((ROOT / "engine" / "SYNC.json").read_text())["source_repo"]) / "src" / "webapp" / "auth.py"
    if not src.exists():
        pytest.skip("aip_products not on this machine")
    mine = (ROOT / "web" / "auth.py").read_text().split("\n", 1)[1]      # minus the provenance line
    assert mine == src.read_text(), "web/auth.py drifted from aip_products/src/webapp/auth.py"


def test_the_real_secrets_file_is_ignored_by_git():
    ignore = (ROOT / ".gitignore").read_text()
    assert "web/.streamlit/secrets.toml" in ignore


def test_the_table_follows_the_deck_row_settings(monkeypatch):
    """The All Allocations table is rendered with the deck row's coverage, window and productivity."""
    import sys
    sys.path.insert(0, str(ROOT))
    from prf_app import paths  # noqa: F401  (engine on sys.path)
    from src import prfallocpage
    seen = []
    real = prfallocpage.render_alloc_page_html
    monkeypatch.setattr(prfallocpage, "render_alloc_page_html", lambda p: seen.append(p) or real(p))
    at = _inside(monkeypatch)
    at.selectbox(key="al_cov").select("80%")
    at.selectbox(key="al_end").select(2015)
    at.number_input(key="al_pf").set_value(120)
    at = at.run()
    assert not at.exception, [e.message for e in at.exception]
    last = seen[-1]
    assert last["default_coverage"] == "0.8"
    assert last["locked"] == {"end": 2015, "len": 15, "pf": 1.2}     # 20 years would start before 2000
    assert "deck_host" not in last            # the in-table deck button stays hidden on the web
