"""Where the program and its data live.

The Python modules sit in ``app/`` and the Windows scripts in ``desktop/``. Configuration,
fixed memory, logs and ``.runtime/`` stay in the project root above them (the install
folder), next to ``VERSION`` and ``requirements.txt``.
"""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
DESKTOP_DIR = ROOT / 'desktop'
