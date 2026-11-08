"""Service behind the first-run setup guide (setup_wizard.ps1). Requests arrive on stdin.

Operations: ``status`` (environment and setup progress), ``init`` (create config.json from
the example with the chosen folders), ``install_deps``, ``pair`` / ``pair_status`` /
``pair_cancel`` (desktop binding code, see pairing.py) and ``complete``. Responses never
contain credentials: only whether the WeChat login and allowlist are present.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import time

import pairing
from app_version import read_version

ROOT = Path(__file__).resolve().parent


class SetupError(ValueError):
    """A message that is safe to show in the guide."""


def _read_config(root):
    try:
        raw = json.loads((Path(root) / 'config.json').read_text(encoding='utf-8-sig'))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _write_config(root, raw):
    """Atomic UTF-8 with BOM, as every other writer of config.json (PowerShell reads it)."""
    path = Path(root) / 'config.json'
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.setup-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _claude_path(raw):
    command = (raw or {}).get('claude_bin', 'claude')
    first = command[0] if isinstance(command, list) and command else command
    return shutil.which(str(first or 'claude')) or ''


def _bridge_running(root, now):
    try:
        status = json.loads((Path(root) / '.runtime' / 'status.json').read_text(encoding='utf-8-sig'))
        return bool(status.get('pid')) and now - float(status.get('heartbeat') or 0) <= 10
    except (OSError, ValueError, TypeError):
        return False


def status(root=ROOT, now=None):
    root = Path(root)
    now = time.time() if now is None else now
    raw = _read_config(root)
    config = raw or {}
    inbox = config.get('inbox') if isinstance(config.get('inbox'), dict) else {}
    marker = Path(root) / '.runtime' / 'onboarding.json'
    return {
        'ok': True, 'version': read_version(root),
        'python': {'ok': sys.version_info >= (3, 10), 'version': '%d.%d.%d' % sys.version_info[:3]},
        'qr_library': importlib.util.find_spec('segno') is not None,
        'claude': _claude_path(config),
        'config': {'exists': raw is not None, 'workdir': str(config.get('workdir') or ''),
                   'inbox': str(inbox.get('dir') or ''),
                   'logged_in': bool(str(config.get('bot_token') or '').strip()),
                   'allowed': len([u for u in config.get('allowed_user_ids') or [] if str(u).strip()]),
                   'backend': str(config.get('backend') or 'claude')},
        'memory': (root / 'BOT_MEMORY.md').exists(),
        'bridge_running': _bridge_running(root, now),
        'pairing': pairing.status(root, now),
        'completed': marker.exists(),
    }


def _folder(value, label):
    text = str(value or '').strip().strip('"')
    if not text or len(text) > 240 or any(ord(c) < 32 for c in text):
        raise SetupError('请选择%s' % label)
    path = PureWindowsPath(text) if os.name == 'nt' or ':' in text[:3] else Path(text)
    if not path.is_absolute():
        raise SetupError('%s需要完整路径，例如 C:\\WeChatBot\\workspace' % label)
    if len(path.parts) < 2:
        raise SetupError('%s不能是整个磁盘，请新建或选择一个专用文件夹' % label)
    return Path(text)


def init(root=ROOT, workdir='', inbox=''):
    """Creates or updates config.json with the chosen folders; other settings stay as they are."""
    root = Path(root)
    work = _folder(workdir, '工作目录')
    box = _folder(inbox or str(work / 'inbox'), '收件目录')
    try:
        work.mkdir(parents=True, exist_ok=True)
        box.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise SetupError('无法创建所选文件夹，请检查路径或权限') from None
    raw = _read_config(root)
    if raw is None:
        if (root / 'config.json').exists():
            raise SetupError('现有 config.json 无法读取，请先修复或移走它')
        try:
            raw = json.loads((root / 'config.example.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise SetupError('缺少 config.example.json，程序文件不完整') from None
    raw['workdir'] = str(work)
    inbox_cfg = raw.get('inbox') if isinstance(raw.get('inbox'), dict) else {}
    inbox_cfg['dir'] = str(box)
    raw['inbox'] = inbox_cfg
    _write_config(root, raw)
    memory = root / 'BOT_MEMORY.md'
    if not memory.exists() and (root / 'BOT_MEMORY.example.md').exists():
        shutil.copyfile(root / 'BOT_MEMORY.example.md', memory)
    return dict(status(root), message='已保存工作目录和收件目录')


def install_deps(root=ROOT):
    try:
        done = subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(Path(root) / 'requirements.txt')],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.SubprocessError):
        raise SetupError('依赖安装失败，请检查网络后重试') from None
    if done.returncode != 0:
        raise SetupError('依赖安装失败，请检查网络后重试')
    importlib.invalidate_caches()
    return dict(status(root), message='依赖已安装')


def pair(root=ROOT):
    state = status(root)
    if not state['config']['logged_in']:
        raise SetupError('请先完成微信扫码登录')
    code = pairing.create(root)
    return dict(status(root), code=code['code'], message='请在微信里发送：绑定 ' + code['code'])


def complete(root=ROOT):
    marker = Path(root) / '.runtime' / 'onboarding.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'completed_at': time.time(), 'version': read_version(root)}), encoding='utf-8')
    return dict(status(root), message='使用引导已完成')


def execute(root, request):
    try:
        if not isinstance(request, dict):
            raise SetupError('请求格式无效')
        op = request.get('operation')
        if op == 'status':
            return status(root)
        if op == 'init':
            return init(root, request.get('workdir'), request.get('inbox'))
        if op == 'install_deps':
            return install_deps(root)
        if op == 'pair':
            return pair(root)
        if op == 'pair_status':
            return status(root)
        if op == 'pair_cancel':
            pairing.cancel(root)
            return status(root)
        if op == 'complete':
            return complete(root)
        raise SetupError('不支持的操作')
    except SetupError as exc:
        return {'ok': False, 'message': str(exc)}
    except (OSError, ValueError, TypeError):
        return {'ok': False, 'message': '操作失败，请检查程序目录的读写权限'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(ROOT))
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read(32768).lstrip('\ufeff'))
    except ValueError:
        request = None
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(execute(args.root, request), ensure_ascii=False))


if __name__ == '__main__':
    main()
