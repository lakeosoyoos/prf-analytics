#!/bin/bash
# Build "PRF Analytics.app" and install it to ~/Applications — double-click, or run in Terminal.
#
#   ./build_app.command          test, build, self-test, install
#   ./build_app.command --sync   first pull the latest PRF code + data from ~/aip_products
#
# Nothing is installed unless every gate passes: the test suite (including the PRF-only DB vs
# full DB parity check), the bundle signature, then the FROZEN app's own --self-test, which
# renders every view from inside the bundle, and --check-window, which clicks through every
# tab of the real window.
#
# WHY THE BUILD HAPPENS OUTSIDE THIS FOLDER: ~/Desktop is iCloud Drive, which stamps Finder
# metadata onto files as fast as it can be removed, and codesign refuses a bundle carrying it
# ("resource fork, Finder information, or similar detritus not allowed"). The venv lives
# outside too, so iCloud's "Optimize Mac Storage" can never evict half of it.
set -euo pipefail
cd "$(dirname "$0")"

SYNC=0
for arg in "$@"; do
  case "$arg" in
    --sync) SYNC=1 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

VENV="${PRF_VENV:-$HOME/.prf_analytics_venv}"
PY="$VENV/bin/python"
OUT="$HOME/Library/Caches/PRF Analytics build"
APP="$OUT/dist/PRF Analytics.app"
DEST="$HOME/Applications/PRF Analytics.app"

if [ ! -x "$PY" ]; then
  echo "No build environment yet — create it once:"
  echo "  python3 -m venv \"$VENV\" && \"$VENV/bin/pip\" install -r requirements.txt"
  exit 1
fi

if [ "$SYNC" = 1 ]; then
  echo "== 1/5 sync engine from aip_products"
  "$PY" sync_engine.py
else
  echo "== 1/5 sync skipped (pass --sync to pull the latest PRF code + data)"
fi

echo "== 2/5 tests"
"$PY" -m pytest -q -p no:cacheprovider

echo "== 3/5 build"
rm -rf "$OUT/dist"
"$VENV/bin/pyinstaller" --noconfirm --log-level ERROR \
  --distpath "$OUT/dist" --workpath "$OUT/work" build/prf_analytics.spec

echo "== 4/5 sign"
# Ad-hoc: Apple Silicon will not run an unsigned arm64 binary, and a whole-bundle signature
# is what lets macOS open the app without calling it damaged.
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "== 5/5 self-test of the built app (server, then the real window — it opens for a few seconds)"
"$APP/Contents/MacOS/PRF Analytics" --self-test
"$APP/Contents/MacOS/PRF Analytics" --check-window

mkdir -p "$HOME/Applications"
rm -rf "$DEST"
ditto "$APP" "$DEST"
echo
echo "Installed: $DEST  ($(du -sh "$DEST" | cut -f1))"
echo "Open it from Finder → Applications (your home folder's), Spotlight, or: open \"$DEST\""
