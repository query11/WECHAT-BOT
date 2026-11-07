"""Isolated template receipts/recovery. No real database, models or transport."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from attachment_batches import AttachmentBatchStore, BatchError, collect_input_records
from task_journal import TaskJournal
from task_templates import TaskTemplateStore, TemplateError


class TemplateJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / 'tasks.sqlite'
        self.journal = TaskJournal(self.path)
        self.templates = TaskTemplateStore(self.path)
        self.batches = AttachmentBatchStore(self.path)
        self.definition = dict(inputs='本周工作记录', steps='归纳事项和进度',
                               output_dir='工作目录', output_format='Markdown')
        self.snapshot = self.templates.save('alice', '周报', self.definition)

    def row(self, ident):
        with self.journal.connect(read_only=True) as db:
            row = db.execute('SELECT kind,state,payload,inputs,template,updated FROM tasks WHERE id=?',
                             (ident,)).fetchone()
        return dict(zip(('kind', 'state', 'payload', 'inputs', 'template', 'updated'), row))

    def ready(self, name='资料', uid='alice'):
        source = self.base / (name + '.txt')
        raw = b'isolated input'
        source.write_bytes(raw)
        entry = dict(uid=uid, name=source.name, orig=source.name, at=time.time(),
                     bytes=len(raw), md5=hashlib.md5(raw).hexdigest())
        records = collect_input_records([entry], self.base, [str(self.base)], uid)
        batch = self.batches.create_recent(uid, name, records)
        return self.batches.seal(uid, batch['id'])

    def recoverable(self, ident):
        self.journal = TaskJournal(self.path)
        self.assertEqual(self.row(ident)['state'], 'recoverable')

    def test_empty_body_and_snapshot_survive_modify_delete_and_caller_mutation(self):
        original = deepcopy(self.snapshot)
        ident, added, accepted = self.journal.add_template('alice', 'empty', '', self.snapshot)
        self.assertTrue(added)
        self.assertEqual(accepted, original)
        self.assertEqual(self.row(ident)['kind'], 'template_task')
        self.assertEqual(self.row(ident)['payload'], '')
        self.snapshot['definition']['steps'] = '调用方修改'
        accepted['definition']['steps'] = '返回值修改'
        revised = self.templates.save('alice', '周报', dict(self.definition, steps='新版步骤'))
        self.assertEqual(revised['revision'], 2)
        self.assertTrue(self.templates.delete('alice', '周报'))
        self.recoverable(ident)
        self.assertEqual(self.journal.claim_entry('alice', ident),
                         dict(body='', inputs=None, template=original))
        self.assertIsNone(self.journal.claim_entry('alice', ident))

    def test_template_batch_keeps_original_manifest_and_definition(self):
        batch = self.ready()
        original = deepcopy(self.snapshot)
        ident, added, manifest = self.journal.add_batch('alice', 'batch-empty', '', batch['id'],
                                                        template=self.snapshot)
        self.assertTrue(added)
        self.assertEqual(self.row(ident)['kind'], 'template_batch_task')
        self.assertEqual(self.batches.get('alice', batch['id'])['task_id'], ident)
        self.templates.save('alice', '周报', dict(self.definition, steps='更新'))
        self.templates.delete('alice', '周报')
        # Changes to the catalog or its returned objects cannot replace the
        # independent input manifest already bound to the accepted receipt.
        with self.journal.connect() as db:
            db.execute('DELETE FROM attachment_batch_files WHERE batch_id=?', (batch['id'],))
        self.recoverable(ident)
        self.assertEqual(self.journal.claim_entry('alice', ident),
                         dict(body='', inputs=manifest, template=original))

    def test_owner_isolation_for_create_recovery_and_release(self):
        with self.assertRaises(TemplateError):
            self.journal.add_template('bob', 'foreign', '', self.snapshot)
        batch = self.ready()
        with self.assertRaises(TemplateError):
            self.journal.add_batch('bob', 'foreign', '', batch['id'], template=self.snapshot)
        other = self.templates.save('bob', '周报', self.definition)
        with self.assertRaises(BatchError):
            self.journal.add_batch('bob', 'foreign-batch', '', batch['id'], template=other)
        ident, _, _ = self.journal.add_batch('alice', 'owned', '', batch['id'], template=self.snapshot)
        self.assertFalse(self.journal.reject_batch('bob', ident))
        self.recoverable(ident)
        before = self.row(ident)
        self.assertIsNone(self.journal.claim_entry('bob', ident))
        self.assertEqual(self.row(ident), before)
        self.assertEqual(self.journal.claim_entry('alice', ident)['template'], self.snapshot)

    def test_corrupt_or_foreign_snapshots_preserve_recoverable_state(self):
        foreign = dict(self.snapshot, uid='bob')
        invalid = deepcopy(self.snapshot)
        invalid['definition']['steps'] = ''
        bad_values = ['', '{', '[]', json.dumps(foreign), json.dumps(invalid),
                      json.dumps({key: value for key, value in self.snapshot.items() if key != 'revision'}),
                      '{"uid":"bob",' + json.dumps(self.snapshot)[1:]]
        for kind in ('template_task', 'template_batch_task'):
            if kind == 'template_task':
                ident, _, _ = self.journal.add_template('alice', kind, '要求', self.snapshot)
            else:
                batch = self.ready('损坏快照')
                ident, _, _ = self.journal.add_batch('alice', kind, '要求', batch['id'],
                                                     template=self.snapshot)
            self.recoverable(ident)
            for raw in bad_values:
                with self.subTest(kind=kind, raw=raw[:20]):
                    with self.journal.connect() as db:
                        db.execute('UPDATE tasks SET template=? WHERE id=?', (raw, ident))
                    before = self.row(ident)
                    with self.assertRaises(TemplateError):
                        self.journal.claim_entry('alice', ident)
                    self.assertEqual(self.row(ident), before)

    def test_corrupt_or_foreign_inputs_preserve_both_batch_kinds(self):
        for with_template in (False, True):
            batch = self.ready('清单' + str(with_template))
            ident, _, manifest = self.journal.add_batch('alice', 'manifest-' + str(with_template),
                                                        '要求', batch['id'],
                                                        template=self.snapshot if with_template else None)
            foreign_file = deepcopy(manifest)
            foreign_file['files'][0]['uid'] = 'bob'
            invalid_file = deepcopy(manifest)
            invalid_file['files'][0]['sha256'] = ''
            invalid_index = deepcopy(manifest)
            invalid_index['files'][0]['index'] = True
            corruptions = ['', '{', '[]', json.dumps(dict(manifest, uid='bob')),
                           json.dumps(dict(manifest, id='')), json.dumps(dict(manifest, files=[])),
                           json.dumps(dict(manifest, name='')), json.dumps(foreign_file),
                           json.dumps(invalid_file), json.dumps(invalid_index),
                           '{"uid":"bob",' + json.dumps(manifest)[1:]]
            self.recoverable(ident)
            for raw in corruptions:
                with self.subTest(template=with_template, raw=raw[:20]):
                    with self.journal.connect() as db:
                        db.execute('UPDATE tasks SET inputs=? WHERE id=?', (raw, ident))
                    before = self.row(ident)
                    with self.assertRaises(BatchError):
                        self.journal.claim_entry('alice', ident)
                    self.assertEqual(self.row(ident), before)
            with self.journal.connect() as db:
                db.execute('UPDATE tasks SET inputs=? WHERE id=?', (json.dumps(manifest), ident))
            self.assertEqual(self.journal.claim_entry('alice', ident)['inputs'], manifest)

    def test_invalid_new_snapshot_creates_nothing_and_keeps_batch_ready(self):
        batch = self.ready()
        for snapshot in (None, {}, dict(self.snapshot, uid='bob')):
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(TemplateError):
                    self.journal.add_template('alice', 'invalid', '', snapshot)
                if snapshot is not None:
                    with self.assertRaises(TemplateError):
                        self.journal.add_batch('alice', 'invalid', '', batch['id'], template=snapshot)
        with self.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        actual = self.batches.get('alice', batch['id'])
        self.assertEqual((actual['state'], actual['task_id']), ('ready', ''))

    def test_batch_binding_failure_rolls_back_receipt_inputs_and_snapshot(self):
        batch = self.ready()
        with self.journal.connect() as db:
            db.execute("CREATE TRIGGER reject_batch_bind BEFORE UPDATE ON attachment_batches "
                       "WHEN NEW.state='used' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.journal.add_batch('alice', 'rollback', '', batch['id'], template=self.snapshot)
        with self.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        actual = self.batches.get('alice', batch['id'])
        self.assertEqual((actual['state'], actual['task_id']), ('ready', ''))

    def test_receipt_failure_never_consumes_batch(self):
        batch = self.ready()
        with self.journal.connect() as db:
            db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON tasks "
                       "BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.journal.add_template('alice', 'rollback', '', self.snapshot)
        with self.assertRaises(sqlite3.DatabaseError):
            self.journal.add_batch('alice', 'rollback-batch', '', batch['id'], template=self.snapshot)
        actual = self.batches.get('alice', batch['id'])
        self.assertEqual((actual['state'], actual['task_id']), ('ready', ''))

    def test_duplicate_template_message_returns_original_snapshot(self):
        original = deepcopy(self.snapshot)
        ident, _, _ = self.journal.add_template('alice', 'repeat', '原要求', original)
        newer = self.templates.save('alice', '周报', dict(self.definition, steps='新版'))
        self.assertEqual(self.journal.add_template('alice', 'repeat', '新要求', newer),
                         (ident, False, original))
        self.assertEqual(self.row(ident)['payload'], '原要求')
        other = self.templates.save('bob', '周报', self.definition)
        other_id, added, _ = self.journal.add_template('bob', 'repeat', '', other)
        self.assertTrue(added)
        self.assertNotEqual(other_id, ident)

    def test_duplicate_template_batch_does_not_consume_another_batch(self):
        first, second = self.ready('一'), self.ready('二')
        ident, _, manifest = self.journal.add_batch('alice', 'repeat-batch', '原要求', first['id'],
                                                    template=self.snapshot)
        newer = self.templates.save('alice', '周报', dict(self.definition, steps='新版'))
        self.assertEqual(self.journal.add_batch('alice', 'repeat-batch', '新要求', second['id'],
                                                 template=newer), (ident, False, manifest))
        self.assertEqual(json.loads(self.row(ident)['template']), self.snapshot)
        self.assertEqual(self.row(ident)['payload'], '原要求')
        self.assertEqual(self.batches.get('alice', second['id'])['state'], 'ready')

    def test_reject_batch_releases_both_kinds_and_only_queued_owner_receipt(self):
        for with_template in (False, True):
            batch = self.ready('释放' + str(with_template))
            ident, _, _ = self.journal.add_batch('alice', 'reject-' + str(with_template), '要求', batch['id'],
                                                   template=self.snapshot if with_template else None)
            self.assertFalse(self.journal.reject_batch('bob', ident))
            self.assertTrue(self.journal.reject_batch('alice', ident))
            self.assertEqual(self.row(ident)['state'], 'cancelled')
            self.assertIsNone(self.row(ident)['payload'])
            actual = self.batches.get('alice', batch['id'])
            self.assertEqual((actual['state'], actual['task_id']), ('ready', ''))
            self.assertFalse(self.journal.reject_batch('alice', ident))
        batch = self.ready('已启动')
        ident, _, _ = self.journal.add_batch('alice', 'started', '', batch['id'], template=self.snapshot)
        self.journal.update(ident, 'running')
        self.assertFalse(self.journal.reject_batch('alice', ident))
        self.assertEqual(self.batches.get('alice', batch['id'])['state'], 'used')

    def test_queue_editing_remains_only_ordinary_tasks(self):
        ordinary, _ = self.journal.add('alice', 'normal', 'task', '普通', payload='原文')
        templated, _, _ = self.journal.add_template('alice', 'templated', '', self.snapshot)
        plain_batch, _, _ = self.journal.add_batch('alice', 'plain-batch', '原文', self.ready('普通批次')['id'])
        template_batch, _, _ = self.journal.add_batch('alice', 'template-batch', '',
                                                       self.ready('模板批次')['id'], template=self.snapshot)
        for ident in (templated, plain_batch, template_batch):
            before = self.row(ident)
            self.assertFalse(self.journal.edit_queued('alice', ident, '修改', '修改'))
            self.assertEqual(self.row(ident), before)
        self.assertFalse(self.journal.edit_queued('bob', ordinary, '修改', '修改'))
        self.assertTrue(self.journal.edit_queued('alice', ordinary, '修改', '修改'))
        self.assertEqual(self.row(ordinary)['payload'], '修改')

    def test_restart_recovers_four_kinds_without_replaying_started_templates(self):
        ordinary, _ = self.journal.add('alice', 'normal', 'task', '普通', payload='原文')
        plain_batch, _, _ = self.journal.add_batch('alice', 'plain', '原文', self.ready('普通')['id'])
        templated, _, _ = self.journal.add_template('alice', 'template', '', self.snapshot)
        template_batch, _, _ = self.journal.add_batch('alice', 'template-batch', '',
                                                       self.ready('模板')['id'], template=self.snapshot)
        started, _, _ = self.journal.add_template('alice', 'started', '', self.snapshot)
        self.journal.update(started, 'running')
        self.journal = TaskJournal(self.path)
        for ident in (ordinary, plain_batch, templated, template_batch):
            self.assertEqual(self.row(ident)['state'], 'recoverable')
        self.assertEqual(self.row(started)['state'], 'uncertain')
        self.assertIsNone(self.journal.claim_entry('alice', started))
        self.assertEqual(self.journal.claim_entry('alice', ordinary),
                         dict(body='原文', inputs=None, template=None))

    def test_latest_artifacts_include_empty_template_tasks_without_older_fallback(self):
        old, _ = self.journal.add('alice', 'old', 'task', '旧任务', payload='原文')
        self.journal.save_artifacts('alice', old, [dict(path='older')])
        self.journal.update(old, 'completed')
        for kind in ('template_task', 'template_batch_task'):
            if kind == 'template_task':
                ident, _, _ = self.journal.add_template('alice', kind, '', self.snapshot)
            else:
                ident, _, _ = self.journal.add_batch('alice', kind, '', self.ready('最近')['id'],
                                                       template=self.snapshot)
            self.journal.update(ident, 'completed')
            self.assertEqual(self.journal.artifacts('alice'),
                             dict(id=ident, state='completed', records=[]))
        self.assertIsNone(self.journal.artifacts('bob', ident))

    def test_old_sqlite_migration_adds_template_column_and_keeps_legacy_uncertainty(self):
        legacy = self.base / 'legacy.sqlite'
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, uid TEXT, message_id TEXT, '
                       'kind TEXT, label TEXT, state TEXT, updated REAL)')
            db.execute("INSERT INTO tasks VALUES ('old','alice','legacy','task','旧任务','queued',0)")
        journal = TaskJournal(legacy)
        with journal.connect(read_only=True) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
            self.assertIn('template', columns)
            self.assertEqual(db.execute('SELECT template,state FROM tasks WHERE id=?', ('old',)).fetchone(),
                             ('', 'uncertain'))
        ident, _, _ = journal.add_template('alice', 'new', '', self.snapshot)
        restarted = TaskJournal(legacy)
        self.assertEqual(restarted.claim_entry('alice', ident)['template'], self.snapshot)
        self.assertIsNone(restarted.claim_entry('alice', 'old'))

    def test_template_body_must_be_text(self):
        batch = self.ready()
        for body in (None, 0, [], {}):
            with self.subTest(body=body):
                with self.assertRaises(TemplateError):
                    self.journal.add_template('alice', '', body, self.snapshot)
                with self.assertRaises(TemplateError):
                    self.journal.add_batch('alice', '', body, batch['id'], template=self.snapshot)


if __name__ == '__main__':
    unittest.main(verbosity=2)
