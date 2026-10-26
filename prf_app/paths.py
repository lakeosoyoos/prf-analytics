"""Where everything lives, in development and inside the frozen .app.

The engine (the web app's PRF modules, a package literally named `src` because the modules
import each other as `src.prfpage`) sits at engine/ in the repo and at the bundle root in the
.app, next to config.ini and data/ — the same layout as the aip_products repo, which is what
lets the copied modules find their seeds through config.ROOT without being edited.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
ENGINE = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent.parent / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

APP_NAME = "PRF Analytics"
DB_PATH = Path(os.environ.get("PRF_DB_PATH") or ENGINE / "data" / "prf_analytics.db")
ASSETS_DIR = ENGINE / "data" / "assets"
SEED_DIR = ENGINE / "data" / "seed"
SYNC_JSON = ENGINE / "SYNC.json"
SHELL_HTML = Path(__file__).resolve().parent / "shell.html"

# The user's own copies of the hand-edited commission seeds. The bundled ones are read-only
# inside the .app, so editing happens on a copy here, and a copy here always wins.
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
LOG_PATH = Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
SEEDS = ("aip_commission.csv", "commission_by_timezone.csv")


def seed_path(name: str) -> Path:
    user = SUPPORT_DIR / name
    return user if user.exists() else SEED_DIR / name
