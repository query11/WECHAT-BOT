"""Durable receipts and opt-in recovery of unstarted model tasks. No transport tokens."""
from contextlib import contextmanager
import json
import sqlite3
import time
import uuid
from pathlib import Path

LABELS = {'queued': '等待执行', 'running': '正在处理', 'completed': '处理结束',
          'waiting': '等待确认', 'failed': '失败', 'cancelled': '已取消', 'uncertain': '结果不确定',
          'recoverable': '未开始，可手动恢复', 'interrupted': '已中断，请检查后重新发送'}
RESULT_RETENTION_SECONDS = 7 * 24 * 3600
RESOLUTIONS = {'checked': '已人工核对', 'abandoned': '已放弃恢复', 'archived': '已归档'}


def decode_result_checks(raw):
    """Optional evidence degrades explicitly without stranding valid artifacts."""
    try:
        if not isinstance(raw, str) or len(raw) > 64 * 1024:
            raise ValueError('invalid checks')
        checks = json.loads(raw or '[]')
        if (not isinstance(checks, list) or len(checks) > 12
                or any(not isinstance(check, dict)
                       or check.get('status') not in ('passed', 'failed', 'unverified')
                       or any(not isinstance(check.get(key), str) or len(check[key]) > limit
                              for key, limit in (('file', 255), ('kind', 40), ('detail', 300)))
                       or ('condition' in check and
                           (not isinstance(check['condition'], str) or len(check['condition']) > 1000))
                       for check in checks)):
            raise ValueError('invalid checks')
        return checks
    except (ValueError, TypeError, RecursionError):
        return [dict(file='', kind='', status='unverified',
                     detail='保存的检查结论不可读；有效产物仍可按原规则取回。')]


class TaskJournal:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, uid TEXT, message_id TEXT, kind TEXT, label TEXT, state TEXT, updated REAL)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
            if 'payload' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN payload TEXT')
            if 'summary' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN summary TEXT DEFAULT ''")
            if 'outputs' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN outputs TEXT DEFAULT '[]'")
            for name, declaration in (
                    ('execution_state', "TEXT DEFAULT ''"), ('delivery_state', "TEXT DEFAULT ''"),
                    ('artifacts', "TEXT DEFAULT '[]'"),
                    ('inputs', "TEXT DEFAULT ''"),
                    ('template', "TEXT DEFAULT ''"),
                    ('result_checks', "TEXT DEFAULT '[]'"),
                    ('resolution', "TEXT DEFAULT ''"), ('resolved_at', 'REAL DEFAULT 0'), ('revision', "TEXT DEFAULT ''"),
                    ('local_files', "TEXT DEFAULT ''")):
                if name not in columns:
                    db.execute('ALTER TABLE tasks ADD COLUMN ' + name + ' ' + declaration)
            db.execute('CREATE TABLE IF NOT EXISTS task_replies (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, body TEXT, created REAL, accepted_chunks INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0, state TEXT DEFAULT \'pending\', chunk_size INTEGER DEFAULT 1800)')
            db.execute('CREATE INDEX IF NOT EXISTS task_replies_task ON task_replies(task_id)')
            db.execute('CREATE TABLE IF NOT EXISTS task_revision_links ('
                       'task_id TEXT PRIMARY KEY, uid TEXT NOT NULL, parent_id TEXT NOT NULL, '
                       'root_id TEXT NOT NULL, created REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS task_revision_root ON task_revision_links(uid,root_id)')
            db.execute('CREATE TABLE IF NOT EXISTS received_messages (uid TEXT, message_id TEXT, state TEXT, label TEXT, updated REAL, PRIMARY KEY(uid,message_id))')
            # A crashed dispatch may already have changed an external application.
            # Preserve an uncertainty receipt instead of dispatching it again.
            stranded = db.execute("SELECT uid,message_id,label FROM received_messages WHERE state='processing' AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.uid=received_messages.uid AND tasks.message_id=received_messages.message_id)").fetchall()
            for uid, mid, label in stranded:
                db.execute('INSERT INTO tasks (id,uid,message_id,kind,label,state,updated,summary) VALUES (?,?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex, uid, mid, 'dispatch', label, 'uncertain', time.time(), '消息分发中断，结果未确认；不会自动重跑。'))
            db.execute("UPDATE received_messages SET state='failed' WHERE state='processing'")
            db.execute("UPDATE tasks SET state='recoverable', execution_state='recoverable' WHERE state='queued' AND kind IN ('task','batch_task','template_task','template_batch_task','revision_task','local_file_task') AND payload IS NOT NULL")
            db.execute("UPDATE tasks SET state='interrupted', execution_state=CASE WHEN execution_state IN ('completed','failed','cancelled') THEN execution_state ELSE 'interrupted' END, payload=NULL WHERE kind='inbox' AND state IN ('queued','running','waiting')")
            db.execute("UPDATE tasks SET state='uncertain', execution_state=CASE WHEN execution_state IN ('completed','failed','cancelled') THEN execution_state ELSE 'uncertain' END, updated=? WHERE state IN ('queued','running','waiting')", (time.time(),))
            # A persisted pending send cannot still be in flight after restart.
            # Keep known execution outcomes and chunk receipts for manual resend.
            db.execute("UPDATE task_replies SET state='unconfirmed' WHERE state='pending'")
            db.execute("UPDATE tasks SET delivery_state='unconfirmed' WHERE delivery_state='pending'")
            self._prune_replies(db)

    @contextmanager
    def connect(self, timeout=10, read_only=False):
        if read_only:
            db = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=timeout)
            db.execute('PRAGMA query_only=ON')
        else:
            db = sqlite3.connect(self.path, timeout=timeout)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, uid, message_id, kind, label, payload=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if message_id:
                found = db.execute('SELECT id FROM tasks WHERE uid=? AND message_id=? AND kind=?',
                                   (uid, str(message_id), kind)).fetchone()
                if found:
                    return found[0], False
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO tasks (id,uid,message_id,kind,label,state,updated,payload) VALUES (?,?,?,?,?,?,?,?)',
                       (ident, uid, str(message_id or ''), kind, label[:100], 'queued', time.time(), payload))
            self._prune_history(db)
            return ident, True

    def update(self, ident, state, summary=None, outputs=None, execution_state=None):
        if state not in LABELS:
            raise ValueError(state)
        with self.connect() as db:
            db.execute('UPDATE tasks SET state=?, execution_state=?, updated=?, payload=CASE WHEN ? IN (\'queued\',\'recoverable\') THEN payload ELSE NULL END, summary=COALESCE(?,summary), outputs=COALESCE(?,outputs) WHERE id=?', (state, execution_state or state, time.time(), state, str(summary)[:1000] if summary is not None else None, json.dumps([str(p)[:1000] for p in outputs[:20]],ensure_ascii=False) if outputs is not None else None, ident))

    @staticmethod
    def _saved_template(raw, uid):
        from task_templates import TemplateError, validate_snapshot, MAX_DEFINITION_JSON
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate field')
                result[key] = value
            return result
        try:
            if not isinstance(raw, str) or not raw or len(raw) > MAX_DEFINITION_JSON:
                raise ValueError('missing or oversized snapshot')
            return validate_snapshot(json.loads(raw, object_pairs_hook=pairs), uid)
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise TemplateError('原任务的模板快照无效，不能按普通文字任务恢复。') from None

    @staticmethod
    def _saved_inputs(raw, uid):
        from attachment_batches import BatchError, _bounded_records
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('duplicate field')
                    result[key] = value
                return result
            inputs = json.loads(raw, object_pairs_hook=pairs)
            if (not isinstance(inputs, dict) or set(inputs) != {'id', 'uid', 'name', 'files'}
                    or inputs['uid'] != uid
                    or not isinstance(inputs['id'], str) or len(inputs['id']) != 32
                    or any(c not in '0123456789abcdef' for c in inputs['id'])
                    or not isinstance(inputs['name'], str) or not inputs['name'].strip()
                    or len(inputs['name']) > 80 or any(ord(c) < 32 for c in inputs['name'])):
                raise ValueError('invalid manifest')
            _bounded_records(inputs['files'], uid)
            for index, record in enumerate(inputs['files'], 1):
                if type(record.get('index')) is not int or record['index'] != index:
                    raise ValueError('invalid file position')
            return inputs
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise BatchError('原任务的批次输入记录无效，不能按普通文字任务恢复。') from None

    def add_template(self, uid, message_id, body, snapshot):
        """Save the invocation and its independent, owner-checked definition."""
        from task_templates import TemplateError, validate_snapshot
        if not isinstance(body, str):
            raise TemplateError('模板任务的补充要求须为文字。')
        copied = validate_snapshot(snapshot, uid)
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute("SELECT id,template FROM tasks WHERE uid=? AND message_id=? AND kind='template_task'",
                               (uid, str(message_id))).fetchone() if message_id else None
            if found:
                return found[0], False, self._saved_template(found[1], uid)
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO tasks (id,uid,message_id,kind,label,state,updated,payload,template) VALUES (?,?,?,?,?,?,?,?,?)',
                       (ident, uid, str(message_id or ''), 'template_task',
                        ('任务模板 · ' + copied['name'] + (' · ' + body if body else ''))[:100],
                        'queued', time.time(), body, json.dumps(copied, ensure_ascii=False)))
            self._prune_history(db)
            return ident, True, copied

    def add_local_files(self, uid, message_id, snapshot):
        from desktop_file_tasks import validate_local_files
        copied = validate_local_files(snapshot, uid)
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM tasks WHERE uid=? AND (id=? OR message_id=?)',
                                  (uid, copied['id'], str(message_id))).fetchone()
            if existing:
                return existing[0], False, copied
            label = '文件任务 · ' + (copied['template']['name'] if copied['template'] else copied['body'])
            db.execute('INSERT INTO tasks(id,uid,message_id,kind,label,state,updated,payload,local_files,template) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (copied['id'], uid, str(message_id), 'local_file_task', label[:100], 'queued', time.time(),
                        copied['body'], json.dumps(copied, ensure_ascii=False),
                        json.dumps(copied['template'], ensure_ascii=False) if copied['template'] else ''))
            self._prune_history(db)
            return copied['id'], True, copied

    def add_revision(self, uid, message_id, snapshot):
        from task_revisions import RevisionError, validate_revision, ENDED
        copied = validate_revision(snapshot, uid)
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM tasks WHERE uid=? AND (id=? OR message_id=?)',
                                  (uid, copied['id'], str(message_id))).fetchone()
            if existing:
                return existing[0], False, copied
            parent = db.execute('SELECT state FROM tasks WHERE uid=? AND id=?', (uid, copied['parent_id'])).fetchone()
            if not parent or parent[0] not in ENDED:
                raise RevisionError('来源任务已不可用，请重新选择。')
            link = db.execute('SELECT root_id FROM task_revision_links WHERE uid=? AND task_id=?',
                              (uid, copied['parent_id'])).fetchone()
            if copied['root_id'] != (link[0] if link else copied['parent_id']):
                raise RevisionError('来源任务的版本关联已变化，请重新核对。')
            ident, now = copied['id'], time.time()
            db.execute('INSERT INTO tasks(id,uid,message_id,kind,label,state,updated,payload,revision) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (ident, uid, str(message_id), 'revision_task', ('继续修改 · '+copied['instructions'])[:100],
                        'queued', now, copied['instructions'], json.dumps(copied, ensure_ascii=False)))
            db.execute('INSERT INTO task_revision_links VALUES (?,?,?,?,?)',
                       (ident, uid, copied['parent_id'], copied['root_id'], now))
            self._prune_history(db)
            return ident, True, copied

    def add_batch(self, uid, message_id, body, batchid, *, template=None):
        """Bind the sealed input set and receipt in the same SQLite transaction."""
        from attachment_batches import BatchError, _bounded_records
        copied = None
        if template is not None:
            from task_templates import TemplateError, validate_snapshot
            if not isinstance(body, str):
                raise TemplateError('模板任务的补充要求须为文字。')
            copied = validate_snapshot(template, uid)
        kind = 'template_batch_task' if copied is not None else 'batch_task'
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT id,inputs,template FROM tasks WHERE uid=? AND message_id=? AND kind=?',
                               (uid, str(message_id), kind)).fetchone() if message_id else None
            if found:
                if copied is not None:
                    self._saved_template(found[2], uid)
                return found[0], False, self._saved_inputs(found[1], uid)
            row = db.execute('SELECT name,state,task_id FROM attachment_batches WHERE uid=? AND id=?',
                             (uid, batchid)).fetchone()
            if not row or row[1] != 'ready' or row[2]:
                raise BatchError('批次未封存或已绑定任务，不能重复执行。')
            files = [dict(json.loads(record), index=position) for position, record in db.execute(
                'SELECT position,record FROM attachment_batch_files WHERE batch_id=? ORDER BY position', (batchid,))]
            _bounded_records(files, uid)
            manifest = dict(id=batchid, uid=uid, name=row[0], files=files)
            ident, now = uuid.uuid4().hex, time.time()
            label = ('任务模板 · ' + copied['name'] + ' · ' if copied else '') + '附件批次 · ' + row[0]
            db.execute('INSERT INTO tasks (id,uid,message_id,kind,label,state,updated,payload,inputs,template) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (ident, uid, str(message_id or ''), kind, (label + (' · ' + body if body else ''))[:100],
                        'queued', now, body, json.dumps(manifest, ensure_ascii=False),
                        json.dumps(copied, ensure_ascii=False) if copied is not None else ''))
            db.execute("UPDATE attachment_batches SET state='used',task_id=?,updated=? WHERE uid=? AND id=?",
                       (ident, now, uid, batchid))
            self._prune_history(db)
            return ident, True, manifest

    def inputs(self, uid, ident):
        with self.connect(timeout=.1, read_only=True) as db:
            row = db.execute('SELECT inputs FROM tasks WHERE uid=? AND id=?', (uid, ident)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def reject_batch(self, uid, ident):
        """Undo a receipt/batch binding only when queue insertion failed."""
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT inputs FROM tasks WHERE uid=? AND id=? AND kind IN ('batch_task','template_batch_task') AND state='queued'",
                             (uid, ident)).fetchone()
            if not row:
                return False
            batchid = self._saved_inputs(row[0], uid)['id']
            now = time.time()
            db.execute("UPDATE tasks SET state='cancelled',execution_state='cancelled',payload=NULL,updated=? WHERE id=?", (now, ident))
            db.execute("UPDATE attachment_batches SET state='ready',task_id='',updated=? WHERE uid=? AND id=? AND task_id=? AND state='used'",
                       (now, uid, batchid, ident))
            return True

    def claim(self, uid, ident):
        """Only the owner may explicitly restore a task that never started."""
        entry = self.claim_entry(uid, ident)
        return entry['body'] if entry else None

    def claim_entry(self, uid, ident):
        """Validate every durable dependency before consuming recovery state."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT payload,kind,inputs,template,revision,local_files FROM tasks WHERE id=? AND uid=? AND state='recoverable' AND kind IN ('task','batch_task','template_task','template_batch_task','revision_task','local_file_task') AND COALESCE(resolution,'')='' AND payload IS NOT NULL", (ident, uid)).fetchone()
            if row is None:
                return None
            template = (self._saved_template(row[3], uid)
                        if row[1] in ('template_task', 'template_batch_task') else None)
            inputs = (self._saved_inputs(row[2], uid)
                      if row[1] in ('batch_task', 'template_batch_task') else None)
            revision = None
            if row[1] == 'revision_task':
                from task_revisions import RevisionError, validate_revision
                revision = validate_revision(json.loads(row[4]), uid)
                if revision['id'] != ident:
                    raise RevisionError('修改任务编号与保存记录不一致，未恢复任务。')
            if row[1] == 'local_file_task':
                from desktop_file_tasks import LocalFileError, validate_local_files
                local_files = validate_local_files(json.loads(row[5]), uid)
                if local_files['id'] != ident:
                    raise LocalFileError('文件任务编号与记录不一致，未恢复。')
            db.execute("UPDATE tasks SET state='queued', execution_state='queued', updated=? WHERE id=?", (time.time(), ident))
            entry = dict(body=row[0], inputs=inputs, template=template)
            if revision is not None:
                entry['revision'] = revision
            if row[1] == 'local_file_task':
                entry['local_files'] = local_files
            return entry

    def inspect_queued(self, uid, ident):
        with self.connect(timeout=.1, read_only=True) as db:
            return db.execute("SELECT 1 FROM tasks WHERE uid=? AND id=? AND state='queued'",
                              (uid, ident)).fetchone() is not None

    def edit_queued(self, uid, ident, payload, label):
        with self.connect(timeout=.1) as db:
            changed = db.execute("UPDATE tasks SET payload=?,label=?,updated=? WHERE uid=? AND id=? AND state='queued' AND kind='task' AND payload IS NOT NULL",
                                 (payload, label[:100], time.time(), uid, ident))
            return changed.rowcount == 1

    def cancel_queued(self, uid, ident):
        with self.connect(timeout=.1) as db:
            changed = db.execute("UPDATE tasks SET state='cancelled',execution_state='cancelled',payload=NULL,updated=? WHERE uid=? AND id=? AND state='queued'",
                                 (time.time(), uid, ident))
            return changed.rowcount == 1

    def save_artifacts(self, uid, ident, records, *, result_checks=None):
        # File evidence is independent of model and transport completion status.
        with self.connect(timeout=.1) as db:
            if result_checks is not None:
                changed = db.execute('UPDATE tasks SET artifacts=?,result_checks=? WHERE uid=? AND id=?',
                                     (json.dumps(records[:21], ensure_ascii=False),
                                      json.dumps(result_checks[:12], ensure_ascii=False), uid, ident))
                return changed.rowcount == 1
            changed = db.execute('UPDATE tasks SET artifacts=? WHERE uid=? AND id=?',
                                 (json.dumps(records[:21], ensure_ascii=False), uid, ident))
            return changed.rowcount == 1

    def artifacts(self, uid, ident='latest'):
        with self.connect(timeout=.05, read_only=True) as db:
            if ident.lower() in ('latest', 'last', '最近', '刚才'):
                row = db.execute("SELECT id,state,artifacts,result_checks FROM tasks WHERE uid=? AND kind IN ('task','batch_task','template_task','template_batch_task','revision_task','local_file_task') AND state NOT IN ('queued','running','waiting','recoverable') AND COALESCE(resolution,'')!='archived' ORDER BY updated DESC LIMIT 1", (uid,)).fetchone()
            else:
                row = db.execute('SELECT id,state,artifacts,result_checks FROM tasks WHERE uid=? AND id=?', (uid, ident)).fetchone()
            if row is None:
                return None
            records = json.loads(row[2] or '[]')
            if not isinstance(records, list):
                raise ValueError('artifact catalog is invalid')
            checks = decode_result_checks(row[3] or '')
            catalog = dict(id=row[0], state=row[1], records=records)
            if checks:
                catalog['result_checks'] = checks
            return catalog

    def unresolved(self, uid=None, *, timeout=10, read_only=False):
        with self.connect(timeout=timeout, read_only=read_only) as db:
            return db.execute("SELECT 1 FROM tasks WHERE state IN ('recoverable','uncertain','interrupted') AND COALESCE(resolution,'')='' AND (? IS NULL OR uid=?) LIMIT 1", (uid, uid)).fetchone() is not None

    def recent(self, uid=None, uncertain=False, limit=10, *, timeout=10, read_only=False):
        clauses, args = [], []
        if uid is not None:
            clauses.append('uid=?'); args.append(uid)
        if uncertain:
            clauses.append("state IN ('uncertain','recoverable','interrupted') AND COALESCE(resolution,'')=''")
        else:
            clauses.append("COALESCE(resolution,'')!='archived'")
        sql = "SELECT id,label,state,updated,COALESCE(resolution,'') FROM tasks"
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY updated DESC LIMIT ?'
        args.append(limit)
        with self.connect(timeout=timeout, read_only=read_only) as db:
            return [dict(zip(('id','label','state','updated','resolution'), row)) for row in db.execute(sql, args)]

    def describe(self, uid=None, *, timeout=10, read_only=False):
        options = dict(timeout=timeout, read_only=read_only)
        unresolved = self.recent(uid, uncertain=True, limit=10, **options)
        rows = unresolved + [r for r in self.recent(uid, **options) if r["id"] not in {u["id"] for u in unresolved}]
        if not rows:
            return '📋 暂无任务记录。'
        return '📋 最近任务（不会自动重跑）：\n' + '\n'.join(
            '%s · %s · %s' % (time.strftime('%m-%d %H:%M', time.localtime(r['updated'])),
                              LABELS[r['state']] + (' · ' + RESOLUTIONS[r['resolution']] if r['resolution'] else ''), r['label']) +
            '\n  编号：' + r['id'] +
            ('\n  恢复：/resume ' + r['id'] if r['state'] == 'recoverable' and not r['resolution'] else '') for r in rows) + '\n查看完整结果：/result 编号；补发未确认文字：/resend 编号；结清：/resolve 编号 checked|abandoned|archived。'

    def latest_outcome(self, uid, *, timeout=10, read_only=False):
        with self.connect(timeout=timeout, read_only=read_only) as db:
            row = db.execute("SELECT label,state,updated FROM tasks WHERE uid=? AND state NOT IN ('queued','running','waiting') AND COALESCE(resolution,'')!='archived' ORDER BY updated DESC LIMIT 1", (uid,)).fetchone()
            return dict(zip(('label', 'state', 'updated'), row)) if row else None

    def begin_message(self, uid, mid, label):
        if not mid:
            return 'new'
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM received_messages WHERE uid=? AND message_id=?', (uid, str(mid))).fetchone()
            if row:
                return row[0]
            db.execute('INSERT INTO received_messages VALUES (?,?,?,?,?)', (uid, str(mid), 'processing', label[:100], time.time()))
            db.execute("DELETE FROM received_messages WHERE state!='processing' AND updated<?", (time.time() - RESULT_RETENTION_SECONDS,))
            return 'new'

    def finish_message(self, uid, mid):
        if mid:
            with self.connect(timeout=.1) as db:
                db.execute("UPDATE received_messages SET state='handled',updated=? WHERE uid=? AND message_id=?", (time.time(), uid, str(mid)))

    def fail_message(self, uid, mid, label):
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM tasks WHERE uid=? AND message_id=?', (uid, str(mid))).fetchone():
                db.execute('INSERT INTO tasks (id,uid,message_id,kind,label,state,updated,summary) VALUES (?,?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex, uid, str(mid), 'dispatch', label[:100], 'uncertain', time.time(), '消息分发失败，结果未确认；请检查实际状态，不会自动重跑。'))
            db.execute('INSERT INTO received_messages VALUES (?,?,?,?,?) ON CONFLICT(uid,message_id) DO UPDATE SET state=excluded.state,updated=excluded.updated',
                       (uid, str(mid), 'failed', label[:100], time.time()))

    @classmethod
    def _prune_history(cls, db):
        """Apply one retention policy inside the caller's insertion transaction.

        The newest 200 receipts stay, plus active/unresolved work and tasks whose
        result delivery still needs attention. Reply bodies keep their existing
        seven-day lifetime; an expired body does not resolve an uncertain send.
        Only metadata is removed here, never source attachments or output files.
        """
        cls._prune_replies(db)
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('attachment_batches','attachment_batch_files','attachment_batch_intakes')")}
        has_batches = len(tables) == 3
        # A valid used batch cannot have pending intakes. Preserve both sides if
        # older/inconsistent data does, rather than create a dangling task link.
        pending_batch = (" AND NOT EXISTS (SELECT 1 FROM attachment_batches b "
                         "JOIN attachment_batch_intakes i ON i.batch_id=b.id "
                         "WHERE b.uid=tasks.uid AND b.task_id=tasks.id AND i.state='pending')"
                         if has_batches else '')
        db.execute("DELETE FROM tasks WHERE "
                   "(state IN ('completed','failed','cancelled') OR "
                   " (state IN ('recoverable','uncertain','interrupted') AND COALESCE(resolution,'')!='')) "
                   "AND COALESCE(delivery_state,'') NOT IN ('pending','unconfirmed') "
                   "AND NOT EXISTS (SELECT 1 FROM task_replies r WHERE r.task_id=tasks.id "
                   "                AND r.state IN ('pending','unconfirmed')) "
                   "AND NOT EXISTS (SELECT 1 FROM task_revision_links l WHERE tasks.id IN (l.task_id,l.parent_id,l.root_id)) "
                   "AND id NOT IN (SELECT id FROM tasks ORDER BY updated DESC,id DESC LIMIT 200)" + pending_batch)
        if has_batches:
            # Used batches cannot run again. Once their receipt has expired,
            # remove the bound catalog metadata together instead of advertising
            # a nonexistent task or releasing the batch for duplicate execution.
            # Also repair orphan links left by the previous task-only pruning.
            orphaned = ("SELECT b.id FROM attachment_batches b WHERE b.state='used' AND b.task_id!='' "
                        "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.id=b.task_id AND t.uid=b.uid) "
                        "AND NOT EXISTS (SELECT 1 FROM attachment_batch_intakes i "
                        "                WHERE i.batch_id=b.id AND i.state='pending')")
            db.execute('DELETE FROM attachment_batch_files WHERE batch_id IN (' + orphaned + ')')
            db.execute('DELETE FROM attachment_batch_intakes WHERE batch_id IN (' + orphaned + ')')
            db.execute('DELETE FROM attachment_batches WHERE id IN (' + orphaned + ')')
        # Cascading by task id also removes recent replies of a pruned receipt.
        cls._prune_replies(db)

    @staticmethod
    def _prune_replies(db):
        db.execute('DELETE FROM task_replies WHERE created<? OR NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.id=task_replies.task_id)', (time.time() - RESULT_RETENTION_SECONDS,))

    def prepare_reply(self, ident, body, execution_state, chunk_size=1800):
        with self.connect(timeout=.1) as db:
            self._prune_replies(db)
            row = db.execute('SELECT 1 FROM tasks WHERE id=?', (ident,)).fetchone()
            if not row:
                raise ValueError('task receipt missing')
            result = db.execute('INSERT INTO task_replies (task_id,body,created,chunk_size) VALUES (?,?,?,?)', (ident, body, time.time(), chunk_size))
            db.execute("UPDATE tasks SET execution_state=?,delivery_state='pending' WHERE id=?", (execution_state, ident))
            return result.lastrowid

    def record_delivery(self, reply_id, accepted, total, state):
        if state not in ('pending', 'accepted', 'unconfirmed'):
            raise ValueError(state)
        with self.connect(timeout=.1) as db:
            row = db.execute('SELECT task_id FROM task_replies WHERE id=?', (reply_id,)).fetchone()
            if not row:
                return
            db.execute('UPDATE task_replies SET accepted_chunks=MAX(accepted_chunks,?),total_chunks=?,state=? WHERE id=?', (accepted, total, state, reply_id))
            counts = dict(db.execute('SELECT state,COUNT(*) FROM task_replies WHERE task_id=? GROUP BY state', (row[0],)).fetchall())
            delivery = 'unconfirmed' if counts.get('unconfirmed') else 'pending' if counts.get('pending') else 'accepted'
            db.execute('UPDATE tasks SET delivery_state=? WHERE id=?', (delivery, row[0]))

    def replies(self, uid, ident, *, only_pending=False):
        with self.connect(timeout=.05, read_only=True) as db:
            task = db.execute('SELECT state FROM tasks WHERE uid=? AND id=?', (uid, ident)).fetchone()
            if not task or task[0] in ('queued', 'running', 'waiting'):
                return None
            sql = 'SELECT id,body,accepted_chunks,total_chunks,state,chunk_size FROM task_replies WHERE task_id=? AND created>=?'
            if only_pending:
                sql += " AND state!='accepted'"
            sql += ' ORDER BY id'
            return [dict(zip(('id','body','accepted_chunks','total_chunks','state','chunk_size'), row)) for row in db.execute(sql, (ident, time.time() - RESULT_RETENTION_SECONDS))]

    def resolve(self, uid, ident, action):
        if action not in RESOLUTIONS:
            raise ValueError(action)
        with self.connect(timeout=.1) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,resolution FROM tasks WHERE uid=? AND id=?', (uid, ident)).fetchone()
            if not row or row[0] in ('queued','running','waiting'):
                return False
            if action == 'checked' and row[0] not in ('uncertain','interrupted'):
                return False
            if action == 'abandoned' and row[0] != 'recoverable':
                return False
            if row[1]:
                return row[1] == action
            db.execute('UPDATE tasks SET resolution=?,resolved_at=?,payload=NULL WHERE id=?', (action, time.time(), ident))
            return True
