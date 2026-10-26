"""Entry point for development and for the PyInstaller .app (see build/prf_analytics.spec)."""
import sys

from prf_app.desktop import main

if __name__ == "__main__":
    sys.exit(main())
