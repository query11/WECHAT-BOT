"""Owner-scoped attachment batches and version-frozen, temporary task inputs.

Only callers' inbox receipts are accepted; text paths never discover inputs.
This module does not run a model, open documents, send files or recover tasks.
Construct one store at backend startup: unfinished intakes become interrupted.
"""
from contextlib import ExitStack, contextmanager
import json
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

import task_artifacts as artifacts

MAX_BATCH_FILES = 12
MAX_BATCH_BYTES = 100 * 1024 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
_FINGERPRINT = re.compile(r'^[0-9a-f]{64}$')
_FIELDS = ('path', 'name', 'label', 'status', 'reason', 'format', 'size',
           'mtime_ns', 'sha256', 'uid', 'orig', 'inbox_name', 'received_at', 'role')


class BatchError(ValueError):
    """A user-visible rejection, containing no raw database or file payload."""


def _owner(uid):
    if not isinstance(uid, str) or not uid or len(uid) > 300:
        raise BatchError('附件批次的用户标识无效。')
    return uid


def _name(name):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise BatchError('批次名称须为 1 至 80 个字。')
    if any(ord(c) < 32 for c in name):
        raise BatchError('批次名称不能含控制字符或换行。')
    return name.strip()


def _valid_record(record, uid=None):
    if not isinstance(record, dict) or record.get('status') != 'valid':
        raise BatchError('输入附件未通过落盘与版本核验。')
    path = record.get('path')
    if not isinstance(path, str) or not os.path.isabs(path):
        raise BatchError('附件记录的路径无效，不能开始批次任务。')
    if uid is not None and record.get('uid') != uid:
        raise BatchError('附件记录不属于当前用户。')
    size, stamp = record.get('size'), record.get('mtime_ns')
    if (isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INPUT_BYTES
            or isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0
            or not isinstance(record.get('sha256'), str)
            or not _FINGERPRINT.fullmatch(record['sha256'])):
        raise BatchError('附件指纹无效或超过单文件 20 MB 上限。')
    out = {key: record[key] for key in _FIELDS if key in record}
    # The actual path determines a filename, never a user-controlled JSON name.
    out['name'] = os.path.basename(path)
    out['role'] = 'template' if record.get('role') == 'template' else 'input'
    for key in ('label', 'reason', 'format', 'orig', 'inbox_name'):
        if key in out:
            if not isinstance(out[key], str) or len(out[key]) > 2000:
                raise BatchError('附件元数据格式无效。')
    return out


def _bounded_records(records, uid=None):
    if not isinstance(records, (list, tuple)) or not 0 < len(records) <= MAX_BATCH_FILES:
        raise BatchError('一个批次须包含 1 至 12 个附件。')
    out = [_valid_record(record, uid) for record in records]
    paths = [os.path.normcase(os.path.abspath(record['path'])) for record in out]
    if len(set(paths)) != len(paths):
        raise BatchError('批次输入中含重复文件，请检查附件清单。')
    if sum(record['size'] for record in out) > MAX_BATCH_BYTES:
        raise BatchError('批次附件总量超过 100 MB 上限。')
    return out


def collect_input_records(entries, inbox_dir, roots, uid, **security_options):
    """Verify exactly these registered inbox entries for this owner, atomically.

    entries must come from InboxStore.all()/confirmed download receipts. The
    registered basename is the only path component; an entry's ``path`` is
    ignored. Any malformed, foreign, missing or changed entry rejects all.
    """
    uid = _owner(uid)
    if not isinstance(entries, (list, tuple)) or not 0 < len(entries) <= MAX_BATCH_FILES:
        raise BatchError('一个批次须选择 1 至 12 条真实收件记录。')
    directory = os.path.abspath(os.fspath(inbox_dir))
    records = []
    options = dict(security_options)
    options['max_mb'] = min(20, float(options.get('max_mb') or 20))
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('uid') != uid:
            raise BatchError('所选收件记录不属于当前用户。')
        name = entry.get('name')
        if (not isinstance(name, str) or not name or name != os.path.basename(name)
                or '/' in name or '\\' in name or ':' in name or name in ('.', '..')
                or any(ord(c) < 32 for c in name)):
            raise BatchError('收件登记名无效，不能根据文字路径建立批次。')
        original = entry.get('orig') or name
        if not isinstance(original, str) or len(original) > 2000:
            raise BatchError('收件原文件名格式无效。')
        # Both registered and display names can carry private-file hints.
        original_base = original.replace('\\', '/').split('/')[-1]
        if (artifacts._private_name(original_base)
                or original_base.casefold() in artifacts._HERE_NAMES):
            raise BatchError('凭据、密钥或助手运行数据不能作为批次附件。')
        try:
            size = entry['bytes']
            stamp = entry['at']
            if (isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INPUT_BYTES
                    or isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                    or not math.isfinite(stamp) or stamp < 0):
                raise ValueError()
        except (KeyError, ValueError, TypeError, OverflowError):
            raise BatchError('收件登记的大小或时间无效。') from None
        record = artifacts.validate_artifact(os.path.join(directory, name), roots,
                                             label=original[:80], **options)
        if record.get('status') != 'valid':
            raise BatchError('附件无法安全读取：' + record.get('reason', '基础核验失败。'))
        if record.get('size') != size:
            raise BatchError('附件大小与收件登记不同，请检查后重新接收。')
        # Inbox MD5 can be absent on older receipts. If present, verify actual
        # bytes using a checked snapshot, never a second unguarded path open.
        expected_md5 = entry.get('md5') or ''
        if not isinstance(expected_md5, str) or (expected_md5 and not re.fullmatch(r'[0-9a-fA-F]{32}', expected_md5)):
            raise BatchError('收件登记的 MD5 格式无效。')
        if expected_md5:
            import hashlib
            checked, raw = artifacts._revalidate(record, roots, options)
            if raw is None or checked.get('status') != 'valid':
                raise BatchError('附件在核验期间变化，请检查后重新接收。')
            if hashlib.md5(raw).hexdigest().lower() != expected_md5.lower():
                raise BatchError('附件内容与收件时的指纹不同，请检查后重新接收。')
        record.update(uid=uid, orig=original, inbox_name=name,
                      received_at=float(stamp), role='input')
        records.append(record)
    return _bounded_records(records, uid)


@contextmanager
def frozen_batch_inputs(records, roots, workdir, *, task_id=None, registry_dir=None, **security_options):
    """Yield a complete manifest of verified temporary copies, then remove it.

    Every original must match the saved size/mtime/SHA256. All snapshots are
    retained for the context's duration, so later uploads/edits cannot change
    this task's input set. Failure of one input prevents yielding any inputs.
    """
    records = _bounded_records(records)
    directory = os.path.abspath(os.fspath(workdir))
    if not any(artifacts._within(directory, os.fspath(root)) for root in roots if root):
        raise BatchError('批次输入临时目录不在允许的工作目录范围内。')
    try:
        artifacts._check_components(directory)
        if artifacts._norm(directory) != artifacts._norm(os.path.realpath(directory)):
            raise BatchError('批次输入临时目录包含路径重定向。')
        if not os.path.isdir(directory):
            raise BatchError('批次工作目录不存在，无法创建输入快照。')
    except (OSError, artifacts.ArtifactError):
        raise BatchError('无法创建安全的批次输入临时目录。') from None
    options = dict(security_options)
    options['max_mb'] = min(20, float(options.get('max_mb') or 20))
    with ExitStack() as stack:
        manifest = []
        for index, record in enumerate(records, 1):
            checked, snapshot = stack.enter_context(artifacts.verified_artifact_snapshot(
                record, roots, directory, staging_roots=[directory],
                task_id=task_id, registry_dir=registry_dir,
                snapshot_kind='batch', **options))
            if snapshot is None or checked.get('status') != 'valid':
                raise BatchError('批次第 %d 个附件无法冻结：%s' % (
                    index, checked.get('reason', '当前版本不可用。')))
            frozen = dict(record, original_path=record['path'],
                          original_mtime_ns=record['mtime_ns'], path=snapshot,
                          mtime_ns=os.stat(snapshot).st_mtime_ns,
                          index=record.get('index', index))
            manifest.append(frozen)
        yield manifest


class AttachmentBatchStore:
    """SQLite batch catalog sharing TaskJournal's file, without task recovery."""

    def __init__(self, journal_path):
        self.path = str(journal_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS attachment_batches ('
                       'id TEXT PRIMARY KEY, uid TEXT NOT NULL, name TEXT NOT NULL, '
                       'state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, '
                       "task_id TEXT NOT NULL DEFAULT '', intake_mode TEXT NOT NULL DEFAULT 'live')")
            columns = {row[1] for row in db.execute('PRAGMA table_info(attachment_batches)')}
            if 'intake_mode' not in columns:
                db.execute("ALTER TABLE attachment_batches ADD COLUMN intake_mode TEXT NOT NULL DEFAULT 'live'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS attachment_batch_open_owner "
                       "ON attachment_batches(uid) WHERE state='open'")
            db.execute('CREATE TABLE IF NOT EXISTS attachment_batch_files ('
                       'batch_id TEXT NOT NULL, position INTEGER NOT NULL, record TEXT NOT NULL, '
                       'PRIMARY KEY(batch_id,position))')
            db.execute('CREATE TABLE IF NOT EXISTS attachment_batch_intakes ('
                       'uid TEXT NOT NULL, message_id TEXT NOT NULL, batch_id TEXT NOT NULL, '
                       'state TEXT NOT NULL, failed INTEGER NOT NULL DEFAULT 0, '
                       "errors TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL, "
                       'PRIMARY KEY(uid,message_id))')
            columns = {row[1] for row in db.execute('PRAGMA table_info(attachment_batch_intakes)')}
            if 'dismissed' not in columns:
                db.execute('ALTER TABLE attachment_batch_intakes ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS attachment_batch_dialogs ('
                       'uid TEXT PRIMARY KEY, batch_id TEXT NOT NULL, manifest TEXT NOT NULL, '
                       'plan TEXT NOT NULL, created REAL NOT NULL)')
            # Keep failed evidence: no silent seal after a restart mid-download.
            db.execute("UPDATE attachment_batch_intakes SET state='interrupted', failed=MAX(1,failed), "
                       "errors=?,updated=? WHERE state='pending'",
                       (json.dumps(['附件接收被重启中断，请补传缺失文件并核对清单。'], ensure_ascii=False), time.time()))

    @contextmanager
    def connect(self, read_only=False):
        if read_only:
            db = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=.05)
            db.execute('PRAGMA query_only=ON')
        else:
            db = sqlite3.connect(self.path, timeout=.1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _get(self, db, uid, ident):
        if str(ident).lower() in ('latest', 'last', '最近', '刚才'):
            row = db.execute('SELECT * FROM attachment_batches WHERE uid=? ORDER BY created DESC,id DESC LIMIT 1', (uid,)).fetchone()
        else:
            row = db.execute('SELECT * FROM attachment_batches WHERE uid=? AND id=?', (uid, str(ident))).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['files'] = []
        for file in db.execute('SELECT position,record FROM attachment_batch_files WHERE batch_id=? ORDER BY position', (row['id'],)):
            record = json.loads(file['record'])
            if not isinstance(record, dict):
                raise BatchError('批次附件清单损坏，请取消后重新建立。')
            result['files'].append(dict(record, index=file['position']))
        intakes = db.execute('SELECT rowid AS failure_id,state,failed,errors,dismissed FROM attachment_batch_intakes WHERE uid=? AND batch_id=? ORDER BY rowid', (uid, row['id'])).fetchall()
        result['pending'] = sum(intake['state'] == 'pending' for intake in intakes)
        result['failed'] = sum(intake['failed'] for intake in intakes if not intake['dismissed'])
        result['failures'] = [dict(id=intake['failure_id'], count=intake['failed'],
                                   errors=json.loads(intake['errors'])) for intake in intakes
                              if intake['failed'] and not intake['dismissed']]
        result['errors'] = [error for intake in result['failures'] for error in intake['errors']]
        return result

    def _require(self, db, uid, ident):
        batch = self._get(db, uid, ident)
        if batch is None:
            raise BatchError('找不到当前用户的这个附件批次。')
        return batch

    def get(self, uid, ident='latest'):
        with self.connect(read_only=True) as db:
            return self._get(db, _owner(uid), ident)

    def list(self, uid):
        uid = _owner(uid)
        with self.connect(read_only=True) as db:
            ids = [row[0] for row in db.execute('SELECT id FROM attachment_batches WHERE uid=? ORDER BY created DESC,id DESC LIMIT 10', (uid,))]
            return [self._get(db, uid, ident) for ident in ids]

    def unique_ready(self, uid):
        """Inspect all ready receipts, including older ones outside the display page."""
        uid = _owner(uid)
        with self.connect(read_only=True) as db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM attachment_batches WHERE uid=? AND state='ready' ORDER BY created DESC,id DESC LIMIT 2", (uid,))]
            return self._get(db, uid, ids[0]) if len(ids) == 1 else None

    def active(self, uid):
        uid = _owner(uid)
        with self.connect(read_only=True) as db:
            row = db.execute("SELECT id FROM attachment_batches WHERE uid=? AND state='open' AND intake_mode='live'", (uid,)).fetchone()
            return self._get(db, uid, row[0]) if row else None

    def create(self, uid, name, *, collect=True):
        uid, name = _owner(uid), _name(name)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM attachment_batches WHERE uid=? AND state='open'", (uid,)).fetchone():
                raise BatchError('已有正在收件的批次，请先封存或取消它。')
            ident, now = uuid.uuid4().hex, time.time()
            db.execute('DELETE FROM attachment_batch_dialogs WHERE uid=?', (uid,))
            db.execute('INSERT INTO attachment_batches(id,uid,name,state,created,updated,intake_mode) VALUES (?,?,?,?,?,?,?)', (ident, uid, name, 'open', now, now, 'live' if collect else 'selected'))
            return self._get(db, uid, ident)

    def begin_intake(self, uid, batchid, message_id):
        uid = _owner(uid)
        if not isinstance(message_id, (str, int)) or not str(message_id) or len(str(message_id)) > 300:
            raise BatchError('附件消息编号无效，无法保证不重复收件。')
        message_id = str(message_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, batchid)
            if batch['state'] != 'open':
                raise BatchError('这个批次已经停止收件，新附件不会加入。')
            if db.execute('SELECT 1 FROM attachment_batch_intakes WHERE uid=? AND message_id=?', (uid, message_id)).fetchone():
                return False
            now = time.time()
            db.execute('INSERT INTO attachment_batch_intakes(uid,message_id,batch_id,state,updated) VALUES (?,?,?,?,?)', (uid, message_id, batch['id'], 'pending', now))
            db.execute('UPDATE attachment_batches SET updated=? WHERE id=?', (now, batch['id']))
            return True

    def finish_intake(self, uid, batchid, message_id, records, failed=0):
        uid = _owner(uid)
        if isinstance(failed, bool) or not isinstance(failed, int) or failed < 0:
            raise BatchError('附件失败计数无效。')
        if not isinstance(records, (list, tuple)) or len(records) > MAX_BATCH_FILES:
            raise BatchError('附件接收清单格式无效。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, batchid)
            intake = db.execute('SELECT state FROM attachment_batch_intakes WHERE uid=? AND message_id=? AND batch_id=?', (uid, str(message_id), batch['id'])).fetchone()
            if intake is None:
                raise BatchError('找不到这条附件的批次接收记录。')
            if intake['state'] == 'complete' or intake['state'] == 'cancelled':
                return batch
            if intake['state'] != 'pending':
                raise BatchError('这条附件接收已中断，不能在重启后继续加入批次。')
            now = time.time()
            if batch['state'] != 'open':
                # A late completion belongs to the original cancelled batch.
                db.execute("UPDATE attachment_batch_intakes SET state='cancelled',updated=? WHERE uid=? AND message_id=?", (now, uid, str(message_id)))
                return self._get(db, uid, batch['id'])
            valid, errors = [], []
            for record in records:
                try:
                    valid.append(_valid_record(record, uid))
                except BatchError as exc:
                    failed += 1
                    errors.append(str(exc))
            if failed:
                errors.insert(0, '%d 个附件接收或核验失败，请补传缺失文件并核对清单。' % failed)
            if not records and not failed:
                failed = 1
                errors.append('这条附件消息没有确认落盘的文件，请重新发送缺失附件。')
            existing = {os.path.normcase(record['path']): record for record in batch['files']}
            additions = []
            for record in valid:
                key = os.path.normcase(record['path'])
                if key in existing:
                    if any(record[field] != existing[key][field] for field in ('size', 'mtime_ns', 'sha256')):
                        failed += 1
                        errors.append('同名收件文件在批次内发生变化，请取消后重新建立。')
                    continue
                existing[key] = record
                additions.append(record)
            if (len(batch['files']) + len(additions) > MAX_BATCH_FILES
                    or sum(record['size'] for record in list(batch['files']) + additions) > MAX_BATCH_BYTES):
                failed += max(1, len(additions))
                errors.append('附件批次超过 12 个文件或总量 100 MB 上限，本条附件未加入。')
                additions = []
            for position, record in enumerate(additions, len(batch['files']) + 1):
                db.execute('INSERT INTO attachment_batch_files VALUES (?,?,?)', (batch['id'], position, json.dumps(record, ensure_ascii=False)))
            db.execute("UPDATE attachment_batch_intakes SET state='complete',failed=?,errors=?,updated=? WHERE uid=? AND message_id=?", (failed, json.dumps(errors, ensure_ascii=False), now, uid, str(message_id)))
            db.execute('UPDATE attachment_batches SET updated=? WHERE id=?', (now, batch['id']))
            return self._get(db, uid, batch['id'])

    @staticmethod
    def desktop_version(batch):
        value = {key: batch.get(key) for key in ('id', 'state', 'task_id', 'files', 'pending', 'failures', 'updated')}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    def _check_desktop_version(self, batch, expected_version):
        if expected_version is not None and expected_version != self.desktop_version(batch):
            raise BatchError('附件清单已经变化，请刷新并重新核对后操作。')

    def seal(self, uid, ident, *, expected_version=None):
        uid = _owner(uid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, ident)
            self._check_desktop_version(batch, expected_version)
            if batch['state'] == 'ready':
                return batch
            if batch['state'] != 'open':
                raise BatchError('只有正在收件的批次可以封存。')
            if batch['pending']:
                raise BatchError('还有附件正在下载，全部确认落盘后才能封存。')
            if batch['failed']:
                raise BatchError('批次含失败或中断的附件，请补传缺失文件并核对清单，再移除对应失败记录。')
            _bounded_records(batch['files'], uid)
            db.execute("UPDATE attachment_batches SET state='ready',updated=? WHERE id=?", (time.time(), batch['id']))
            return self._get(db, uid, batch['id'])

    def repair(self, uid, ident, action, index, *, expected_version=None):
        """Edit only an open manifest; retain files on disk and failed receipt evidence."""
        uid = _owner(uid)
        if action not in ('remove', 'dismiss') or type(index) is not int or index < 1:
            raise BatchError('请使用清单中的附件序号或失败记录编号。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, ident)
            self._check_desktop_version(batch, expected_version)
            if batch['state'] != 'open' or batch['task_id']:
                raise BatchError('只有未确认的批次可以修改；已确认的清单保持固定。')
            if batch['pending']:
                raise BatchError('还有附件正在接收，请接收结束后再修改清单。')
            if action == 'dismiss':
                changed = db.execute('UPDATE attachment_batch_intakes SET dismissed=1 '
                                     'WHERE uid=? AND batch_id=? AND rowid=? AND failed>0 AND dismissed=0',
                                     (uid, batch['id'], index))
                if not changed.rowcount:
                    raise BatchError('这条失败记录不存在或已经移除，请查看最新清单。')
            else:
                if index > len(batch['files']):
                    raise BatchError('附件序号不在清单中。')
                files = [r for r in batch['files'] if r['index'] != index]
                db.execute('DELETE FROM attachment_batch_files WHERE batch_id=?', (batch['id'],))
                for position, record in enumerate(files, 1):
                    db.execute('INSERT INTO attachment_batch_files VALUES (?,?,?)',
                               (batch['id'], position, json.dumps(_valid_record(record, uid), ensure_ascii=False)))
            db.execute('DELETE FROM attachment_batch_dialogs WHERE uid=? AND batch_id=?', (uid, batch['id']))
            db.execute('UPDATE attachment_batches SET updated=? WHERE id=?', (time.time(), batch['id']))
            return self._get(db, uid, batch['id'])

    @staticmethod
    def _manifest(batch):
        return json.dumps(batch['files'], ensure_ascii=False, sort_keys=True)

    def prepare_dialog(self, uid, ident, plan):
        """Save the exact reviewed files and task/template snapshot for 15 minutes."""
        uid = _owner(uid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, ident)
            if batch['state'] not in ('open', 'ready') or batch['pending'] or batch['failed']:
                raise BatchError('请先等待附件接收完成，并处理清单里的失败记录。')
            _bounded_records(batch['files'], uid)
            now = time.time()
            db.execute('DELETE FROM attachment_batch_dialogs WHERE created<?', (now - 900,))
            db.execute('INSERT OR REPLACE INTO attachment_batch_dialogs VALUES (?,?,?,?,?)',
                       (uid, batch['id'], self._manifest(batch), json.dumps(plan, ensure_ascii=False), now))
            return batch

    def dialog_batch(self, uid):
        with self.connect(read_only=True) as db:
            row = db.execute('SELECT batch_id FROM attachment_batch_dialogs WHERE uid=? AND created>=?',
                             (_owner(uid), time.time() - 900)).fetchone()
            return self._get(db, uid, row[0]) if row else None

    def confirm_dialog(self, uid):
        """Atomically seal only the reviewed manifest. A full queue may retry this plan."""
        uid = _owner(uid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM attachment_batch_dialogs WHERE uid=? AND created>=?',
                             (uid, time.time() - 900)).fetchone()
            if not row:
                raise BatchError('没有待确认的附件任务，或确认已超过 15 分钟。请重新发送“发完了，任务要求”。')
            batch = self._require(db, uid, row['batch_id'])
            if batch['state'] not in ('open', 'ready'):
                raise BatchError('这批附件已经使用或取消，不会重复执行。')
            if batch['pending'] or batch['failed'] or self._manifest(batch) != row['manifest']:
                raise BatchError('附件清单已经变化，请重新发送“发完了，任务要求”核对后再确认。')
            _bounded_records(batch['files'], uid)
            db.execute("UPDATE attachment_batches SET state='ready',updated=? WHERE id=?", (time.time(), batch['id']))
            return self._get(db, uid, batch['id']), json.loads(row['plan'])

    def cancel(self, uid, ident):
        uid = _owner(uid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, ident)
            if batch['state'] not in ('open', 'ready'):
                return False
            db.execute("UPDATE attachment_batches SET state='cancelled',updated=? WHERE id=?", (time.time(), batch['id']))
            return True

    def set_template(self, uid, ident, index):
        uid = _owner(uid)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise BatchError('模板编号须为附件清单中的正整数，0 可清除模板。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, ident)
            if batch['state'] != 'open' or batch['task_id']:
                raise BatchError('批次已封存、取消或绑定任务，不能修改模板。')
            if index > len(batch['files']):
                raise BatchError('这个模板编号不在批次附件清单中。')
            for record in batch['files']:
                record['role'] = 'template' if record['index'] == index else 'input'
                db.execute('UPDATE attachment_batch_files SET record=? WHERE batch_id=? AND position=?', (json.dumps(_valid_record(record, uid), ensure_ascii=False), batch['id'], record['index']))
            db.execute('UPDATE attachment_batches SET updated=? WHERE id=?', (time.time(), batch['id']))
            return self._get(db, uid, batch['id'])

    def create_recent(self, uid, name, records):
        uid, name = _owner(uid), _name(name)
        records = _bounded_records(records, uid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM attachment_batches WHERE uid=? AND state='open'", (uid,)).fetchone():
                raise BatchError('已有正在收件的批次，请先封存或取消它。')
            ident, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO attachment_batches(id,uid,name,state,created,updated,intake_mode) VALUES (?,?,?,?,?,?,?)', (ident, uid, name, 'open', now, now, 'selected'))
            for index, record in enumerate(records, 1):
                db.execute('INSERT INTO attachment_batch_files VALUES (?,?,?)', (ident, index, json.dumps(record, ensure_ascii=False)))
            return self._get(db, uid, ident)

    def bind_task(self, uid, ident, taskid):
        uid = _owner(uid)
        if not isinstance(taskid, str) or not taskid or len(taskid) > 100:
            raise BatchError('任务编号无效，无法绑定附件批次。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, ident)
            if batch['state'] == 'used' and batch['task_id'] == taskid:
                return batch
            if batch['state'] != 'ready' or batch['task_id']:
                raise BatchError('批次未封存或已绑定其他任务，不能重复执行。')
            _bounded_records(batch['files'], uid)
            db.execute("UPDATE attachment_batches SET state='used',task_id=?,updated=? WHERE id=?", (taskid, time.time(), batch['id']))
            return self._get(db, uid, batch['id'])

    def release_task(self, uid, ident, taskid):
        """Undo only this binding after a failed queue/receipt insertion.

        The caller must never release a task that started. No files or prior
        intakes change, and a competing/different task binding is preserved.
        """
        uid = _owner(uid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = self._require(db, uid, ident)
            if batch['state'] == 'ready' and not batch['task_id']:
                return True
            if batch['state'] != 'used' or batch['task_id'] != taskid:
                return False
            db.execute("UPDATE attachment_batches SET state='ready',task_id='',updated=? WHERE id=?", (time.time(), batch['id']))
            return True
