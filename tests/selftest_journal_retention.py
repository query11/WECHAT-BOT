"""Real temporary SQLite retention boundaries; no runtime data or transport."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from attachment_batches import AttachmentBatchStore, collect_input_records
from task_journal import RESULT_RETENTION_SECONDS, TaskJournal
from task_templates import TaskTemplateStore


class JournalRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wechat-retention-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'tasks.sqlite'
        self.journal = TaskJournal(self.path)
        self.sequence = 0

    def snapshot(self):
        return TaskTemplateStore(self.path).save('owner', 'Review', dict(
            inputs='Provided materials', steps='Summarize verified facts',
            output_dir='.', output_format='TXT'))

    def ready(self, name='inputs', uid='owner'):
        self.sequence += 1
        source = self.root / ('source-%d.txt' % self.sequence)
        source.write_bytes(b'isolated attachment bytes')
        entry = dict(uid=uid, name=source.name, orig=source.name,
                     bytes=source.stat().st_size, at=time.time())
        records = collect_input_records([entry], self.root, [self.root], uid)
        store = AttachmentBatchStore(self.path)
        batch = store.create(uid, name)
        store.begin_intake(uid, batch['id'], 'intake-%d' % self.sequence)
        store.finish_intake(uid, batch['id'], 'intake-%d' % self.sequence, records)
        return store, store.seal(uid, batch['id']), source

    def seed(self, count=200, prefix='filler'):
        now = time.time()
        with self.journal.connect() as db:
            db.executemany('INSERT INTO tasks(id,uid,message_id,kind,label,state,updated) '
                           'VALUES (?,?,?,?,?,?,?)',
                           [('%s-%03d' % (prefix, n), 'other', '%s-%d' % (prefix, n),
                             'task', 'Finished', 'completed', now - 1000 + n)
                            for n in range(count)])

    def row(self, ident):
        with self.journal.connect(read_only=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            return dict(row) if row else None

    def old(self, ident):
        with self.journal.connect() as db:
            db.execute('UPDATE tasks SET updated=0 WHERE id=?', (ident,))

    def reply(self, ident, state='accepted', created=None):
        reply = self.journal.prepare_reply(ident, 'Stored result text', 'completed')
        self.journal.record_delivery(reply, int(state == 'accepted'), 1, state)
        if created is not None:
            with self.journal.connect() as db:
                db.execute('UPDATE task_replies SET created=? WHERE id=?', (created, reply))
        return reply

    def test_all_insert_kinds_share_retention_and_remove_linked_replies(self):
        for kind in ('task', 'template_task', 'batch_task', 'template_batch_task'):
            with self.subTest(kind=kind):
                # Each subcase gets a separate database and real batch manifest.
                self.path = self.root / (kind + '.sqlite')
                self.journal = TaskJournal(self.path)
                snapshot = self.snapshot()
                _, batch, _ = self.ready(name=kind)
                self.seed()
                self.reply('filler-000')
                with self.journal.connect() as db:
                    db.execute('INSERT INTO task_replies(task_id,body,created,state) '
                               'VALUES (?,?,?,?)', ('missing', 'orphan', time.time(), 'accepted'))
                if kind == 'task':
                    ident, _ = self.journal.add('owner', 'new', 'task', 'New', payload='Run')
                elif kind == 'template_task':
                    ident, _, _ = self.journal.add_template('owner', 'new', '', snapshot)
                else:
                    ident, _, _ = self.journal.add_batch('owner', 'new', 'Run', batch['id'],
                        template=snapshot if kind == 'template_batch_task' else None)
                with self.journal.connect(read_only=True) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 200)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM task_replies').fetchone()[0], 0)
                self.assertIsNone(self.row('filler-000'))
                self.assertEqual(self.row(ident)['kind'], kind)

    def test_template_only_stream_is_bounded_without_an_ordinary_task(self):
        snapshot = self.snapshot()
        for n in range(205):
            ident, added, _ = self.journal.add_template('owner', str(n), '', snapshot)
            self.assertTrue(added)
            self.journal.update(ident, 'completed')
        with self.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 200)

    def test_active_unresolved_and_unconfirmed_delivery_survive_count_pressure(self):
        protected = []
        for state in ('queued', 'running', 'waiting', 'recoverable', 'uncertain', 'interrupted'):
            ident, _ = self.journal.add('owner', state, 'task', state, payload='Original')
            self.journal.update(ident, state)
            self.old(ident)
            protected.append(ident)
        # Even inconsistent resolution metadata must never discard active work.
        with self.journal.connect() as db:
            db.execute("UPDATE tasks SET resolution='archived' WHERE state='running'")
        for state in ('pending', 'unconfirmed'):
            ident, _ = self.journal.add('owner', state, 'task', state)
            self.journal.update(ident, 'completed')
            self.reply(ident, state)
            self.old(ident)
            protected.append(ident)
        # Per-reply evidence also protects against a stale aggregate state.
        stale = protected[-1]
        with self.journal.connect() as db:
            db.execute("UPDATE tasks SET delivery_state='accepted' WHERE id=?", (stale,))
        self.seed()
        self.journal.add_template('owner', 'pressure', '', self.snapshot())
        for ident in protected:
            self.assertIsNotNone(self.row(ident), ident)

    def test_resolved_old_uncertainty_can_expire_but_not_unconfirmed_delivery(self):
        ident, _ = self.journal.add('owner', 'resolved', 'task', 'Checked')
        self.journal.update(ident, 'uncertain')
        self.assertTrue(self.journal.resolve('owner', ident, 'checked'))
        self.old(ident)
        self.seed()
        self.journal.add('owner', 'new', 'task', 'New')
        self.assertIsNone(self.row(ident))

    def test_reply_ttl_does_not_silently_resolve_an_unconfirmed_send(self):
        now = time.time()
        ident, _ = self.journal.add('owner', 'unconfirmed', 'task', 'Result')
        self.journal.update(ident, 'completed')
        self.reply(ident, 'unconfirmed', now - RESULT_RETENTION_SECONDS - 1)
        self.old(ident)
        accepted, _ = self.journal.add('owner', 'accepted', 'task', 'Other result')
        self.journal.update(accepted, 'completed')
        boundary = self.reply(accepted, 'accepted', now - RESULT_RETENTION_SECONDS)
        self.seed()
        with patch('task_journal.time.time', return_value=now):
            self.journal.add_template('owner', 'new', '', self.snapshot())
        self.assertEqual(self.row(ident)['delivery_state'], 'unconfirmed')
        with self.journal.connect(read_only=True) as db:
            rows = db.execute('SELECT id FROM task_replies').fetchall()
            self.assertEqual(rows, [(boundary,)])

    def test_pruned_used_batch_metadata_disappears_atomically_but_source_survives(self):
        store, batch, source = self.ready()
        ident, _, _ = self.journal.add_batch('owner', 'bound', 'Run', batch['id'])
        self.journal.update(ident, 'completed')
        self.reply(ident)
        self.old(ident)
        self.seed()
        self.journal.add_template('owner', 'new', '', self.snapshot())
        self.assertIsNone(self.row(ident))
        self.assertIsNone(store.get('owner', batch['id']))
        with self.journal.connect(read_only=True) as db:
            for table in ('attachment_batch_files', 'attachment_batch_intakes'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE batch_id=?',
                                            (batch['id'],)).fetchone()[0], 0)
        self.assertEqual(source.read_bytes(), b'isolated attachment bytes')

    def test_recoverable_and_unconfirmed_batches_keep_catalog_and_saved_manifest(self):
        cases = []
        for state in ('recoverable', 'completed'):
            store, batch, _ = self.ready(name=state)
            ident, _, manifest = self.journal.add_batch('owner', state, 'Original', batch['id'],
                                                       template=self.snapshot())
            self.journal.update(ident, state)
            if state == 'completed':
                self.reply(ident, 'unconfirmed')
            self.old(ident)
            cases.append((state, store, batch, ident, manifest))
        self.seed()
        self.journal.add('owner', 'new', 'task', 'New')
        for state, store, batch, ident, manifest in cases:
            self.assertEqual(store.get('owner', batch['id'])['task_id'], ident)
            self.assertEqual(json.loads(self.row(ident)['inputs']), manifest)
            if state == 'recoverable':
                self.assertEqual(self.journal.claim_entry('owner', ident)['inputs'], manifest)

    def test_previous_orphaned_used_batch_is_removed_without_touching_unbound_batches(self):
        store, used, source = self.ready(name='used')
        ident, _, _ = self.journal.add_batch('owner', 'used', 'Run', used['id'])
        with self.journal.connect() as db:
            db.execute('DELETE FROM tasks WHERE id=?', (ident,))  # Simulate the legacy pruner.
        _, ready, _ = self.ready(name='ready')
        cancelled = store.create('owner', 'cancelled')
        self.assertTrue(store.cancel('owner', cancelled['id']))
        opened = store.create('owner', 'open')
        self.journal.add_template('owner', 'new', '', self.snapshot())
        self.assertIsNone(store.get('owner', used['id']))
        for batch in (ready, cancelled, opened):
            self.assertIsNotNone(store.get('owner', batch['id']))
        self.assertTrue(source.exists())

    def test_pending_batch_intake_protects_both_sides_of_an_inconsistent_binding(self):
        store, batch, _ = self.ready()
        ident, _, _ = self.journal.add_batch('owner', 'bound', 'Run', batch['id'])
        self.journal.update(ident, 'completed')
        self.old(ident)
        with self.journal.connect() as db:
            db.execute("UPDATE attachment_batch_intakes SET state='pending' WHERE batch_id=?", (batch['id'],))
        self.seed()
        self.journal.add('owner', 'new', 'task', 'New')
        self.assertIsNotNone(self.row(ident))
        self.assertEqual(store.get('owner', batch['id'])['pending'], 1)

    def test_pruning_failure_rolls_back_new_batch_receipt_binding_and_reply_cleanup(self):
        store, batch, _ = self.ready()
        snapshot = self.snapshot()
        self.seed()
        old_reply = self.reply('filler-000', created=time.time() - RESULT_RETENTION_SECONDS - 1)
        with self.journal.connect() as db:
            db.execute("CREATE TRIGGER prevent_retention BEFORE DELETE ON tasks "
                       "BEGIN SELECT RAISE(ABORT, 'isolated pruning failure'); END")
        for template in (None, snapshot):
            with self.subTest(template=bool(template)):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.journal.add_batch('owner', 'new', 'Run', batch['id'], template=template)
                self.assertEqual(store.get('owner', batch['id'])['state'], 'ready')
                self.assertEqual(store.get('owner', batch['id'])['task_id'], '')
                with self.journal.connect(read_only=True) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE uid='owner'").fetchone()[0], 0)
                    self.assertIsNotNone(db.execute('SELECT 1 FROM task_replies WHERE id=?', (old_reply,)).fetchone())

    def test_old_database_without_batch_tables_uses_the_same_retention(self):
        self.seed()
        self.journal.add('owner', 'new', 'task', 'New')
        self.assertIsNone(self.row('filler-000'))
        with self.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 200)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='attachment_batches'").fetchone())


if __name__ == '__main__':
    unittest.main()
