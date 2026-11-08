"""Explicit task-file declarations and bounded, read-only file verification.

Only ``[[产物]]{"path":"absolute path","label":"description"}`` lines
are declarations. A path in prose is never discovery evidence. Verification
establishes existence/readability/basic format and a version fingerprint; it
does not establish semantic correctness or that this task created the file.
No directory is searched, archive extracted, document executed or file sent.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import io
import json
import ntpath
import os
import stat
import struct
import zipfile
import xml.etree.ElementTree as ET
import zlib

from snapshot_lifecycle import managed_snapshot

from bridge_text import _SEND_DENY_EXT, _SEND_DENY_HERE, _SEND_DENY_NAMES

MARK_ARTIFACT = '[[产物]]'
MAX_DECLARATIONS = 20
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_MARKER_CHARS = 4096
MAX_LABEL_CHARS = 80
MAX_ARCHIVE_ENTRIES = 2048
MAX_ARCHIVE_EXPANDED = 200 * 1024 * 1024
MAX_ARCHIVE_RATIO = 200
ARTIFACT_PROMPT = (
    '\n【任务产物登记】\n'
    '本次实际生成或修改了要交付的文件时，回复末尾为每个文件独立写一行 '
    '[[产物]] 后紧接 JSON 对象，例如 '
    '[[产物]]{"path":"D:/工作目录/结果.xlsx","label":"汇总表"}。'
    'path 必须是完整绝对路径，label 简短；JSON 中的反斜杠需转义。'
    '最多 20 个，只登记实际存在的交付文件，不登记资料输入、凭据或运行数据。'
    '没有文件则不要编造声明；正文提到路径不等于产物登记。'
    '此声明只登记，不会自动发送。仅当用户明确要文件时，才按原规则追加 '
    '[[发文件]]绝对路径。登记与本机核验不证明内容正确或文件由本任务创建。\n'
)

_PRIVATE_DIRS = {'.runtime', '.git', '.ssh', '.aws', '.azure', '.gnupg',
                 '.codex', '.claude'}
_PRIVATE_NAMES = set(_SEND_DENY_NAMES) | {
    'secrets.json', 'secrets.yaml', 'secrets.yml', 'secret.key',
    'id_dsa', 'id_ecdsa', 'authorized_keys', 'known_hosts',
    'tasks.sqlite-wal', 'tasks.sqlite-shm', 'task-history-before-details.sqlite-wal',
    'task-history-before-details.sqlite-shm',
}
_HERE_NAMES = set(_SEND_DENY_HERE) | {
    'status.json', 'code_identity.json', 'bridge.log', 'backend.lock',
    'fixed_memory.md', 'fixed-memory.md', 'memory.md',
}
_TEXT_EXTS = {'.txt', '.md', '.csv', '.tsv', '.log', '.rst', '.tex', '.bib',
              '.ris', '.nbib', '.html', '.htm', '.svg', '.xml', '.yaml', '.yml',
              '.py', '.js', '.ts', '.css', '.cs', '.sql', '.ps1', '.sh', '.bat',
              '.cmd', '.ini'}
_OFFICE_PARTS = {'.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml',
                 '.pptx': 'ppt/presentation.xml'}


class ArtifactError(Exception):
    """A bounded verifier rejection; the code is safe to persist."""
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _record(path='', label=''):
    return {'path': path, 'name': os.path.basename(path) if path else '',
            'label': label, 'status': 'invalid', 'reason': '', 'format': '',
            'size': None, 'mtime_ns': None, 'sha256': ''}


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _within(path, root):
    try:
        return os.path.commonpath((_norm(path), _norm(root))) == _norm(root)
    except (ValueError, OSError):
        return False


def _limits(max_files, max_mb):
    try:
        count = min(MAX_DECLARATIONS, max(1, int(max_files)))
        # Zero in legacy send_file.max_mb means unlimited. This verifier always
        # has a hard resource bound even when that transport setting is zero.
        mb = float(max_mb or 20)
        if not 0 < mb < float('inf'):
            raise ValueError()
        cap = min(MAX_FILE_BYTES, max(1, int(mb * 1024 * 1024)))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('产物核验数量/大小限制无效') from None
    return count, cap


def _reparse(path):
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & 0x400)


def _check_components(path):
    # Check every existing ancestor, including the allowed root itself. Windows
    # junctions are reparse points even when os.path.islink returns False.
    current = path
    while True:
        try:
            if _reparse(current):
                raise ArtifactError('rejected', '路径包含符号链接或重解析点，不能登记或取回。')
        except FileNotFoundError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def _private_name(name):
    low = name.casefold()
    return (low in _PRIVATE_NAMES or low.endswith(_SEND_DENY_EXT)
            or low == '.env' or low.startswith('.env.')
            or low.startswith(('credentials.', 'credentials-', 'secrets.'))
            or low.startswith(('tasks.sqlite-', 'deepseek-api.', 'claude-api-sessions.')))


def _check_path(path, roots, here=None, protected_paths=()):
    if not isinstance(path, str) or not path or len(path) > 2000:
        raise ArtifactError('rejected', '必须提供长度不超过 2000 字的绝对文件路径。')
    if path != path.strip() or any(ord(ch) < 32 for ch in path):
        raise ArtifactError('rejected', '路径含控制字符或两端空白。')
    win = path.replace('/', '\\')
    # Device paths, UNC, alternate data streams, drive-relative paths and
    # ambiguous Windows suffixes must not create a second path interpretation.
    if win.startswith('\\\\') or not os.path.isabs(path):
        raise ArtifactError('rejected', '仅支持本机完整绝对路径，不支持网络/设备或相对路径。')
    drive, tail = ntpath.splitdrive(win)
    parts = tail.split('\\')
    if ':' in tail or any(p == '..' or (p and p[-1] in ' .') for p in parts):
        raise ArtifactError('rejected', '路径含上级跳转、数据流或含糊的文件名。')
    # On non-Windows hosts, do not interpret a Windows-looking declaration as
    # an accidental relative local filename.
    if os.name != 'nt' and drive:
        raise ArtifactError('rejected', '路径不是当前主机的绝对路径。')
    full = os.path.abspath(path)
    if not any(_within(full, os.fspath(root)) for root in roots if root):
        raise ArtifactError('rejected', '文件不在允许的工作目录范围内。')
    if any(part.casefold() in _PRIVATE_DIRS for part in parts if part):
        raise ArtifactError('rejected', '凭据目录或助手运行数据不能作为任务产物。')
    if _private_name(os.path.basename(full)):
        raise ArtifactError('rejected', '凭据、密钥或任务数据库不能作为任务产物。')
    if here and _within(full, os.fspath(here)):
        name = os.path.basename(full).casefold()
        # These names remain private in bridge subdirectories and backups.
        if name in _HERE_NAMES or name.startswith(('bridge.log.', 'config.json.',
                                                  'sessions.json.', 'status.json.')):
            raise ArtifactError('rejected', '助手配置、固定记忆或运行数据不能作为任务产物。')
    for protected in protected_paths or ():
        if protected and _within(full, os.fspath(protected)):
            raise ArtifactError('rejected', '文件属于助手明确保护的凭据或运行数据。')
    _check_components(full)
    # A junction raced into an ancestor changes the real path. Do not accept
    # even an in-scope redirection: the declaration must name the actual file.
    if _norm(os.path.realpath(full)) != _norm(full):
        raise ArtifactError('rejected', '路径解析后发生重定向，不能登记或取回。')
    return full


def _windows_final_path(fd):
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    call = api.GetFinalPathNameByHandleW
    call.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    call.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    size = call(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
    if not size or size >= len(buf):
        raise OSError(ctypes.get_last_error(), '无法核验实际打开的文件路径')
    value = buf.value
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


@contextmanager
def _open_file(path):
    if os.name == 'nt':
        import msvcrt
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        create = api.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        # FILE_SHARE_READ excludes writers/deleters while this version is read.
        # OPEN_REPARSE_POINT lets fstat reject a raced-in final-component link.
        handle = create(path, 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), '无法独占读取当前文件版本')
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            api.CloseHandle.argtypes = (wintypes.HANDLE,)
            api.CloseHandle(handle)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ArtifactError('invalid', '只能登记普通文件，不能登记目录或设备。')
        if getattr(opened, 'st_file_attributes', 0) & 0x400:
            raise ArtifactError('rejected', '实际打开的文件是重解析点。')
        if getattr(opened, 'st_nlink', 1) > 1:
            raise ArtifactError('rejected', '文件存在硬链接，不能可靠排除凭据别名。')
        actual = (_windows_final_path(source.fileno()) if os.name == 'nt'
                  else os.path.realpath('/proc/self/fd/%d' % source.fileno())
                  if os.path.exists('/proc/self/fd') else os.path.realpath(path))
        if _norm(actual) != _norm(path):
            raise ArtifactError('rejected', '实际打开的文件路径与声明不一致。')
        yield source


def _fingerprint(info):
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _read_version(path, cap, roots, here, protected_paths):
    full = _check_path(path, roots, here, protected_paths)
    info = os.stat(full, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactError('invalid', '只能登记普通文件，不能登记目录或设备。')
    if info.st_size <= 0:
        raise ArtifactError('invalid', '文件为空，不能登记为可取回的产物。')
    if info.st_size > cap:
        raise ArtifactError('rejected', '文件超过产物核验大小上限。')
    with _open_file(full) as source:
        before = os.fstat(source.fileno())
        if _fingerprint(before) != _fingerprint(info):
            raise ArtifactError('changed', '文件在核验开始前发生变化，请重新登记。')
        raw = source.read(cap + 1)
        after = os.fstat(source.fileno())
        if len(raw) > cap:
            raise ArtifactError('rejected', '文件超过产物核验大小上限。')
        if len(raw) != before.st_size or _fingerprint(before) != _fingerprint(after):
            raise ArtifactError('changed', '文件在读取期间发生变化，请重新登记。')
        if _fingerprint(os.stat(full, follow_symlinks=False)) != _fingerprint(after):
            raise ArtifactError('changed', '文件在核验期间被替换，请重新登记。')
        _check_path(full, roots, here, protected_paths)
    return full, raw, after


def _text(raw):
    try:
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            value = raw.decode('utf-16')
        else:
            try:
                value = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                value = raw.decode('gb18030')
    except UnicodeError:
        raise ArtifactError('invalid', '文本编码无法读取。') from None
    if '\x00' in value:
        raise ArtifactError('invalid', '文本文件包含二进制空字符。')
    return value


def _xml(raw):
    # Avoid internal entity expansion and external-resource declarations.
    text = _text(raw)
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ArtifactError('invalid', 'XML 含实体或外部文档声明，未进行安全格式核验。')
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError):
        raise ArtifactError('invalid', 'XML 基础结构无法解析。') from None


def _archive(raw, ext, cap):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        with archive:
            members = archive.infolist()
            if not members or len(members) > MAX_ARCHIVE_ENTRIES:
                raise ArtifactError('invalid', '压缩包为空或成员数量超过核验上限。')
            expanded = 0
            names = set()
            for entry in members:
                name = entry.filename.replace('\\', '/')
                parts = name.split('/')
                if (name.startswith('/') or ntpath.splitdrive(name)[0]
                        or ':' in name or any(p == '..' for p in parts)
                        or any(ord(c) < 32 for c in name)):
                    raise ArtifactError('rejected', '压缩包包含越界或含糊的成员路径。')
                if name.casefold() in names:
                    raise ArtifactError('invalid', '压缩包含重复成员名称。')
                names.add(name.casefold())
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ArtifactError('rejected', '压缩包包含链接或设备成员。')
                if any(p.casefold() in _PRIVATE_DIRS for p in parts) or _private_name(parts[-1]):
                    raise ArtifactError('rejected', '压缩包包含凭据或运行数据成员。')
                if parts[-1].casefold() in _HERE_NAMES:
                    raise ArtifactError('rejected', '压缩包包含助手配置或运行数据成员。')
                if entry.flag_bits & 1:
                    raise ArtifactError('invalid', '加密压缩包无法完成基础核验。')
                if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ArtifactError('invalid', '压缩算法不在有界核验范围内。')
                expanded += entry.file_size
                if (expanded > min(MAX_ARCHIVE_EXPANDED, max(cap, cap * 10))
                        or entry.file_size > MAX_ARCHIVE_EXPANDED
                        or entry.file_size > max(entry.compress_size, 1) * MAX_ARCHIVE_RATIO):
                    raise ArtifactError('rejected', '压缩包展开大小或压缩比超过核验上限。')
            # Read streams without extracting. ZipFile validates CRC at EOF.
            # Independently count actual bytes instead of trusting ZIP metadata.
            seen = 0
            essential = _OFFICE_PARTS.get(ext)
            required = {'[Content_Types].xml', essential} if essential else set()
            found = set()
            for entry in members:
                payload = bytearray() if entry.filename in required else None
                actual = 0
                with archive.open(entry) as source:
                    while True:
                        block = source.read(65536)
                        if not block:
                            break
                        actual += len(block)
                        seen += len(block)
                        if seen > min(MAX_ARCHIVE_EXPANDED, max(cap, cap * 10)) or actual > entry.file_size:
                            raise ArtifactError('rejected', '压缩包实际展开量超过核验上限。')
                        if payload is not None:
                            if actual > 16 * 1024 * 1024:
                                raise ArtifactError('rejected', 'Office 核心 XML 超过核验上限。')
                            payload.extend(block)
                if actual != entry.file_size:
                    raise ArtifactError('invalid', '压缩包成员长度不一致。')
                if payload is not None:
                    _xml(bytes(payload))
                    found.add(entry.filename)
            if required - found:
                raise ArtifactError('invalid', 'Office 文件缺少核心包结构。')
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError,
            EOFError, ValueError, OSError):
        raise ArtifactError('invalid', '压缩包损坏或无法安全读取。') from None
    return 'office-package' if ext in _OFFICE_PARTS else 'zip'


def _format(raw, path, cap):
    ext = os.path.splitext(path)[1].casefold()
    if ext in _OFFICE_PARTS or ext == '.zip':
        return _archive(raw, ext, cap)
    if ext == '.json':
        try:
            json.loads(_text(raw))
        except (ValueError, RecursionError):
            raise ArtifactError('invalid', 'JSON 基础结构无法解析。') from None
        return 'json'
    if ext in ('.xml', '.svg'):
        _xml(raw)
        return 'xml'
    if ext in _TEXT_EXTS or ext == '':
        _text(raw)
        return 'text'
    if ext == '.pdf':
        if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-4096:]:
            raise ArtifactError('invalid', 'PDF 缺少文件头或结束标记。')
        return 'pdf-header'
    if ext == '.png':
        if (len(raw) < 45 or not raw.startswith(b'\x89PNG\r\n\x1a\n')
                or raw[12:16] != b'IHDR' or b'IEND' not in raw[-16:]
                or not all(struct.unpack('>II', raw[16:24]))):
            raise ArtifactError('invalid', 'PNG 基础文件头、尺寸或结束结构无效。')
        return 'image-header'
    if ext in ('.jpg', '.jpeg'):
        if len(raw) < 4 or not raw.startswith(b'\xff\xd8\xff') or not raw.rstrip().endswith(b'\xff\xd9'):
            raise ArtifactError('invalid', 'JPEG 文件头或结束标记无效。')
        return 'image-header'
    if ext == '.gif':
        if (len(raw) < 14 or raw[:6] not in (b'GIF87a', b'GIF89a')
                or raw[-1:] != b';' or not all(struct.unpack('<HH', raw[6:10]))):
            raise ArtifactError('invalid', 'GIF 文件头、尺寸或结束标记无效。')
        return 'image-header'
    if ext == '.bmp':
        if len(raw) < 26 or raw[:2] != b'BM' or struct.unpack('<I', raw[2:6])[0] != len(raw):
            raise ArtifactError('invalid', 'BMP 文件头或长度无效。')
        return 'image-header'
    if ext == '.webp':
        if (len(raw) < 20 or raw[:4] != b'RIFF' or raw[8:12] != b'WEBP'
                or struct.unpack('<I', raw[4:8])[0] + 8 != len(raw)):
            raise ArtifactError('invalid', 'WebP 文件头或长度无效。')
        return 'image-header'
    return 'unverified'


def _verify(path, roots, *, label='', workdir=None, here=None,
            protected_paths=(), max_mb=100):
    # workdir is accepted for a consistent caller interface; declarations are
    # deliberately absolute and must never be resolved against model context.
    _, cap = _limits(MAX_DECLARATIONS, max_mb)
    record = _record(path if isinstance(path, str) else '', str(label)[:MAX_LABEL_CHARS])
    try:
        full, raw, info = _read_version(path, cap, roots, here, protected_paths)
        record.update(path=full, name=os.path.basename(full), size=info.st_size,
                      mtime_ns=info.st_mtime_ns, sha256=hashlib.sha256(raw).hexdigest())
        record['format'] = _format(raw, full, cap)
        detail = ('基础读取通过，未校验此文件格式' if record['format'] == 'unverified'
                  else '文件头基础检查通过，未完整验证可打开' if record['format'] in ('pdf-header', 'image-header')
                  else '基础格式结构核验通过，未完整验证可打开')
        record.update(status='valid', reason='文件存在、非空且可读；%s；不证明内容正确或由本任务创建。' % detail)
        return record, raw
    except ArtifactError as exc:
        record.update(status=exc.status, reason=exc.reason)
    except FileNotFoundError:
        record.update(status='missing', reason='文件已不存在或尚未生成。')
    except (OSError, ValueError, OverflowError):
        record.update(status='unreadable', reason='无法安全读取当前文件版本，可能被占用、移动或没有读取权限。')
    except (RecursionError, MemoryError):
        record.update(status='invalid', reason='文件结构超过基础核验处理能力。')
    return record, None


def validate_artifact(path, roots, **options):
    """Return a JSON-compatible verification receipt; never raise for a file."""
    return _verify(path, roots, **options)[0]


def _load_marker(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    if len(payload) > MAX_MARKER_CHARS:
        raise ValueError('marker too long')
    value = json.loads(payload, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) - {'path', 'label'}
            or not isinstance(value.get('path'), str)
            or not isinstance(value.get('label', ''), str)
            or len(value.get('label', '')) > MAX_LABEL_CHARS
            or any(ord(c) < 32 for c in value.get('label', ''))):
        raise ValueError('invalid declaration')
    return value


def strip_artifact_marks(text):
    """Remove declarations without any file I/O (including failed results).

    The second return value contains only syntactically valid declarations,
    capped at 20. They have not been verified and cannot be treated as receipts.
    collect_artifacts independently records malformed/overflow declarations.
    """
    body, declarations = [], []
    for line in str(text or '').splitlines():
        trimmed = line.strip()
        if not trimmed.startswith(MARK_ARTIFACT):
            body.append(line)
            continue
        if len(declarations) >= MAX_DECLARATIONS:
            continue
        try:
            declarations.append(_load_marker(trimmed[len(MARK_ARTIFACT):].strip()))
        except (ValueError, RecursionError, MemoryError):
            pass
    return '\n'.join(body).strip(), declarations


def collect_artifacts(text, roots, *, max_files=20, max_mb=100, **options):
    """Strip all marker lines, and verify only explicit declarations, in order.

    At most max_files records plus one overflow receipt are returned. Invalid
    markers produce explicit receipts, with no untrusted payload echoed back.
    Duplicate absolute paths share one receipt. Plain text paths are ignored.
    """
    count, _ = _limits(max_files, max_mb)
    body, records, seen = [], [], set()
    excess = 0
    for line in str(text or '').splitlines():
        trimmed = line.strip()
        if not trimmed.startswith(MARK_ARTIFACT):
            body.append(line)
            continue
        if len(records) >= count:
            excess += 1
            continue
        try:
            declaration = _load_marker(trimmed[len(MARK_ARTIFACT):].strip())
        except (ValueError, RecursionError, MemoryError):
            record = _record()
            record['reason'] = '产物声明格式无效；须为单行 JSON，含绝对 path 和简短 label。'
            records.append(record)
            continue
        key = _norm(declaration['path'])
        if key in seen:
            continue
        seen.add(key)
        if any(part.startswith('.wechat-batch-inputs-') for part in declaration['path'].replace('\\', '/').split('/')):
            record = _record(declaration['path'], declaration.get('label', ''))
            record.update(status='rejected', reason='批次输入副本在任务结束后清理，不能登记为交付产物；请另存结果文件。')
            records.append(record)
            continue
        records.append(validate_artifact(declaration['path'], roots,
                                        label=declaration.get('label', ''),
                                        max_mb=max_mb, **options))
    if excess:
        record = _record()
        record.update(status='rejected', reason='另有 %d 条产物声明超过本次 %d 个的上限，未登记或读取。' % (excess, count))
        records.append(record)
    return '\n'.join(body).strip(), records


def _revalidate(record, roots, options):
    if not isinstance(record, dict):
        result = _record()
        result.update(status='rejected', reason='产物记录无效。')
        return result, None
    if record.get('status') != 'valid' or not all(record.get(k) is not None for k in ('size', 'mtime_ns')) or not record.get('sha256'):
        result = dict(record)
        result.update(status='rejected', reason='此产物未通过原始基础核验，不能按任务取回；请检查后重新登记。')
        return result, None
    result, raw = _verify(record.get('path'), roots, label=record.get('label', ''), **options)
    if result.get('sha256') and any(result.get(key) != record.get(key) for key in ('path', 'size', 'mtime_ns', 'sha256')):
        # Do not overwrite the original persisted receipt with the new version.
        result = dict(record)
        result.update(status='changed', reason='文件与任务登记时的版本不同，未发送；请检查当前文件后重新登记或明确按路径取回。')
        return result, None
    if result['status'] != 'valid':
        return result, None
    result['reason'] = '当前内容指纹与任务登记版本一致；不证明内容正确或由本任务创建。'
    return result, raw


def revalidate_artifact(record, roots, **options):
    """Compare the current full-file hash, size and mtime with the saved receipt."""
    return _revalidate(record, roots, options)[0]


def read_verified_artifact(record, roots, **options):
    """Read the recorded version for deterministic checks without writing a copy.

    The bytes and receipt come from the same guarded read as file delivery.
    A version mismatch never exposes replacement bytes to the checker.
    """
    return _revalidate(record, roots, options)


@contextmanager
def verified_artifact_snapshot(record, roots, staging_dir, *, staging_roots=None,
                               task_id=None, registry_dir=None, snapshot_kind='artifact', **options):
    """Yield (receipt, immutable-version path or None), deleting it afterwards.

    staging_dir must be a trusted, bridge-owned directory under staging_roots
    (default: source roots). The caller checks permission against the original path and
    sends this snapshot with the original filename. This prevents a later edit
    of the original path from silently sending another version.
    """
    checked, raw = _revalidate(record, roots, options)
    if raw is None:
        yield checked, None
        return
    directory = os.path.abspath(os.fspath(staging_dir))
    trusted_roots = roots if staging_roots is None else staging_roots
    if not any(_within(directory, os.fspath(root)) for root in trusted_roots if root):
        checked = dict(checked, status='rejected', reason='产物发送临时目录不在允许范围内。')
        yield checked, None
        return
    try:
        os.makedirs(directory, exist_ok=True)
        _check_components(directory)
        if _norm(directory) != _norm(os.path.realpath(directory)):
            raise ArtifactError('rejected', '产物发送临时目录包含路径重定向。')
    except (OSError, ValueError, ArtifactError):
        checked = dict(checked, status='unreadable', reason='无法创建安全的产物发送快照，未发送。')
        yield checked, None
        return
    registry_dir = registry_dir or os.path.join(
        os.fspath(options.get('here') or roots[0]), '.runtime', 'snapshot-lifecycle')
    manager = managed_snapshot(directory, checked['name'], registry_dir=registry_dir,
                               task_id=task_id, kind=snapshot_kind)
    try:
        snapshot = manager.__enter__()
    except (OSError, ValueError):
        checked = dict(checked, status='unreadable', reason='无法创建安全的产物发送快照，未发送。')
        yield checked, None
        return
    try:
        try:
            with open(snapshot, 'xb') as destination:
                destination.write(raw)
        except OSError:
            checked = dict(checked, status='unreadable', reason='无法写入产物发送快照，未发送。')
            snapshot = None
        yield checked, snapshot
    finally:
        manager.__exit__(None, None, None)
