"""Bounded recovery of explicitly registered snapshots; never discover user files.

Each directory has a durable receipt and matching marker, plus an OS lease held
for its entire use. Recovery only unlinks listed regular files in that exact
directory. Unknown/tampered entries are retained. No age/PID guess is used to
decide whether another backend is alive, and cleanup never changes task results.
"""
from contextlib import contextmanager, ExitStack
import json
import logging
import os
import re
import stat
import time
import uuid

log = logging.getLogger('bridge')
MARKER = '.wechat-snapshot.json'
LEASE = '.wechat-snapshot.lease'
_PREFIXES = {'artifact': 'artifact-send-', 'batch': '.wechat-batch-inputs-'}
_UUID = re.compile(r'^[0-9a-f]{32}$')
_RUN = (os.getpid(), uuid.uuid4().hex)


class SnapshotError(OSError):
    pass


class _Busy(SnapshotError):
    pass


def _norm(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _within(path, root):
    try:
        return os.path.commonpath((_norm(path), _norm(root))) == _norm(root)
    except (ValueError, OSError):
        return False


def _components(path):
    current = os.path.abspath(os.fspath(path))
    while True:
        try:
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise SnapshotError('snapshot path contains a redirection')
        except FileNotFoundError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if _norm(path) != _norm(os.path.realpath(path)):
        raise SnapshotError('snapshot path resolves elsewhere')


def _regular(path):
    _components(path)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SnapshotError('snapshot entry is not a private regular file')
    return info


@contextmanager
def _pin_directories(*paths):
    """Prevent Windows ancestor rename/junction replacement during deletion."""
    handles = []
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            create = api.CreateFileW
            create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
            create.restype = wintypes.HANDLE
            close = api.CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            ordered = set()
            for path in paths:
                current = os.path.abspath(path)
                while True:
                    ordered.add(current)
                    parent = os.path.dirname(current)
                    if current == parent:
                        break
                    current = parent
            for path in sorted(ordered, key=lambda p: (len(p), p)):
                _components(path)
                handle = create(path, 0x80, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise SnapshotError('unable to pin snapshot directory')
                handles.append(handle)
                _components(path)
        else:
            for path in paths:
                _components(path)
        yield
    finally:
        if os.name == 'nt':
            for handle in reversed(handles):
                close(handle)


@contextmanager
def _lock(path, *, create=False):
    """An OS lock survives Python scheduling and releases on hard process exit."""
    _components(path)
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    stream = os.fdopen(fd, 'r+b')
    locked = False
    try:
        actual, named = os.fstat(fd), _regular(path)
        if (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino):
            raise SnapshotError('snapshot lock changed while opening')
        if os.name == 'nt':
            from task_artifacts import _windows_final_path
            if _norm(_windows_final_path(fd)) != _norm(path):
                raise SnapshotError('snapshot lock resolves elsewhere')
        if actual.st_size == 0 and create:
            stream.write(b'1')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise _Busy('snapshot is active') from None
        yield
    finally:
        try:
            if locked:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            stream.close()


def _json(path):
    _regular(path)
    with open(path, 'rb') as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise SnapshotError('snapshot receipt is too large')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SnapshotError('duplicate snapshot receipt field')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def _write(path, record):
    with open(path, 'xb') as stream:
        stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True).encode('ascii'))
        stream.flush()
        os.fsync(stream.fileno())


def _validate(record, receipt, registry, allowed_roots):
    fields = {'version', 'id', 'kind', 'run_id', 'pid', 'task_id', 'root', 'directory', 'registry', 'files'}
    if not isinstance(record, dict) or set(record) != fields or record['version'] != 1:
        raise SnapshotError('invalid snapshot receipt schema')
    ident, kind = record['id'], record['kind']
    if not isinstance(ident, str) or not _UUID.fullmatch(ident) or kind not in _PREFIXES:
        raise SnapshotError('invalid managed snapshot identity')
    if (not isinstance(record['run_id'], str) or not _UUID.fullmatch(record['run_id'])
            or type(record['pid']) is not int or record['pid'] <= 0
            or not isinstance(record['task_id'], str) or not 0 < len(record['task_id']) <= 160):
        raise SnapshotError('invalid snapshot owner')
    root, directory = record['root'], record['directory']
    for path in (root, directory, record['registry']):
        if not isinstance(path, str) or not os.path.isabs(path) or path != os.path.abspath(path):
            raise SnapshotError('invalid snapshot absolute path')
        _components(path)
    if (_norm(record['registry']) != _norm(registry)
            or _norm(receipt) != _norm(os.path.join(registry, ident + '.json'))
            or _norm(directory) != _norm(os.path.join(root, _PREFIXES[kind] + ident))
            or not any(_within(root, allowed) for allowed in allowed_roots)):
        raise SnapshotError('snapshot receipt exceeds managed scope')
    files = record['files']
    if not isinstance(files, list) or len(files) != 1:
        raise SnapshotError('invalid snapshot file list')
    name = files[0]
    if (not isinstance(name, str) or not name or name.casefold() in ('.', '..', MARKER, LEASE)
            or name != os.path.basename(name) or any(c in name for c in '/\\:')
            or any(ord(c) < 32 for c in name) or name[-1] in ' .'):
        raise SnapshotError('invalid snapshot filename')
    return directory


def _cleanup(receipt, registry, allowed_roots):
    """Caller holds registry lock; lease prevents deleting an active snapshot."""
    record = _json(receipt)
    directory = _validate(record, receipt, registry, allowed_roots)
    if not os.path.exists(directory):
        # No directory means no user files can be removed by this branch.
        os.unlink(receipt)
        return 'removed'
    marker, lease = os.path.join(directory, MARKER), os.path.join(directory, LEASE)
    with _pin_directories(registry, directory):
        if not os.path.lexists(marker):
            # A previous cleanup may have removed its marker just before an
            # empty-directory removal failed. Never delete any file without it.
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    raise SnapshotError('snapshot marker missing; nonempty directory retained')
        else:
            if _json(marker) != record:
                raise SnapshotError('snapshot marker does not match durable receipt')
            _cleanup_files(record, directory, lease, marker)
    _components(directory)
    os.rmdir(directory)
    os.unlink(receipt)
    return 'removed'


def _cleanup_files(record, directory, lease, marker):
    # Missing lease can be the result of an interrupted cleanup. An identical
    # marker still proves ownership, and Windows prevents removing a live lease.
    with _lock(lease, create=True):
        permitted = set(record['files']) | {MARKER, LEASE}
        with os.scandir(directory) as entries:
            names = set()
            for entry in entries:
                names.add(entry.name)
                if entry.name not in permitted or len(names) > len(permitted):
                    raise SnapshotError('unknown file in snapshot directory; retained')
                _regular(entry.path)
        # No recursive deletion: only the single registered copy is unlinked.
        for name in record['files']:
            path = os.path.join(directory, name)
            if os.path.lexists(path):
                _regular(path)
                os.unlink(path)
    os.unlink(lease)
    os.unlink(marker)


def reclaim_snapshots(registry_dir, *, allowed_roots, max_records=32, max_seconds=.15, _only=None):
    """Best-effort, non-blocking startup cleanup. Returns compact local counts.

    Only scan direct receipt entries, bounded by count and wall-clock deadline;
    no workdir scan or adoption of legacy/unregistered temporary directories.
    File-system calls already in progress cannot be preempted by the deadline.
    """
    result = {'removed': 0, 'active': 0, 'retained': 0}
    registry = os.path.abspath(os.fspath(registry_dir))
    deadline = time.monotonic() + max(0, min(float(max_seconds), 2))
    try:
        _components(registry)
        if not os.path.isdir(registry):
            return result
        with _pin_directories(registry), _lock(os.path.join(registry, '.registry.lock'), create=True):
            with ExitStack() as stack:
                entries = ([_only] if _only else
                           (entry.name for entry in stack.enter_context(os.scandir(registry))))
                examined = 0
                for name in entries:
                    if time.monotonic() >= deadline or examined >= min(max(0, int(max_records)), 256):
                        break
                    examined += 1
                    if not re.fullmatch(r'[0-9a-f]{32}\.json', name):
                        continue
                    try:
                        status = _cleanup(os.path.join(registry, name), registry, allowed_roots)
                    except _Busy:
                        status = 'active'
                    except (OSError, ValueError, TypeError, KeyError, RecursionError):
                        status = 'retained'
                    result[status] += 1
    except (OSError, ValueError, TypeError, RecursionError):
        result['retained'] += 1
    if result['retained']:
        log.warning('快照清理暂缓：%d 项，任务/发送结果不受影响。', result['retained'])
    return result


@contextmanager
def managed_snapshot(root, name, *, registry_dir, task_id=None, kind='artifact'):
    """Yield one registered output path while holding a cross-process lease."""
    global _RUN
    root, registry = os.path.abspath(os.fspath(root)), os.path.abspath(os.fspath(registry_dir))
    if _RUN[0] != os.getpid():
        _RUN = (os.getpid(), uuid.uuid4().hex)
    ident = uuid.uuid4().hex
    record = dict(version=1, id=ident, kind=kind, run_id=_RUN[1], pid=_RUN[0],
                  task_id=str(task_id or ('untracked-' + uuid.uuid4().hex)), root=root,
                  directory=os.path.join(root, _PREFIXES[kind] + ident),
                  registry=registry, files=[name])
    receipt = os.path.join(registry, ident + '.json')
    directory = _validate(record, receipt, registry, [root])
    for path in (root, registry):
        _components(path)
        os.makedirs(path, exist_ok=True)
        _components(path)
    lease, entered = None, False
    try:
        with _lock(os.path.join(registry, '.registry.lock'), create=True):
            os.mkdir(directory, 0o700)
            lease = _lock(os.path.join(directory, LEASE), create=True)
            lease.__enter__()
            entered = True
            # Receipt precedes the marker and any payload. No payload is written
            # before both durable identities exist.
            _write(receipt, record)
            _write(os.path.join(directory, MARKER), record)
        yield os.path.join(directory, name)
    finally:
        if entered:
            try:
                lease.__exit__(None, None, None)
            except OSError:
                log.warning('快照租约释放暂缓，任务/发送结果不受影响。')
        # Best effort only: neither an upload return nor a model exception may
        # be replaced by an antivirus/share-lock/cleanup failure.
        reclaim_snapshots(registry, allowed_roots=[root], max_records=256,
                          max_seconds=.15, _only=ident + '.json')
