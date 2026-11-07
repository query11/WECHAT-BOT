"""Isolated batch/version regressions. No real inbox, models or transport."""
import hashlib
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import attachment_batches as B
import task_artifacts as A


class AttachmentBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.inbox = self.base / 'inbox'
        self.workdir = self.base / 'work'
        self.other = self.base / 'outside'
        for directory in (self.inbox, self.workdir, self.other):
            directory.mkdir()
        self.path = self.base / 'tasks.sqlite'
        self.store = B.AttachmentBatchStore(self.path)
        self.roots = [str(self.inbox), str(self.workdir)]

    def entry(self, name='one.txt', raw=b'original input', uid='alice', orig=None):
        path = self.inbox / name
        path.write_bytes(raw)
        return dict(uid=uid, name=name, orig=orig or name, at=time.time(),
                    bytes=len(raw), md5=hashlib.md5(raw).hexdigest())

    def records(self, entries=None, uid='alice'):
        return B.collect_input_records(entries or [self.entry()], self.inbox,
                                       self.roots, uid)

    def intake(self, batch, entries=None, mid='message-1', failed=0):
        self.assertTrue(self.store.begin_intake(batch['uid'], batch['id'], mid))
        self.store.finish_intake(batch['uid'], batch['id'], mid,
                                 self.records(entries, batch['uid']), failed)
        return self.store.get(batch['uid'], batch['id'])

    def ready(self, name='报销', entries=None):
        batch = self.store.create_recent('alice', name, self.records(entries))
        return self.store.seal('alice', batch['id'])

    def test_constructor_never_recovers_or_modifies_task_journal(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE tasks (id TEXT,state TEXT,payload TEXT)')
            db.execute("INSERT INTO tasks VALUES ('keep','queued','sensitive')")
        B.AttachmentBatchStore(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT * FROM tasks').fetchall(),
                             [('keep', 'queued', 'sensitive')])

    def test_one_open_per_user_owner_isolation_and_recent_list(self):
        first = self.store.create('alice', '发票')
        other = self.store.create('bob', '模板')
        with self.assertRaises(B.BatchError):
            self.store.create('alice', '重复')
        self.assertEqual(self.store.active('alice')['id'], first['id'])
        self.assertIsNone(self.store.get('bob', first['id']))
        self.assertIsNone(self.store.get('alice', other['id']))
        with self.assertRaises(B.BatchError):
            self.store.begin_intake('bob', first['id'], 'cross-owner')
        self.assertEqual([x['id'] for x in self.store.list('alice')], [first['id']])
        self.assertEqual(self.store.get('alice', 'latest')['id'], first['id'])

    def test_selected_batches_do_not_automatically_collect_later_attachments(self):
        selected = self.store.create('alice', '只选历史', collect=False)
        self.assertEqual(selected['intake_mode'], 'selected')
        self.assertIsNone(self.store.active('alice'))
        self.assertTrue(self.store.begin_intake('alice', selected['id'], 'selected-prepare'))
        self.store.finish_intake('alice', selected['id'], 'selected-prepare', self.records())
        self.assertIsNone(self.store.active('alice'))
        self.assertTrue(self.store.cancel('alice', selected['id']))
        recent = self.store.create_recent('alice', '历史清单', self.records())
        self.assertEqual(recent['intake_mode'], 'selected')
        self.assertIsNone(self.store.active('alice'))

    def test_no_empty_or_pending_seal_and_message_deduplication(self):
        batch = self.store.create('alice', '发票')
        with self.assertRaises(B.BatchError):
            self.store.seal('alice', batch['id'])
        self.assertTrue(self.store.begin_intake('alice', batch['id'], 'm1'))
        self.assertFalse(self.store.begin_intake('alice', batch['id'], 'm1'))
        self.assertEqual(self.store.get('alice', batch['id'])['pending'], 1)
        with self.assertRaises(B.BatchError):
            self.store.seal('alice', batch['id'])
        records = self.records()
        self.store.finish_intake('alice', batch['id'], 'm1', records)
        self.store.finish_intake('alice', batch['id'], 'm1', records)
        batch = self.store.seal('alice', batch['id'])
        self.assertEqual((batch['state'], batch['pending'], len(batch['files'])), ('ready', 0, 1))
        with self.assertRaises(B.BatchError):
            self.store.begin_intake('alice', batch['id'], 'm2')

    def test_partial_failure_is_explicit_and_cannot_seal(self):
        batch = self.intake(self.store.create('alice', '发票'), failed=1)
        self.assertEqual(len(batch['files']), 1)
        self.assertEqual(batch['pending'], 0)
        self.assertEqual(batch['failed'], 1)
        self.assertTrue(batch['errors'])
        with self.assertRaises(B.BatchError):
            self.store.seal('alice', batch['id'])

    def test_empty_completion_is_failed_not_silent_success(self):
        batch = self.store.create('alice', '空消息')
        self.store.begin_intake('alice', batch['id'], 'm1')
        self.store.finish_intake('alice', batch['id'], 'm1', [])
        self.assertEqual(self.store.get('alice', batch['id'])['failed'], 1)
        with self.assertRaises(B.BatchError):
            self.store.seal('alice', batch['id'])

    def test_invalid_or_foreign_file_receipts_cannot_enter_batch(self):
        batch = self.store.create('alice', '发票')
        records = self.records()
        records[0]['uid'] = 'bob'
        self.store.begin_intake('alice', batch['id'], 'm1')
        self.store.finish_intake('alice', batch['id'], 'm1', records)
        actual = self.store.get('alice', batch['id'])
        self.assertEqual(actual['files'], [])
        self.assertEqual(actual['failed'], 1)
        with self.assertRaises(B.BatchError):
            self.store.create_recent('alice', 'foreign', records)

    def test_cancelled_intake_cannot_move_to_next_batch_or_delete_file(self):
        batch = self.store.create('alice', '旧')
        records = self.records()
        self.store.begin_intake('alice', batch['id'], 'm1')
        self.assertTrue(self.store.cancel('alice', batch['id']))
        newer = self.store.create('alice', '新')
        self.store.finish_intake('alice', batch['id'], 'm1', records)
        self.assertEqual(self.store.get('alice', batch['id'])['files'], [])
        self.assertEqual(self.store.get('alice', newer['id'])['files'], [])
        self.assertFalse(self.store.begin_intake('alice', newer['id'], 'm1'))
        self.assertTrue(Path(records[0]['path']).exists())

    def test_restart_records_interruption_and_refuses_late_completion(self):
        batch = self.store.create('alice', '未完成')
        self.store.begin_intake('alice', batch['id'], 'm1')
        restarted = B.AttachmentBatchStore(self.path)
        actual = restarted.get('alice', batch['id'])
        self.assertEqual((actual['pending'], actual['failed']), (0, 1))
        self.assertIn('重启', actual['errors'][0])
        with self.assertRaises(B.BatchError):
            restarted.seal('alice', batch['id'])
        with self.assertRaises(B.BatchError):
            restarted.finish_intake('alice', batch['id'], 'm1', self.records())

    def test_file_positions_append_only_and_duplicate_receipt_is_not_new_file(self):
        batch = self.store.create('alice', '文件组')
        first = self.entry('one.txt')
        self.intake(batch, [first], 'm1')
        second = self.entry('two.txt')
        self.intake(batch, [first, second], 'm2')
        actual = self.store.get('alice', batch['id'])
        self.assertEqual([(x['index'], x['name']) for x in actual['files']],
                         [(1, 'one.txt'), (2, 'two.txt')])
        self.assertEqual(actual['failed'], 0)

    def test_same_path_changed_between_messages_blocks_batch(self):
        batch = self.store.create('alice', '文件组')
        self.intake(batch, [self.entry('one.txt', b'first')], 'm1')
        self.intake(batch, [self.entry('one.txt', b'later')], 'm2')
        actual = self.store.get('alice', batch['id'])
        self.assertEqual(len(actual['files']), 1)
        self.assertEqual(actual['failed'], 1)
        with self.assertRaises(B.BatchError):
            self.store.seal('alice', batch['id'])

    def test_database_failure_rolls_back_files_and_keeps_pending(self):
        batch = self.store.create('alice', '发票')
        self.store.begin_intake('alice', batch['id'], 'm1')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER deny_finish BEFORE UPDATE ON attachment_batch_intakes "
                       "BEGIN SELECT RAISE(ABORT,'injected persistence failure'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.finish_intake('alice', batch['id'], 'm1', self.records())
        actual = self.store.get('alice', batch['id'])
        self.assertEqual((actual['files'], actual['pending'], actual['failed']), ([], 1, 0))

    def test_database_lock_fails_fast_without_mutating_receipt(self):
        batch = self.store.create('alice', '发票')
        self.store.begin_intake('alice', batch['id'], 'm1')
        with closing(sqlite3.connect(self.path)) as lock, lock:
            lock.execute('BEGIN IMMEDIATE')
            start = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                self.store.finish_intake('alice', batch['id'], 'm1', self.records())
            self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.store.get('alice', batch['id'])['pending'], 1)

    def test_template_changes_only_open_exact_members_and_zero_clears(self):
        batch = self.store.create_recent('alice', '发票', self.records([
            self.entry('one.txt'), self.entry('two.txt')]))
        self.assertEqual(batch['state'], 'open')
        actual = self.store.set_template('alice', batch['id'], 2)
        self.assertEqual([x['role'] for x in actual['files']], ['input', 'template'])
        self.assertEqual([x['role'] for x in self.store.set_template('alice', batch['id'], 0)['files']], ['input', 'input'])
        with self.assertRaises(B.BatchError):
            self.store.set_template('alice', batch['id'], 3)
        self.store.seal('alice', batch['id'])
        with self.assertRaises(B.BatchError):
            self.store.set_template('alice', batch['id'], 1)

    def test_ready_can_cancel_used_cannot_and_bind_release_is_owner_scoped(self):
        first = self.ready('可取消')
        self.assertTrue(self.store.cancel('alice', first['id']))
        second = self.ready('执行')
        bound = self.store.bind_task('alice', second['id'], 'task-1')
        self.assertEqual((bound['state'], bound['task_id']), ('used', 'task-1'))
        self.assertFalse(self.store.cancel('alice', second['id']))
        self.assertEqual(self.store.bind_task('alice', second['id'], 'task-1')['task_id'], 'task-1')
        with self.assertRaises(B.BatchError):
            self.store.bind_task('alice', second['id'], 'task-2')
        self.assertFalse(self.store.release_task('alice', second['id'], 'task-2'))
        with self.assertRaises(B.BatchError):
            self.store.release_task('bob', second['id'], 'task-1')
        self.assertTrue(self.store.release_task('alice', second['id'], 'task-1'))
        self.assertTrue(self.store.release_task('alice', second['id'], 'task-1'))
        self.assertEqual(self.store.get('alice', second['id'])['state'], 'ready')

    def test_real_inbox_verification_md5_scope_and_path_metadata_is_ignored(self):
        entry = self.entry()
        entry['path'] = str(self.other / 'not-trusted.txt')
        actual = self.records([entry])[0]
        self.assertEqual(actual['path'], str(self.inbox / 'one.txt'))
        self.assertEqual(actual['sha256'], hashlib.sha256(b'original input').hexdigest())
        for mutate in (dict(uid='bob'), dict(name='../one.txt'), dict(name='C:\\one.txt'),
                       dict(name='one.txt:stream'), dict(bytes=0), dict(at=float('nan')),
                       dict(md5='bad'), dict(orig='id_rsa'), dict(orig='config.json')):
            with self.subTest(mutate=mutate), self.assertRaises(B.BatchError):
                self.records([dict(entry, **mutate)])
        (self.inbox / entry['name']).write_bytes(b'changed! input')
        with self.assertRaises(B.BatchError):
            self.records([entry])
        (self.inbox / entry['name']).unlink()
        with self.assertRaises(B.BatchError):
            self.records([entry])

    def test_every_entry_must_belong_to_owner_and_duplicate_input_rejects_all(self):
        one, two = self.entry('one.txt'), self.entry('two.txt', uid='bob')
        with self.assertRaises(B.BatchError):
            self.records([one, two])
        with self.assertRaises(B.BatchError):
            self.records([one, one])
        with self.assertRaises(B.BatchError):
            B.collect_input_records([one], self.inbox, [self.workdir], 'alice')

    def test_bounded_file_counts_bytes_and_intake_overflow_remains_explicit(self):
        entries = [self.entry('%d.txt' % x) for x in range(13)]
        with self.assertRaises(B.BatchError):
            self.records(entries)
        records = self.records(entries[:2])
        with patch.object(B, 'MAX_BATCH_BYTES', 2):
            with self.assertRaises(B.BatchError):
                self.store.create_recent('alice', '太大', records)
        batch = self.store.create('alice', '满批次')
        self.intake(batch, entries[:12])
        self.intake(batch, entries[12:], mid='overflow')
        actual = self.store.get('alice', batch['id'])
        self.assertEqual(len(actual['files']), 12)
        self.assertGreater(actual['failed'], 0)
        with self.assertRaises(B.BatchError):
            self.store.seal('alice', batch['id'])

    def test_snapshots_keep_all_inputs_fixed_then_cleanup(self):
        entries = [self.entry('one.txt', b'one'), self.entry('two.txt', b'two')]
        batch = self.store.create_recent('alice', '输入', self.records(entries))
        self.store.set_template('alice', batch['id'], 2)
        batch = self.store.seal('alice', batch['id'])
        originals = [x['path'] for x in batch['files']]
        with B.frozen_batch_inputs(batch['files'], self.roots, self.workdir) as manifest:
            snapshots = [Path(x['path']) for x in manifest]
            self.assertEqual([x['role'] for x in manifest], ['input', 'template'])
            self.assertEqual([x['original_path'] for x in manifest], originals)
            self.assertTrue(all('.wechat-batch-inputs-' in str(x) for x in snapshots))
            self.assertEqual([x.read_bytes() for x in snapshots], [b'one', b'two'])
            self.assertEqual([x['mtime_ns'] for x in manifest], [x.stat().st_mtime_ns for x in snapshots])
            self.assertEqual([x['original_mtime_ns'] for x in manifest], [x['mtime_ns'] for x in batch['files']])
            Path(originals[0]).write_bytes(b'after freeze edit')
            self.assertEqual(snapshots[0].read_bytes(), b'one')
            self.entry('new.txt', b'later upload')
            self.assertEqual(len(manifest), 2)
        self.assertTrue(all(not path.exists() for path in snapshots))
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_changed_missing_invalid_or_outside_inputs_never_yield_partial_manifest(self):
        records = self.records([self.entry('one.txt'), self.entry('two.txt')])
        Path(records[1]['path']).write_bytes(b'changed')
        with self.assertRaises(B.BatchError):
            with B.frozen_batch_inputs(records, self.roots, self.workdir):
                self.fail('partial manifest must never be supplied')
        self.assertEqual(list(self.workdir.iterdir()), [])
        records = self.records([self.entry('one.txt'), self.entry('two.txt')])
        Path(records[1]['path']).unlink()
        with self.assertRaises(B.BatchError):
            with B.frozen_batch_inputs(records, self.roots, self.workdir):
                self.fail('missing input must never be supplied')
        with self.assertRaises(B.BatchError):
            with B.frozen_batch_inputs(records, self.roots, self.other):
                self.fail('out-of-scope staging must never be created')
        self.assertEqual(list(self.other.iterdir()), [])
        with self.assertRaises(B.BatchError):
            with B.frozen_batch_inputs([dict(records[0], sha256='')], self.roots, self.workdir):
                self.fail('invalid version must never be supplied')

    def test_task_exception_cleans_snapshots_without_touching_originals(self):
        records = self.records()
        with self.assertRaisesRegex(RuntimeError, 'model failure'):
            with B.frozen_batch_inputs(records, self.roots, self.workdir) as manifest:
                self.assertTrue(Path(manifest[0]['path']).exists())
                raise RuntimeError('model failure')
        self.assertEqual(list(self.workdir.iterdir()), [])
        self.assertEqual(Path(records[0]['path']).read_bytes(), b'original input')

    def test_symlink_source_and_staging_are_rejected(self):
        target = self.entry('target.txt')
        try:
            os.symlink(self.inbox / target['name'], self.inbox / 'link.txt')
            os.symlink(self.workdir, self.base / 'worklink', target_is_directory=True)
        except OSError as exc:
            self.skipTest('symlink not supported for current user: %s' % type(exc).__name__)
        with self.assertRaises(B.BatchError):
            self.records([dict(target, name='link.txt')])
        records = self.records([target])
        with self.assertRaises(B.BatchError):
            with B.frozen_batch_inputs(records, [self.base], self.base / 'worklink'):
                self.fail('staging symlink must be rejected')


if __name__ == '__main__':
    unittest.main(verbosity=2)
