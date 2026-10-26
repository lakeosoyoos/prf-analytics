"""The two native actions the window exposes: Save CSV, and editing the commission rates."""
from __future__ import annotations

import csv

from prf_app import desktop, paths, views


class FakeWindow:
    def __init__(self, answer):
        self.answer, self.asked = answer, None

    def create_file_dialog(self, kind, directory="", save_filename=""):
        self.asked = save_filename
        return self.answer


def test_save_writes_the_filtered_csv(tmp_path):
    api = desktop.Api()
    api._window = FakeWindow((str(tmp_path / "out.csv"),))
    saved = api.save_vintage_csv("Grazing", "0.9", 100, "WA")
    assert saved == str(tmp_path / "out.csv")
    assert api._window.asked == "prf_old_vs_new_Grazing_90pct_WA.csv"
    rows = list(csv.reader(open(saved)))
    assert rows[0][:4] == ["State", "County", "FIPS", "Grid"] and {r[0] for r in rows[1:]} == {"WA"}


def test_save_cancelled_writes_nothing(tmp_path):
    api = desktop.Api()
    api._window = FakeWindow(None)
    assert api.save_vintage_csv("Grazing", "90%", 100, "All states") == ""
    assert not list(tmp_path.iterdir())


def test_edit_commission_copies_once_and_then_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SUPPORT_DIR", tmp_path)
    opened = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda cmd, check: opened.append(cmd))

    before = views.seed_mtimes()
    target = desktop.Api().edit_commission()
    assert opened and opened[0][:2] == ["open", "-R"]
    for name in paths.SEEDS:
        assert (tmp_path / name).read_bytes() == (paths.SEED_DIR / name).read_bytes()
        assert paths.seed_path(name) == tmp_path / name   # the user's copy is what the map reads
    assert views.seed_mtimes() != before                   # so the map's cache key moved

    (tmp_path / paths.SEEDS[0]).write_text("edited\n")
    desktop.Api().edit_commission()                        # a second press must not clobber edits
    assert (tmp_path / paths.SEEDS[0]).read_text() == "edited\n"
    assert target == str(tmp_path / paths.SEEDS[0])
