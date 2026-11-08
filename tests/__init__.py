"""Offline fixtures and regression tests for the WeChat assistant.

The program modules live in ``app/`` and are imported by their plain names (``import
wechat_bridge``), exactly as they are when ``app/wechat_bridge.py`` runs. Importing this
package puts ``app/`` first on ``sys.path`` and on ``PYTHONPATH`` for child processes.
"""
import os
import sys

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
_paths = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p]
if APP_DIR not in _paths:
    os.environ['PYTHONPATH'] = os.pathsep.join([APP_DIR] + _paths)
