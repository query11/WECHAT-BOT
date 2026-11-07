"""Owner-scoped task definitions. No task recovery, filesystem or model actions.

The catalog shares TaskJournal's SQLite file but never instantiates TaskJournal.
Callers persist a separate definition snapshot when accepting a task; a catalog
update or deletion therefore cannot alter an already accepted task.
"""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
import unicodedata
import uuid

from template_result_checks import CheckDefinitionError, parse_checks


MAX_TEMPLATES = 30
DEFINITION_LIMITS = {'inputs': 2000, 'steps': 8000,
                     'output_dir': 2000, 'output_format': 1000}
MAX_DEFINITION_JSON = 64 * 1024
_COLUMNS = {'id', 'uid', 'name', 'revision', 'definition', 'created', 'updated'}


class TemplateError(ValueError):
    """A safe, Chinese user-facing validation or persistence error."""


def _owner(uid):
    if (not isinstance(uid, str) or not uid or len(uid) > 300
            or any(unicodedata.category(c).startswith('C') for c in uid)):
        raise TemplateError('任务模板的用户标识无效。')
    return uid


def _name(name):
    if not isinstance(name, str):
        raise TemplateError('模板名称须为 1 至 40 个字。')
    if ('/' in name or '\\' in name
            or any(unicodedata.category(c).startswith('C') for c in name)):
        raise TemplateError('模板名称不能含路径分隔符、控制字符或换行。')
    name = name.strip()
    if not 0 < len(name) <= 40:
        raise TemplateError('模板名称须为 1 至 40 个字。')
    if any(c.isspace() for c in name):
        raise TemplateError('模板名称不能含空白，请使用一个完整名称。')
    return name


def validate_definition(definition):
    """Validate and copy bounded text. Paths and step text are never executed."""
    if (not isinstance(definition, dict) or not set(DEFINITION_LIMITS).issubset(definition)
            or set(definition) - set(DEFINITION_LIMITS) - {'checks'}):
        raise TemplateError('模板须包含输入要求、处理步骤、输出目录和输出格式，可另加检查条件。')
    copied = {}
    labels = {'inputs': '输入要求', 'steps': '处理步骤',
              'output_dir': '输出目录', 'output_format': '输出格式'}
    for field, limit in DEFINITION_LIMITS.items():
        value = definition[field]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise TemplateError('模板的%s须为 1 至 %d 个字。' % (labels[field], limit))
        if any(unicodedata.category(c).startswith('C') and c not in '\r\n\t'
               for c in value):
            raise TemplateError('模板的%s含无效控制字符。' % labels[field])
        copied[field] = value
    if 'checks' in definition:
        try:
            parse_checks(definition['checks'])
        except CheckDefinitionError as exc:
            raise TemplateError(str(exc)) from None
        if definition['checks'].strip():
            copied['checks'] = definition['checks'].strip()
    return copied


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TemplateError('模板记录格式损坏，请重新保存。')
        result[key] = value
    return result


def _decode(raw):
    if not isinstance(raw, str) or len(raw) > MAX_DEFINITION_JSON:
        raise TemplateError('模板记录格式损坏，请重新保存。')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        return validate_definition(value)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise TemplateError('模板记录格式损坏，请重新保存。') from None


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2 ** 63 - 1:
        raise TemplateError('模板版本记录损坏，无法继续保存。')
    return value


def _row(row):
    ident, uid, name, revision, raw, created, updated = row
    try:
        if (not isinstance(ident, str) or len(ident) != 32
                or any(c not in '0123456789abcdef' for c in ident)
                or _owner(uid) != uid or _name(name) != name
                or isinstance(created, bool) or not isinstance(created, (int, float))
                or isinstance(updated, bool) or not isinstance(updated, (int, float))
                or not math.isfinite(created) or not math.isfinite(updated)
                or created < 0 or updated < created):
            raise ValueError()
        return dict(id=ident, uid=uid, name=name, revision=_revision(revision),
                    definition=_decode(raw), created=created, updated=updated)
    except (TemplateError, ValueError, TypeError, OverflowError):
        raise TemplateError('模板记录格式损坏，请重新保存。') from None


def validate_snapshot(snapshot, uid):
    """Copy an accepted template without consulting the mutable catalog.

    This is also the validation boundary for a persisted/recovered snapshot.
    It verifies ownership and bounded text, without opening any supplied path.
    """
    uid = _owner(uid)
    if not isinstance(snapshot, dict) or set(snapshot) != _COLUMNS:
        raise TemplateError('任务模板快照格式无效，不能开始任务。')
    if snapshot['uid'] != uid:
        raise TemplateError('任务模板快照不属于当前用户。')
    try:
        encoded = json.dumps(validate_definition(snapshot['definition']), ensure_ascii=False)
        return _row((snapshot['id'], snapshot['uid'], snapshot['name'], snapshot['revision'],
                     encoded, snapshot['created'], snapshot['updated']))
    except (TemplateError, ValueError, TypeError, OverflowError, RecursionError):
        raise TemplateError('任务模板快照格式无效，不能开始任务。') from None


class TaskTemplateStore:
    """Durable template catalog; deletion keeps a monotonically increasing version."""

    def __init__(self, path):
        try:
            self.path = str(Path(path))
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except (OSError, TypeError, ValueError):
            raise TemplateError('无法打开任务模板存储，请稍后重试。') from None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS task_templates ('
                       'id TEXT PRIMARY KEY, uid TEXT NOT NULL, name TEXT NOT NULL, '
                       'revision INTEGER NOT NULL, definition TEXT NOT NULL, '
                       'created REAL NOT NULL, updated REAL NOT NULL)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(task_templates)')}
            if not _COLUMNS.issubset(columns):
                raise TemplateError('任务模板表结构不兼容，无法安全读写。')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS task_templates_owner_name '
                       'ON task_templates(uid,name)')
            db.execute('CREATE TABLE IF NOT EXISTS desktop_template_pins ('
                       'uid TEXT NOT NULL, template_id TEXT NOT NULL, pinned REAL NOT NULL, '
                       'PRIMARY KEY(uid,template_id))')
            db.execute('CREATE TABLE IF NOT EXISTS task_template_revisions ('
                       'uid TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL, '
                       'PRIMARY KEY(uid,name))')
            ledger_columns = {row[1] for row in db.execute('PRAGMA table_info(task_template_revisions)')}
            if not {'uid', 'name', 'revision'}.issubset(ledger_columns):
                raise TemplateError('任务模板版本表结构不兼容，无法安全读写。')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS task_template_revision_owner_name '
                       'ON task_template_revisions(uid,name)')
            # Support a catalog created before the revision ledger existed.
            # Do not read or alter the tasks table or recover queued work.
            for uid, name, revision in db.execute('SELECT uid,name,revision FROM task_templates'):
                _owner(uid)
                if _name(name) != name:
                    raise TemplateError('模板名称记录损坏，无法安全读写。')
                _revision(revision)
                old = db.execute('SELECT revision FROM task_template_revisions WHERE uid=? AND name=?',
                                 (uid, name)).fetchone()
                if old:
                    if _revision(old[0]) < revision:
                        raise TemplateError('模板版本记录不一致，无法安全读写。')
                else:
                    db.execute('INSERT INTO task_template_revisions(uid,name,revision) VALUES (?,?,?)',
                               (uid, name, revision))

    @contextmanager
    def connect(self, read_only=False):
        db = None
        try:
            if read_only:
                db = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=.2)
                db.execute('PRAGMA query_only=ON')
            else:
                db = sqlite3.connect(self.path, timeout=.2)
            with db:
                yield db
        except (sqlite3.Error, OSError, UnicodeError):
            raise TemplateError('任务模板存储暂时不可用，操作未完成，请稍后重试。') from None
        finally:
            if db is not None:
                db.close()

    def save(self, uid, name, definition, *, expected_revision=None):
        uid, name = _owner(uid), _name(name)
        copied = validate_definition(definition)
        encoded = json.dumps(copied, ensure_ascii=False, separators=(',', ':'))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT id,uid,name,revision,definition,created,updated '
                               'FROM task_templates WHERE uid=? AND name=?', (uid, name)).fetchone()
            prior = _row(found) if found else None
            if expected_revision is not None:
                if type(expected_revision) is not int or expected_revision < 0:
                    raise TemplateError('模板版本无效，请重新读取。')
                if (prior['revision'] if prior else 0) != expected_revision:
                    raise TemplateError('模板已在其他地方修改或删除，请重新读取后再保存。')
            if prior is None:
                count = db.execute('SELECT COUNT(*) FROM task_templates WHERE uid=?', (uid,)).fetchone()[0]
                if count >= MAX_TEMPLATES:
                    raise TemplateError('每位用户最多保存 30 个任务模板，请先删除不用的模板。')
            old = db.execute('SELECT revision FROM task_template_revisions WHERE uid=? AND name=?',
                             (uid, name)).fetchone()
            previous = _revision(old[0]) if old else 0
            if prior and previous < prior['revision']:
                raise TemplateError('模板版本记录不一致，无法继续保存。')
            revision = _revision(previous + 1)
            now = time.time()
            ident = prior['id'] if prior else uuid.uuid4().hex
            created = prior['created'] if prior else now
            # Clock corrections do not create a corrupt updated-before-created row.
            now = max(now, prior['updated'] if prior else created)
            if prior:
                db.execute('UPDATE task_templates SET revision=?,definition=?,updated=? '
                           'WHERE uid=? AND name=?', (revision, encoded, now, uid, name))
            else:
                db.execute('INSERT INTO task_templates(id,uid,name,revision,definition,created,updated) '
                           'VALUES (?,?,?,?,?,?,?)',
                           (ident, uid, name, revision, encoded, created, now))
            db.execute('INSERT INTO task_template_revisions(uid,name,revision) VALUES (?,?,?) '
                       'ON CONFLICT(uid,name) DO UPDATE SET revision=excluded.revision',
                       (uid, name, revision))
            return dict(id=ident, uid=uid, name=name, revision=revision,
                        definition=copied, created=created, updated=now)

    def get(self, uid, name):
        uid, name = _owner(uid), _name(name)
        with self.connect(read_only=True) as db:
            found = db.execute('SELECT id,uid,name,revision,definition,created,updated '
                               'FROM task_templates WHERE uid=? AND name=?', (uid, name)).fetchone()
            return _row(found) if found else None

    def list(self, uid):
        uid = _owner(uid)
        with self.connect(read_only=True) as db:
            rows = db.execute('SELECT id,uid,name,revision,definition,created,updated '
                              'FROM task_templates WHERE uid=? ORDER BY updated DESC,name LIMIT ?',
                              (uid, MAX_TEMPLATES + 1)).fetchall()
            if len(rows) > MAX_TEMPLATES:
                raise TemplateError('模板记录数量超过上限，无法安全读取。')
            return [_row(row) for row in rows]

    def pinned(self, uid):
        with self.connect(read_only=True) as db:
            return [row[0] for row in db.execute(
                'SELECT p.template_id FROM desktop_template_pins p JOIN task_templates t '
                'ON t.id=p.template_id AND t.uid=p.uid WHERE p.uid=? ORDER BY p.pinned,p.template_id LIMIT 5',
                (_owner(uid),))]

    def pin(self, uid, name, enabled, expected_revision):
        uid, name = _owner(uid), _name(name)
        if type(enabled) is not bool or type(expected_revision) is not int:
            raise TemplateError('固定模板参数无效，请重新读取。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id,revision FROM task_templates WHERE uid=? AND name=?', (uid, name)).fetchone()
            if not row or row[1] != expected_revision:
                raise TemplateError('模板已经变化，请重新读取后固定。')
            db.execute('DELETE FROM desktop_template_pins WHERE template_id NOT IN (SELECT id FROM task_templates)')
            if not enabled:
                db.execute('DELETE FROM desktop_template_pins WHERE uid=? AND template_id=?', (uid, row[0]))
            elif not db.execute('SELECT 1 FROM desktop_template_pins WHERE uid=? AND template_id=?', (uid, row[0])).fetchone():
                if db.execute('SELECT count(*) FROM desktop_template_pins WHERE uid=?', (uid,)).fetchone()[0] >= 5:
                    raise TemplateError('每位用户最多固定 5 个常用模板，请先取消一个。')
                db.execute('INSERT INTO desktop_template_pins VALUES (?,?,?)', (uid, row[0], time.time()))

    def delete(self, uid, name, *, expected_revision=None):
        uid, name = _owner(uid), _name(name)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if expected_revision is not None:
                row = db.execute('SELECT revision FROM task_templates WHERE uid=? AND name=?', (uid, name)).fetchone()
                if type(expected_revision) is not int or not row or row[0] != expected_revision:
                    raise TemplateError('模板已在其他地方修改或删除，请重新读取后再删除。')
            # Deliberately allow deletion of a corrupt definition; it cannot be
            # executed, but its owner must be able to remove and save it anew.
            return db.execute('DELETE FROM task_templates WHERE uid=? AND name=?',
                              (uid, name)).rowcount > 0
