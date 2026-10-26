# PyInstaller spec for "PRF Analytics.app". Run from the project root (build_app.command does):
#     .venv/bin/pyinstaller --noconfirm build/prf_analytics.spec
#
# The bundle root mirrors the aip_products repo layout — src/ (as frozen modules), config.ini,
# data/assets, data/seed, data/prf_analytics.db — so the copied engine resolves its own paths
# through config.ROOT exactly as it does at home.
import json
import os

ROOT = os.getcwd()
assert os.path.exists(os.path.join(ROOT, "run_prf_analytics.py")), "run from the project root"
ENGINE = os.path.join(ROOT, "engine")
SYNC = json.load(open(os.path.join(ENGINE, "SYNC.json")))
VERSION = open(os.path.join(ROOT, "VERSION")).read().strip()

datas = [
    (os.path.join(ENGINE, "config.ini"), "."),
    (os.path.join(ENGINE, "SYNC.json"), "."),
    (os.path.join(ENGINE, "data", "prf_analytics.db"), "data"),
    (os.path.join(ENGINE, "data", "assets"), os.path.join("data", "assets")),
    (os.path.join(ENGINE, "data", "seed"), os.path.join("data", "seed")),
    (os.path.join(ROOT, "prf_app", "shell.html"), "prf_app"),
]

engine_modules = ["src." + f[:-3] for f in os.listdir(os.path.join(ENGINE, "src"))
                  if f.endswith(".py") and f != "__init__.py"]

a = Analysis(
    [os.path.join(ROOT, "run_prf_analytics.py")],
    pathex=[ROOT, ENGINE],
    datas=datas,
    hiddenimports=engine_modules + ["prf_app.selftest"],
    excludes=["tkinter", "pandas", "numpy", "matplotlib", "scipy", "PyQt5", "PyQt6", "PySide2",
              "PySide6", "IPython", "pytest", "streamlit", "webview.platforms.qt",
              "webview.platforms.gtk", "webview.platforms.winforms", "webview.platforms.edgechromium",
              "webview.platforms.mshtml", "webview.platforms.cef", "webview.platforms.android"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PRF Analytics",
    console=False,
    argv_emulation=False,
    target_arch=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PRF Analytics")
app = BUNDLE(
    coll,
    name="PRF Analytics.app",
    icon=os.path.join(ROOT, "build", "prf_analytics.icns"),
    bundle_identifier="local.prf-analytics",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "PRF Analytics",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": SYNC["synced_at"].replace("-", "").replace(":", "").replace("T", "."),
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.business",
        "NSHumanReadableCopyright": "PRF data: USDA RMA (public) · FSA National Crop Table",
    },
)
