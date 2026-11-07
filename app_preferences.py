"""Local app preferences: identities select discovered entries, never executable input."""
import argparse
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


def load_preferences(path):
    empty = {'version': 1, 'revision': 0, 'favorites': [], 'aliases': {}}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('version') != 1 or
                type(data.get('revision')) is not int or data['revision'] < 0 or
                not isinstance(data.get('favorites'), list) or
                not all(isinstance(x, str) for x in data['favorites']) or
                not isinstance(data.get('aliases'), dict) or
                not all(isinstance(k, str) and isinstance(v, str) for k, v in data['aliases'].items())):
            return empty
        return data
    except (OSError, ValueError, UnicodeError):
        return empty


def catalog_from_launcher(launcher, prefs=None):
    prefs = prefs or {'favorites': [], 'aliases': {}}
    records = {}
    if launcher:
        for name, shortcut in launcher._apps:
            identity = launcher._identities.get(shortcut)
            if identity and identity not in records:
                records[identity] = dict(id=identity, name=name, target=identity,
                                         shortcut=shortcut, available=True)
    for identity in set(prefs['favorites']) | set(prefs['aliases'].values()):
        records.setdefault(identity, dict(id=identity, name='已失效的应用', target=identity,
                                         shortcut='', available=False))
    for identity, record in records.items():
        record['favorite'] = identity in prefs['favorites']
        record['aliases'] = [a for a, target in prefs['aliases'].items() if target == identity]
    return sorted(records.values(), key=lambda r: (not r['favorite'], not r['available'], r['name'].casefold()))


def validate_preferences(payload, launcher):
    from app_launcher import _norm, _strip_tail, _APP_FAMILIES, _BROWSER_ALIASES, parse_command
    favorites, aliases = payload.get('favorites'), payload.get('aliases')
    if not isinstance(favorites, list) or not isinstance(aliases, dict):
        raise ValueError('收藏和别名格式无效')
    known = {r['id'] for r in catalog_from_launcher(launcher)}
    if any(not isinstance(x, str) or x not in known for x in favorites):
        raise ValueError('应用已失效，请重新读取列表后保存')
    names = {}
    if launcher:
        for name, path in launcher._apps:
            identity = launcher._identities.get(path)
            for key in (_norm(name), launcher._base_names.get(path, _norm(name))):
                names.setdefault(key, set()).add(identity)
    normalized = set()
    clean = {}
    for alias, target in aliases.items():
        if (not isinstance(alias, str) or not isinstance(target, str) or target not in known or
                not alias.strip() or len(alias.strip()) > 20 or
                not re.fullmatch(r'[\w\u3400-\u9fff ·-]+', alias.strip()) or
                len(alias.split()) > 3):
            raise ValueError('别名须为 1–20 字的应用简称，不能包含路径或命令符号')
        alias = alias.strip()
        key = _norm(alias)
        if not key or _norm(_strip_tail(alias)) != key or key in normalized:
            raise ValueError('别名重复或包含会被忽略的语气词')
        action,parsed=parse_command("打开"+alias)
        if action!="open" or _norm(parsed)!=key:
            raise ValueError("此别名不能作为应用简称识别，请换一个简短名称")
        normalized.add(key)
        canonical = _BROWSER_ALIASES.get(key, key)
        reserved = names.get(key, set()) | names.get(canonical, set())
        for family, targets in _APP_FAMILIES:
            if canonical in family:
                family_ids = set().union(*(names.get(n, set()) for n in targets))
                if not family_ids or target not in family_ids:
                    raise ValueError('不能将已有应用简称指向其他应用：' + alias)
                reserved |= family_ids
        if reserved and reserved != {target}:
            raise ValueError('别名与已有应用名称冲突：' + alias)
        clean[alias] = target
    return {'version': 1, 'favorites': list(dict.fromkeys(favorites)), 'aliases': clean}


@contextmanager
def _write_lock(path):
    # OS releases this lock on process death; a stale file cannot block saves.
    lock=Path(str(path)+'.lock')
    with lock.open('a+b') as stream:
        if lock.stat().st_size==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise ValueError('其他窗口正在保存，请稍后重试') from None
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)


def save_preferences(path, payload, launcher, expected_revision):
    data = validate_preferences(payload, launcher)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock(path):
        current = load_preferences(path)
        if type(expected_revision) is not int or expected_revision != current['revision']:
            raise ValueError('设置已被其他窗口修改，请重新读取后再保存')
        data['revision'] = current['revision'] + 1
        fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return data


def handle_request(root, request, launcher=None):
    if launcher is None:
        import wechat_bridge
        # Keep discovery/cache rooted alongside the explicitly selected config.
        wechat_bridge.HERE = str(root)
        launcher = wechat_bridge.make_launcher(wechat_bridge.load_config(str(Path(root) / 'config.json')))
    path = Path(root) / 'app_preferences.json'
    if request.get('action', 'view') == 'save':
        prefs = save_preferences(path, request, launcher, request.get('revision'))
    elif request.get('action', 'view') == 'view':
        prefs = load_preferences(path)
    else:
        raise ValueError('未知操作')
    return dict(ok=True, revision=prefs['revision'], apps=catalog_from_launcher(launcher, prefs))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent))
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
        if not isinstance(request, dict):
            raise ValueError('请求格式无效')
        result = handle_request(Path(args.root).resolve(), request)
    except Exception as exc:
        result = {'ok': False, 'error': str(exc)}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
