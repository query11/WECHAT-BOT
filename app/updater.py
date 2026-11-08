"""Checks GitHub Releases for a newer version and installs it in place.

    python app/updater.py check [--force]      # writes .runtime/update.json
    python app/updater.py prepare              # download, verify and unpack the latest release
    python app/updater.py apply --staging DIR [--wait-pid PID ...] [--no-restart]

Publishing: push a tag ``vX.Y.Z`` whose ``VERSION`` file says ``X.Y.Z``; the release
workflow attaches ``WECHAT-BOT-X.Y.Z.zip`` and ``SHA256SUMS.txt``. A release without
assets falls back to GitHub's source archive of the tag.

Safety rules:
- HTTPS to GitHub hosts only, redirects included; size and member limits on archives.
- An asset with a published SHA-256 (asset digest or SHA256SUMS) must match.
- Archives are unpacked without absolute paths, ``..`` or links; the new tree must hold
  the core program files and the VERSION its tag claims.
- User data (config.json, BOT_MEMORY.md, .runtime/, keys, logs, local state) is never
  written or removed. Program files are backed up first and restored on any failure.
- Git checkouts are only notified; developers update them with ``git pull``.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from app_version import display, newer, parse, read_version
from project_paths import ROOT
DEFAULT_REPO = 'query11/WECHAT-BOT'
API = 'https://api.github.com'
ALLOWED_HOSTS = {'api.github.com', 'github.com', 'codeload.github.com',
                 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}
CHECK_INTERVAL = 6 * 3600
RETRY_INTERVAL = 30 * 60
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_UNPACKED = 500 * 1024 * 1024
MAX_MEMBERS = 5000
REQUIRED = ('VERSION', 'app/wechat_bridge.py', 'app/updater.py', 'desktop/tray_app.ps1', 'desktop/launch_tray.ps1')
# Local state, secrets and the bundled interpreter (runtime/python): never shipped in an
# archive, never touched by an update.
PROTECTED_DIRS = {'.runtime', '.git', 'shots', '_bak', '.venv', 'venv', '__pycache__', 'runtime'}
PROTECTED_NAMES = {'config.json', 'BOT_MEMORY.md', 'sessions.json', 'inbox.json', 'meetings.json',
                   'reminders.json', 'app_targets.json', 'app_preferences.json', 'bridge.pid',
                   'get_updates_buf.txt', 'send_style.txt', '.env'}
PROTECTED_SUFFIXES = ('.key', '.pem', '.pfx', '.p12', '.log', '.pid', '.sqlite', '.db', '.tmp', '.bak')


class UpdateError(Exception):
    """A failure whose message is safe to show to the user."""


def is_protected(rel):
    parts = PurePosixPath(rel).parts
    if not parts:
        return True
    name = parts[-1]
    lowered = name.lower()
    return (parts[0] in PROTECTED_DIRS or name in PROTECTED_NAMES or name.startswith(('config.json', 'BOT_MEMORY.md'))
            or lowered.startswith('bridge.log') or any(lowered.endswith(s) or (s + '-') in lowered for s in PROTECTED_SUFFIXES))


def install_kind(root):
    return 'git' if (Path(root) / '.git').exists() else 'archive'


def _work(root):
    return Path(root) / '.runtime' / 'update'


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def update_config(root):
    raw = _read_json(Path(root) / 'config.json').get('update')
    raw = raw if isinstance(raw, dict) else {}
    repo = str(raw.get('repo') or DEFAULT_REPO)
    # GitHub owners are letters, digits and hyphens; a repository name is never . or ..
    owner, _, name = repo.partition('/')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,39}', owner) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) or name in ('.', '..'):
        repo = DEFAULT_REPO
    return {'repo': repo, 'enabled': raw.get('enabled', True) is not False,
            'prerelease': raw.get('prerelease') is True}


def _check_url(url):
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme != 'https' or (parts.hostname or '').lower() not in ALLOWED_HOSTS:
        raise UpdateError('更新地址不在允许的 GitHub 域名内，已停止')
    return url


class _GitHubOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, opener=None, accept='application/vnd.github+json', timeout=20):
    _check_url(url)
    request = urllib.request.Request(url, headers={
        'Accept': accept, 'User-Agent': 'WECHAT-BOT-updater', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        return (opener or urllib.request.build_opener(_GitHubOnly())).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise UpdateError('没有找到已发布的版本；仓库需要公开并发布 Release') from None
        if exc.code in (403, 429):
            raise UpdateError('GitHub 暂时限制了查询次数，请稍后再试') from None
        raise UpdateError('GitHub 返回错误（HTTP %d）' % exc.code) from None
    except (urllib.error.URLError, OSError):
        raise UpdateError('无法连接 GitHub，请检查网络后再试') from None


def _get_json(url, opener=None):
    with _open(url, opener) as response:
        try:
            return json.loads(response.read(2 * 1024 * 1024).decode('utf-8'))
        except (ValueError, UnicodeError):
            raise UpdateError('GitHub 返回的数据无法识别') from None


def fetch_release(repo, prerelease=False, opener=None):
    """The newest published release (pre-releases only when asked)."""
    if prerelease:
        releases = _get_json('%s/repos/%s/releases?per_page=20' % (API, repo), opener)
        candidates = [r for r in releases if isinstance(r, dict) and not r.get('draft') and parse(r.get('tag_name'))] \
            if isinstance(releases, list) else []
        if not candidates:
            raise UpdateError('没有找到已发布的版本')
        best = candidates[0]
        for item in candidates[1:]:
            if newer(item['tag_name'], best['tag_name']):
                best = item
        return best
    release = _get_json('%s/repos/%s/releases/latest' % (API, repo), opener)
    if not isinstance(release, dict) or not parse(release.get('tag_name')):
        raise UpdateError('最新发布的标签不是有效版本号（应为 vX.Y.Z）')
    return release


def _pick_asset(release):
    assets = [a for a in release.get('assets') or [] if isinstance(a, dict) and a.get('state', 'uploaded') == 'uploaded']
    zips = [a for a in assets if str(a.get('name', '')).lower().endswith('.zip')]
    zips.sort(key=lambda a: not str(a.get('name', '')).upper().startswith('WECHAT-BOT'))
    sums = next((a for a in assets if str(a.get('name', '')).upper().startswith('SHA256SUMS')), None)
    if not zips:
        return None, None
    a = zips[0]
    asset = {'name': str(a['name']), 'url': str(a.get('browser_download_url', '')),
             'size': int(a.get('size') or 0), 'digest': str(a.get('digest') or '')}
    return asset, (str(sums.get('browser_download_url', '')) if sums else None)


def summarize(release, current):
    tag = str(release['tag_name'])
    asset, sums_url = _pick_asset(release)
    return {'latest': tag, 'name': str(release.get('name') or tag), 'notes': str(release.get('body') or '')[:20000],
            'published_at': str(release.get('published_at') or ''), 'html_url': str(release.get('html_url') or ''),
            'asset': asset, 'sums_url': sums_url, 'zipball_url': str(release.get('zipball_url') or ''),
            'available': newer(tag, current)}


def preferences(root):
    return _read_json(_work(root).parent / 'update-prefs.json')


def set_preference(root, **values):
    prefs = preferences(root)
    prefs.update({k: v for k, v in values.items() if v is not None})
    _write_json(_work(root).parent / 'update-prefs.json', prefs)
    return prefs


def check(root=ROOT, force=False, now=None, opener=None):
    """Refreshes and returns ``.runtime/update.json``; cached for CHECK_INTERVAL."""
    root = Path(root)
    now = time.time() if now is None else now
    path = root / '.runtime' / 'update.json'
    cached = _read_json(path)
    settings = update_config(root)
    current = read_version(root)
    state = {'current': current, 'install_kind': install_kind(root), 'repo': settings['repo']}
    if not settings['enabled']:
        state.update(checked_at=now, available=False, disabled=True, error='')
    else:
        age = now - float(cached.get('checked_at') or 0)
        if (not force and cached.get('current') == current and cached.get('repo') == settings['repo']
                and age < (RETRY_INTERVAL if cached.get('error') else CHECK_INTERVAL)):
            return cached
        try:
            state.update(summarize(fetch_release(settings['repo'], settings['prerelease'], opener), current),
                         checked_at=now, error='')
        except UpdateError as exc:
            keep = {k: cached[k] for k in ('latest', 'name', 'notes', 'published_at', 'html_url', 'asset', 'sums_url', 'zipball_url') if k in cached}
            state.update(keep, checked_at=now, error=str(exc),
                         available=bool(keep.get('latest') and newer(keep['latest'], current)))
    state['skipped'] = bool(state.get('latest')) and preferences(root).get('skip') == state.get('latest')
    _write_json(path, state)
    return state


def status_text(root=ROOT):
    """Short WeChat reply for /version, from the last saved check (no network)."""
    root = Path(root)
    lines = ['当前版本 ' + display(read_version(root))]
    state = _read_json(root / '.runtime' / 'update.json')
    if state.get('available') and state.get('latest'):
        lines.append('发现新版本 %s：在电脑上的运行中心点“新版本”即可更新。' % state['latest'])
    elif state.get('error'):
        lines.append('最近一次检查更新未成功：' + state['error'])
    elif state.get('checked_at'):
        lines.append('已是最新版本（%s 检查）' % time.strftime('%m-%d %H:%M', time.localtime(float(state['checked_at']))))
    else:
        lines.append('尚未检查更新；运行中心打开后会自动检查。')
    return '\n'.join(lines)


def _progress(root, stage, done=0, total=0):
    _write_json(_work(root) / 'progress.json', {'stage': stage, 'done': done, 'total': total, 'at': time.time()})


def _download(url, target, root, opener=None, limit=MAX_ARCHIVE):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    digest = hashlib.sha256()
    done = 0
    with _open(url, opener, accept='application/octet-stream', timeout=60) as response:
        total = int(response.headers.get('Content-Length') or 0) if getattr(response, 'headers', None) else 0
        if total > limit:
            raise UpdateError('安装包过大，已停止下载')
        with open(partial, 'wb') as out:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise UpdateError('安装包过大，已停止下载')
                digest.update(chunk)
                out.write(chunk)
                _progress(root, 'download', done, total)
    os.replace(partial, target)
    return digest.hexdigest()


def _expected_sha256(asset, sums_url, opener=None):
    digest = (asset or {}).get('digest', '')
    if digest.lower().startswith('sha256:') and re.fullmatch(r'[0-9a-fA-F]{64}', digest[7:]):
        return digest[7:].lower()
    if sums_url:
        with _open(sums_url, opener, accept='application/octet-stream') as response:
            text = response.read(64 * 1024).decode('utf-8', 'replace')
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[-1].lstrip('*') == asset['name'] and re.fullmatch(r'[0-9a-fA-F]{64}', parts[0]):
                return parts[0].lower()
    return None


def _member_path(name):
    """Validated relative path of an archive member, or None for directories."""
    clean = name.replace('\\', '/')
    if clean.endswith('/'):
        return None
    parts = PurePosixPath(clean).parts
    if clean.startswith('/') or not parts or any(p in ('', '.', '..') for p in parts) or ':' in parts[0]:
        raise UpdateError('安装包包含不安全的路径，已停止')
    return PurePosixPath(*parts)


def extract(archive, dest):
    """Unpacks into ``dest`` (replaced), dropping a single top folder and protected names."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        bundle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile):
        raise UpdateError('安装包已损坏或不是 zip 文件') from None
    with bundle:
        infos = bundle.infolist()
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise UpdateError('安装包内容过多或过大，已停止')
        files = []
        for info in infos:
            path = _member_path(info.filename)
            if path is None:
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UpdateError('安装包包含链接文件，已停止')
            files.append((info, path))
        tops = {p.parts[0] for _, p in files}
        strip = len(tops) == 1 and all(len(p.parts) > 1 for _, p in files)
        for info, path in files:
            rel = PurePosixPath(*path.parts[1:]) if strip else path
            if is_protected(str(rel)):
                continue
            target = dest.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
    return dest


def _tree(base):
    base = Path(base)
    return sorted(p.relative_to(base).as_posix() for p in base.rglob('*')
                  if p.is_file() and not is_protected(p.relative_to(base).as_posix()))


def prepare(root=ROOT, opener=None):
    """Downloads, verifies and unpacks the newest release; returns the staging description."""
    root = Path(root)
    state = check(root, force=True, opener=opener)
    if state.get('error'):
        raise UpdateError(state['error'])
    if not state.get('available'):
        raise UpdateError('已是最新版本')
    if state.get('install_kind') == 'git':
        raise UpdateError('当前是 Git 开发目录，请使用 git pull 更新')
    tag = state['latest']
    version = tag.strip().lstrip('v')
    work = _work(root)
    asset = state.get('asset')
    url = asset['url'] if asset else state.get('zipball_url')
    if not url:
        raise UpdateError('这个版本没有可下载的安装包')
    archive = work / 'downloads' / (re.sub(r'[^0-9A-Za-z.-]', '_', tag) + '.zip')
    _progress(root, 'download')
    actual = _download(url, archive, root, opener)
    expected = _expected_sha256(asset, state.get('sums_url'), opener) if asset else None
    if expected and expected != actual:
        archive.unlink(missing_ok=True)
        raise UpdateError('安装包校验失败（SHA-256 不一致），已删除下载内容')
    _progress(root, 'extract')
    staging = extract(archive, work / 'staging' / version)
    missing = [name for name in REQUIRED if not (staging / name).is_file()]
    if missing:
        raise UpdateError('安装包缺少程序文件：' + '、'.join(missing))
    if read_version(staging) != version:
        raise UpdateError('安装包里的版本号与发布标签 %s 不一致' % tag)
    result = {'ok': True, 'tag': tag, 'version': version, 'staging': str(staging),
              'verified': bool(expected), 'source': 'asset' if asset else 'source-archive'}
    _write_json(work / 'prepared.json', result)
    _progress(root, 'ready')
    return result


def _alive(pid):
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000 | 0x1000, False, int(pid))
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def wait_for_exit(pids, timeout=90):
    deadline = time.time() + timeout
    for pid in pids:
        while _alive(pid):
            if time.time() > deadline:
                raise UpdateError('助手仍在运行，未能开始安装；请完全退出后重试')
            time.sleep(0.3)


def _sha_file(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ''


def apply(root=ROOT, staging=None, wait_pids=(), restart=True, timeout=90):
    """Replaces program files with the staged release; restores everything on failure."""
    root = Path(root).resolve()
    work = _work(root)
    staging = Path(staging).resolve()
    if work.resolve() / 'staging' not in staging.parents:
        raise UpdateError('只能安装由 prepare 准备的版本')
    new_files = _tree(staging)
    if any(name not in new_files for name in REQUIRED):
        raise UpdateError('待安装的文件不完整，已停止')
    old_version = read_version(root)
    old_files = set(_read_json(work / 'manifest.json').get('files') or [])
    old_requirements = _sha_file(root / 'requirements.txt')
    result = {'ok': False, 'from': old_version, 'to': read_version(staging), 'at': time.time()}
    try:
        wait_for_exit(wait_pids, timeout)
        backup = work / 'backup' / ('%s-%d' % (old_version, int(time.time())))
        replaced, created, removed = [], [], []
        try:
            for rel in new_files:
                src, dst = staging / rel, root / rel
                if dst.is_file():
                    keep = backup / rel
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, keep)
                    replaced.append(rel)
                else:
                    created.append(rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            # Only files the previous update installed may be removed; never local data.
            for rel in sorted(old_files - set(new_files)):
                dst = root / rel
                if is_protected(rel) or not dst.is_file():
                    continue
                keep = backup / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, keep)
                dst.unlink()
                removed.append(rel)
            _write_json(work / 'manifest.json', {'version': read_version(root), 'files': new_files})
        except Exception:
            for rel in replaced + removed:
                try:
                    shutil.copy2(backup / rel, root / rel)
                except OSError:
                    pass
            for rel in created:
                try:
                    (root / rel).unlink()
                except OSError:
                    pass
            raise UpdateError('安装过程中出错，已恢复原版本') from None
        result.update(ok=True, to=read_version(root), backup=str(backup),
                      changed=len(replaced) + len(created), removed=len(removed))
        if _sha_file(root / 'requirements.txt') != old_requirements:
            result['dependencies'] = _install_requirements(root)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(work / 'downloads', ignore_errors=True)
    except UpdateError as exc:
        result['message'] = str(exc)
    _write_json(work / 'result.json', result)
    if restart:
        _restart(root)
    if not result['ok']:
        raise UpdateError(result['message'])
    return result


def _install_requirements(root):
    try:
        done = subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(Path(root) / 'requirements.txt')],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        return 'installed' if done.returncode == 0 else 'failed'
    except (OSError, subprocess.SubprocessError):
        return 'failed'


def _restart(root):
    if os.name != 'nt':
        return
    shell = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    flags = 0x00000008 | 0x00000200 | 0x08000000   # detached, new group, no window
    subprocess.Popen([shell, '-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
                      '-File', str(Path(root) / 'desktop' / 'launch_tray.ps1'), '-Show'], cwd=str(root), creationflags=flags, close_fds=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='微信助手版本更新')
    parser.add_argument('action', choices=('check', 'prepare', 'apply', 'skip'))
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--staging')
    parser.add_argument('--tag', help='skip: the release tag not to remind about')
    parser.add_argument('--wait-pid', type=int, action='append', default=[])
    parser.add_argument('--no-restart', action='store_true')
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    try:
        if args.action == 'check':
            out = dict(check(args.root, force=args.force), ok=True)
        elif args.action == 'prepare':
            out = prepare(args.root)
        elif args.action == 'skip':
            if not parse(args.tag):
                raise UpdateError('请提供要跳过的版本号')
            set_preference(args.root, skip=args.tag)
            out = dict(check(args.root), ok=True)
        else:
            if not args.staging:
                raise UpdateError('缺少 --staging')
            out = apply(args.root, args.staging, args.wait_pid, restart=not args.no_restart)
    except UpdateError as exc:
        out = {'ok': False, 'message': str(exc)}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
