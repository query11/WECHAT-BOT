"""The installed program version (``VERSION`` file) and release tag comparison.

Release tags look like ``v1.2.0`` or ``v1.3.0-beta.1``; ``VERSION`` holds the same
text without the leading ``v``. A pre-release sorts before its final release.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
_PATTERN = re.compile(r'v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-([0-9A-Za-z.-]{1,40}))?')


def read_version(root=ROOT):
    """Installed version text, or '0.0.0' when the file is missing or malformed."""
    try:
        text = (Path(root) / 'VERSION').read_text(encoding='utf-8-sig').strip()
    except OSError:
        return '0.0.0'
    return text if parse(text) else '0.0.0'


def parse(text):
    """(major, minor, patch, pre) where pre is () for a final release; None if invalid."""
    match = _PATTERN.fullmatch(str(text or '').strip())
    if not match:
        return None
    pre = match.group(4)
    parts = tuple(int(p) if p.isdigit() else p for p in pre.split('.')) if pre else ()
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), parts


def is_prerelease(text):
    parsed = parse(text)
    return bool(parsed and parsed[3])


def _key(parsed):
    major, minor, patch, pre = parsed
    # Final releases (no suffix) sort after any pre-release of the same number;
    # numeric pre-release fields sort before text ones, as in SemVer.
    return (major, minor, patch, 1 if not pre else 0,
            tuple((0, p, '') if isinstance(p, int) else (1, 0, p) for p in pre))


def newer(candidate, current):
    """True when release ``candidate`` is strictly newer than version ``current``."""
    a, b = parse(candidate), parse(current)
    return bool(a and b and _key(a) > _key(b))


def display(text):
    parsed = parse(text)
    return 'v' + str(text).strip().lstrip('v') if parsed else str(text)
