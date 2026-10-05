#!/usr/bin/env python3
"""Run a configured checkout, adjacent checkout, or the installed Python package."""
import importlib.util
import os
from pathlib import Path
import sys

configured = os.environ.get('APPSTORE_ADMIN_PACKAGE')
adjacent = Path(__file__).resolve().parents[3]
root = Path(configured).expanduser().resolve() if configured else adjacent
if configured or (root / 'src/appstore_admin/cli.py').is_file():
    if not (root / 'src/appstore_admin/cli.py').is_file():
        sys.exit('Configured package not found; APPSTORE_ADMIN_PACKAGE must point to the source checkout')
    sys.path.insert(0, str(root / 'src'))
elif importlib.util.find_spec('appstore_admin') is None:
    sys.exit('Install appstore-admin in this Python environment or set APPSTORE_ADMIN_PACKAGE to its source checkout')
from appstore_admin.cli import main
raise SystemExit(main())
