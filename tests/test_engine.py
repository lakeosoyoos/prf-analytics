"""engine/ is what sync_engine.py says it is: verbatim copies, plus a prfopt shim whose
constants equal the real module's."""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

from prf_app import paths

SYNC = json.loads(paths.SYNC_JSON.read_text())
REPO = Path(SYNC["source_repo"])


def _constants(path: Path) -> dict:
    out = {}
    for node in ast.parse(path.read_text()).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if isinstance(target, ast.Name) and node.value is not None:
            try:
                out[target.id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def test_engine_loads_without_pandas():
    from src import prfdeck, prfherdpage, prfintervalpage, prfpage, prfvintagepage  # noqa: F401
    assert "pandas" not in sys.modules


def test_copied_files_unchanged_since_sync():
    for rel, digest in SYNC["files"].items():
        if digest.startswith("generated"):
            continue
        got = hashlib.sha256((paths.ENGINE / rel).read_bytes()).hexdigest()
        assert got == digest, f"engine/{rel} was edited after sync — edit it in aip_products instead"


@pytest.mark.skipif(not REPO.exists(), reason="aip_products not on this machine")
def test_prfopt_shim_matches_source():
    real = _constants(REPO / "src" / "prfopt.py")
    shim = _constants(paths.ENGINE / "src" / "prfopt.py")
    assert shim and all(shim[k] == real[k] for k in shim)
    assert set(shim) == {"INTERVALS", "MIN_INTERVALS", "MAX_INTERVALS", "STEP_PCT", "MIN_PCT", "MAX_PCT"}


@pytest.mark.skipif(not REPO.exists(), reason="aip_products not on this machine")
def test_engine_is_current_with_repo():
    """Informational drift check: fails when aip_products has moved on since the last sync."""
    stale = [rel for rel, digest in SYNC["files"].items()
             if not digest.startswith("generated")
             and hashlib.sha256((REPO / rel).read_bytes()).hexdigest() != digest]
    assert not stale, f"aip_products changed since sync ({stale}) — run sync_engine.py"
