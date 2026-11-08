"""One-time WeChat binding codes created on the desktop.

The setup guide asks for a code, the user sends “绑定 123456” to the assistant in
WeChat, and the bridge adds that sender to ``allowed_user_ids``. A code has six digits,
expires after ten minutes, works once and is dropped after five wrong attempts. Only the
code's SHA-256 is stored, and only someone at this computer can read the code itself.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

CODE_TTL = 600
MAX_ATTEMPTS = 5
_MESSAGE = re.compile(r'\s*(?:绑定|/bind)\s*[:：]?\s*(\d{6})\s*[。.!！]?\s*')


def _paths(root):
    runtime = Path(root) / '.runtime'
    return runtime / 'pairing.json', runtime / 'pairing-result.json'


def _hash(code):
    return hashlib.sha256(('wechat-bot-pairing:' + code).encode('utf-8')).hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(value, fh)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def create(root, now=None):
    """Starts a new pairing window and returns the code to show on screen."""
    now = time.time() if now is None else now
    code = '%06d' % secrets.randbelow(10 ** 6)
    state_path, result_path = _paths(root)
    result_path.unlink(missing_ok=True)
    _write(state_path, {'hash': _hash(code), 'expires_at': now + CODE_TTL, 'attempts': 0})
    return {'code': code, 'expires_at': now + CODE_TTL}


def cancel(root):
    _paths(root)[0].unlink(missing_ok=True)


def mask(uid):
    uid = str(uid)
    return uid if len(uid) <= 8 else uid[:4] + '…' + uid[-4:]


def status(root, now=None):
    now = time.time() if now is None else now
    state_path, result_path = _paths(root)
    state = _read(state_path)
    active = bool(state and state.get('expires_at', 0) > now)
    result = _read(result_path) or {}
    return {'active': active, 'expires_at': state.get('expires_at') if active else 0,
            'paired': bool(result.get('uid')), 'paired_user': mask(result['uid']) if result.get('uid') else ''}


def claim(root, text, uid, now=None):
    """'paired', 'wrong', or None when the message is not a binding attempt or no code is active."""
    match = _MESSAGE.fullmatch(str(text or ''))
    if not match:
        return None
    now = time.time() if now is None else now
    state_path, result_path = _paths(root)
    state = _read(state_path)
    if not state or state.get('expires_at', 0) <= now:
        state_path.unlink(missing_ok=True)
        return None
    if not secrets.compare_digest(_hash(match.group(1)), str(state.get('hash', ''))):
        state['attempts'] = int(state.get('attempts', 0)) + 1
        if state['attempts'] >= MAX_ATTEMPTS:
            state_path.unlink(missing_ok=True)
        else:
            _write(state_path, state)
        return 'wrong'
    state_path.unlink(missing_ok=True)
    _write(result_path, {'uid': str(uid), 'at': now})
    return 'paired'
