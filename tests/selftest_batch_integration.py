"""Real Message/SQLite/queue/files with fake model and transport, no user data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from attachment_batches import AttachmentBatchStore, BatchError, collect_input_records
from bridge_inbox import InboxStore
from ilink_client import Message, ILinkError
from runner_result import RunnerResult
from task_control import TaskControl, TaskQueue
from task_history import read_history
from task_journal import TaskJournal
from wechat_bridge import Bridge
from tests import selftest_standby


class BatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inbox = self.root / 'inbox'
        self.workdir = self.root / 'work'
        self.inbox.mkdir(); self.workdir.mkdir()
        for target in ('bridge_artifacts.HERE', 'bridge_files.HERE', 'bridge_batches.HERE'):
            current = patch(target, str(self.root)); current.start(); self.addCleanup(current.stop)
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        self.b = fixture.bridge
        self.b.queue = TaskQueue()
        self.b.queue_max = 20
        self.b.workdir = str(self.workdir)
        self.b.send_roots = [str(self.root)]
        self.b.send_file_enable = True
        self.b.send_file_limit = 3
        self.b.send_file_max_mb = 20
        self.b.enable_typing = False
        self.b.notice_after = 0
        self.b._mask = str
        self.b._set_stage = Mock()
        self.b._finish_draining = Mock()
        self.b.runner = Mock()
        self.b.client = Mock(account_id='bot')
        self.b._awake.update(('owner', 'other'))
        self.b.journal = TaskJournal(self.root / '.runtime/tasks.sqlite')
        self.b.batch_store = AttachmentBatchStore(self.b.journal.path)
        self.b.inbox_store = InboxStore(str(self.root / 'inbox.json'))
        self.b.inbox_enable = True
        self.b.inbox_dir = str(self.inbox)
        self.b.inbox_max_bytes = 20 * 1024 * 1024
        self.b.inbox_quota_bytes = 0
        self.b.inbox_ask_seconds = 60
        self.b._inbox_start = Bridge._inbox_start.__get__(self.b)
        self.b._recent_inbox_context = Mock(return_value='OLD RECENT CONTEXT MUST NOT ENTER A BATCH')
        self.counter = 0

    def message(self, text='', uid='owner', items=None, mid=None):
        self.counter += 1
        return Message(dict(from_user_id=uid, message_id=mid or 'mid-%d' % self.counter,
                            context_token='current', item_list=items if items is not None else
                            [dict(type=1, text_item=dict(text=text))]))

    def entry(self, name='one.txt', data=b'first input', uid='owner', at=None):
        path = self.inbox / name
        path.write_bytes(data)
        entry = dict(uid=uid, name=name, orig=name, bytes=len(data),
                     md5=hashlib.md5(data).hexdigest(), at=at or time.time(), kind='文件')
        self.b.inbox_store.add(entry)
        return entry

    def ready(self, entries=None, uid='owner'):
        entries = entries or [self.entry()]
        records = collect_input_records(entries, self.inbox, [self.root], uid)
        batch = self.b.batch_store.create_recent(uid, '报销', records)
        return self.b.batch_store.seal(uid, batch['id'])

    def replies(self):
        return '\n'.join(str(call.args[1]) for call in self.b.reply.call_args_list)

    def run_command(self, batch, body='汇总附件', uid='owner', mid=None):
        msg = self.message('/batch run %s %s' % (batch['id'], body), uid, mid=mid)
        self.assertTrue(self.b._batch_command(msg, msg.text))
        return msg

    def queued(self, batch=None, body='汇总附件'):
        batch = batch or self.ready()
        msg = self.run_command(batch, body)
        self.assertEqual(self.b.queue.qsize(), 1, self.replies())
        item = self.b.queue.get_nowait(); self.b.queue.task_done()
        msg._receipt_outcome = 'completed'
        return msg, item[1], item[2]['batch']

    def process_download(self, entries, msg=None, body='看看这些附件'):
        msg = msg or self.message(body, items=[dict(file_item=dict(file_name='attachment.txt'))])
        taken, note = self.b._inbox_start(msg, msg.from_user_id, body)
        self.assertTrue(taken); self.assertIsNone(note)
        item = self.b._inbox_q.get_nowait()
        msg._task_control = TaskControl()
        msg._inbox_summary = ''; msg._inbox_outputs = []
        infos = [dict(kind='文件', name=e['orig'], size=e['bytes']) for e in entries]
        with patch('bridge_inbox.ilink_client.media_download_info', side_effect=infos), \
                patch.object(self.b, '_inbox_save_one', side_effect=[(e, '') for e in entries]):
            state = self.b._process_inbox(item)
        self.b._inbox_q.task_done()
        self.b.journal.update(msg._inbox_journal_id, state)
        return msg, state

    def test_live_intake_binds_before_download_and_body_does_not_auto_analyze(self):
        batch = self.b.batch_store.create('owner', '本次发票')
        msg, state = self.process_download([self.entry()])
        self.assertEqual(state, 'completed')
        self.assertEqual(msg._batch_intake_id, batch['id'])
        actual = self.b.batch_store.get('owner', batch['id'])
        self.assertEqual((actual['pending'], len(actual['files'])), (0, 1))
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()
        self.assertIn('正文未自动处理', self.replies())

    def test_sealed_manifest_persisted_with_receipt_and_later_uploads_do_not_mix(self):
        batch = self.ready([self.entry('one.txt'), self.entry('two.txt')])
        msg = self.run_command(batch)
        manifest = self.b.journal.inputs('owner', msg._journal_id)
        actual = self.b.batch_store.get('owner', batch['id'])
        self.assertEqual((actual['state'], actual['task_id']), ('used', msg._journal_id))
        self.assertEqual([x['name'] for x in manifest['files']], ['one.txt', 'two.txt'])
        self.entry('new.txt', b'later upload')
        self.assertEqual(self.b.journal.inputs('owner', msg._journal_id), manifest)
        self.assertEqual(self.b.queue.queue[0][2]['batch'], manifest)

    def test_actual_model_prompt_reads_only_frozen_manifest(self):
        msg, body, manifest = self.queued(self.ready([self.entry('one.txt'), self.entry('two.txt')]))
        self.entry('later.txt', b'later upload')
        snapshots = []
        def runner(uid, prompt):
            self.assertEqual(uid, 'owner')
            self.assertIn('固定输入清单', prompt)
            self.assertNotIn('OLD RECENT', prompt)
            self.assertNotIn('later.txt', prompt)
            snapshots.extend(Path(row['path']) for row in msg._batch_frozen)
            self.assertTrue(all(path.exists() for path in snapshots))
            return RunnerResult('已完成汇总')
        self.b.runner.run.side_effect = runner
        self.b._run_batch(msg, body, manifest)
        self.b._recent_inbox_context.assert_not_called()
        self.assertTrue(all(not path.exists() for path in snapshots))
        self.assertEqual(msg._batch_frozen, [])
        self.assertFalse(list(self.workdir.glob('.wechat-batch-inputs-*')))

    def test_any_changed_input_prevents_model_call_and_partial_task(self):
        msg, body, manifest = self.queued(self.ready([self.entry('one.txt'), self.entry('two.txt')]))
        Path(manifest['files'][1]['path']).write_bytes(b'changed version')
        self.b._run_batch(msg, body, manifest)
        self.b.runner.run.assert_not_called()
        self.assertEqual(msg._receipt_outcome, 'failed')
        self.assertIn('批次未执行', self.replies())
        self.assertFalse(list(self.workdir.glob('.wechat-batch-inputs-*')))

    def test_resume_retains_original_manifest_after_new_attachment(self):
        batch = self.ready()
        original = self.run_command(batch)
        old_inputs = self.b.journal.inputs('owner', original._journal_id)
        self.b.queue.get_nowait(); self.b.queue.task_done()
        self.b.journal = TaskJournal(self.b.journal.path)
        self.entry('new.txt', b'new arrival')
        recovery = self.message('/resume ' + original._journal_id)
        self.assertIsNone(self.b._enqueue((recovery, '', None), recovery,
                                         recovery_id=original._journal_id))
        item = self.b.queue.get_nowait(); self.b.queue.task_done()
        self.assertEqual(item[1], '汇总附件')
        self.assertEqual(item[2]['batch'], old_inputs)
        self.assertEqual(recovery._journal_id, original._journal_id)

    def test_corrupt_recovery_inputs_never_downgrade_to_text_task(self):
        for bad in ('[]', '{}', '{invalid', json.dumps(dict(uid='other', files=[]))):
            with self.subTest(bad=bad):
                ident = 'corrupt-' + str(self.counter); self.counter += 1
                with self.b.journal.connect() as db:
                    db.execute('INSERT INTO tasks(id,uid,message_id,kind,label,state,updated,payload,inputs) VALUES (?,?,?,?,?,?,?,?,?)',
                               (ident, 'owner', ident, 'batch_task', '损坏', 'recoverable', time.time(), 'do work', bad))
                with self.assertRaises((BatchError, ValueError)):
                    self.b.journal.claim_entry('owner', ident)
                with self.b.journal.connect(read_only=True) as db:
                    self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (ident,)).fetchone()[0], 'recoverable')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()

    def test_queue_full_before_receipt_leaves_batch_ready(self):
        batch = self.ready()
        self.b.queue = TaskQueue(maxsize=1); self.b.queue_max = 1
        filler = self.message(); self.b.queue.put_nowait((filler, 'existing', None))
        msg = self.run_command(batch)
        self.assertFalse(hasattr(msg, '_journal_id'))
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['state'], 'ready')
        self.assertEqual(self.b.queue.qsize(), 1)

    def test_queue_race_after_receipt_rolls_back_batch_and_cancels_receipt(self):
        batch = self.ready()
        with patch.object(self.b.queue, 'put_nowait', side_effect=queue.Full):
            msg = self.run_command(batch)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['state'], 'ready')
        with self.b.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (msg._journal_id,)).fetchone()[0], 'cancelled')

    def test_receipt_insert_failure_is_atomic_and_never_consumes_batch(self):
        batch = self.ready()
        with self.b.journal.connect() as db:
            db.execute("CREATE TRIGGER reject_batch BEFORE INSERT ON tasks WHEN NEW.kind='batch_task' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        self.run_command(batch)
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['state'], 'ready')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()

    def test_other_user_and_duplicate_batch_run_cannot_dispatch(self):
        batch = self.ready()
        self.run_command(batch, uid='other')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.run_command(batch)
        self.run_command(batch)
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertIn('不能重复', self.replies())

    def test_disabled_journal_rejects_batch_commands_without_model(self):
        self.b.journal = None
        msg = self.message('/batch new disabled')
        self.assertTrue(self.b._batch_command(msg, msg.text))
        self.assertIn('记录未启用', self.replies())
        self.assertEqual(self.b.batch_store.list('owner'), [])
        self.b.runner.run.assert_not_called()

    def test_input_snapshots_and_originals_cannot_be_registered_as_outputs(self):
        msg, body, manifest = self.queued()
        def runner(uid, prompt):
            original = manifest['files'][0]['path']
            snapshot = msg._batch_frozen[0]['path']
            return RunnerResult('\n'.join('[[产物]]' + json.dumps(dict(path=p)) for p in (original, snapshot)))
        self.b.runner.run.side_effect = runner
        self.b._run_batch(msg, body, manifest)
        self.assertEqual([x['status'] for x in msg._artifacts], ['rejected', 'rejected'])
        self.assertEqual(msg._receipt_outcome, 'failed')
        self.b.client.send_file.assert_not_called()

    def test_original_input_reply_uses_frozen_bytes_and_keeps_copies_until_send_returns(self):
        msg, body, manifest = self.queued()
        original = Path(manifest['files'][0]['path'])
        originals, snapshots, uploads = [], [], []
        def runner(uid, prompt):
            snapshots.extend(Path(row['path']) for row in msg._batch_frozen)
            originals.extend(row['original_path'] for row in msg._batch_frozen)
            original.write_bytes(b'later original edit')
            return RunnerResult('回传输入\n[[发文件]]' + str(original))
        def send(uid, upload, **options):
            uploads.append(Path(upload))
            self.assertTrue(all(path.exists() for path in snapshots))
            self.assertEqual(Path(upload).read_bytes(), b'first input')
            self.assertEqual(options['file_name'], original.name)
            return original.name, len(b'first input')
        self.b.runner.run.side_effect = runner
        self.b.client.send_file.side_effect = send
        self.b._run_batch(msg, body, manifest)
        self.b.client.send_file.assert_called_once()
        self.assertTrue(all(not path.exists() for path in snapshots + uploads))
        self.assertIn(str(original), self.replies())
        self.assertEqual(msg._output_files, originals)

    def test_model_changed_input_snapshot_is_not_uploaded(self):
        msg, body, manifest = self.queued()
        def runner(uid, prompt):
            snapshot = Path(msg._batch_frozen[0]['path'])
            snapshot.write_bytes(b'model altered input')
            return RunnerResult('already sent\n[[发文件]]' + str(snapshot))
        self.b.runner.run.side_effect = runner
        self.b._run_batch(msg, body, manifest)
        self.b.client.send_file.assert_not_called()
        self.assertEqual(msg._receipt_outcome, 'failed')
        self.assertNotIn('already sent', self.replies())
        self.assertFalse(list(self.workdir.glob('.wechat-batch-inputs-*')))

    def test_transport_unconfirmed_still_cleans_all_task_input_copies(self):
        msg, body, manifest = self.queued()
        self.b.runner.run.side_effect = lambda uid, prompt: RunnerResult(
            'sent\n[[发文件]]' + msg._batch_frozen[0]['path'])
        self.b.client.send_file.side_effect = ILinkError('transport acceptance lost')
        self.b._run_batch(msg, body, manifest)
        self.assertEqual(msg._receipt_outcome, 'uncertain')
        self.assertIn('未获得发送成功确认', self.replies())
        self.assertFalse(list(self.workdir.glob('.wechat-batch-inputs-*')))
        self.assertFalse(list((self.root / '.runtime/artifact-transfers').rglob('*')))

    def test_recent_is_background_fixed_selection_and_never_collects_later_upload(self):
        self.entry('one.txt', at=1); self.entry('two.txt', at=2)
        self.entry('foreign.txt', uid='other', at=3)
        msg = self.message('/batch recent 2 历史附件')
        self.assertTrue(self.b._batch_command(msg, msg.text))
        self.assertEqual(self.b._inbox_q.qsize(), 1, self.replies())
        self.assertEqual([e['name'] for e in msg._batch_recent_entries], ['two.txt', 'one.txt'])
        batch = self.b.batch_store.get('owner', msg._batch_intake_id)
        self.assertEqual((batch['pending'], batch['intake_mode']), (1, 'selected'))
        self.assertIsNone(self.b.batch_store.active('owner'))
        with self.assertRaises(BatchError):
            self.b.batch_store.seal('owner', batch['id'])
        later = self.message(items=[dict(file_item=dict(file_name='later.txt'))])
        self.b._batch_begin_inbox(later, 'owner')
        self.assertFalse(hasattr(later, '_batch_intake_id'))
        self.entry('later.txt', b'new upload', at=4)
        self.b._inbox_q.get_nowait(); self.b._inbox_q.task_done()
        msg._task_control = TaskControl(); msg._inbox_summary = ''; msg._inbox_outputs = []
        self.assertEqual(self.b._process_batch_recent(msg), 'completed')
        batch = self.b.batch_store.seal('owner', batch['id'])
        self.assertEqual([r['name'] for r in batch['files']], ['two.txt', 'one.txt'])
        self.b.runner.run.assert_not_called()

    def test_recent_missing_newest_rejects_whole_selection_without_substitution(self):
        self.entry('old.txt', at=1); self.entry('one.txt', at=2); self.entry('two.txt', at=3)
        msg = self.message('/batch recent 2 历史附件')
        self.b._batch_command(msg, msg.text)
        (self.inbox / 'two.txt').unlink()
        self.b._inbox_q.get_nowait(); self.b._inbox_q.task_done()
        msg._task_control = TaskControl(); msg._inbox_summary = ''; msg._inbox_outputs = []
        self.assertEqual(self.b._process_batch_recent(msg), 'failed')
        actual = self.b.batch_store.get('owner', msg._batch_intake_id)
        self.assertEqual(actual['files'], [])
        self.assertGreater(actual['failed'], 0)
        with self.assertRaises(BatchError):
            self.b.batch_store.seal('owner', actual['id'])

    def test_intake_failure_and_quota_refusal_release_pending_but_block_seal(self):
        batch = self.b.batch_store.create('owner', '未收完')
        msg = self.message(items=[dict(file_item=dict(file_name='one.txt'))])
        self.b._batch_begin_inbox(msg, 'owner')
        msg._inbox_journal_id, _ = self.b.journal.add('owner', msg.message_id, 'inbox', '接收')
        self.b._inbox_pending['owner'] = dict(msg=msg, at=time.time(), plan=[], body='')
        self.assertIn('接收完成前', self.b._batch_finish_inbox(msg, 'waiting'))
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['pending'], 1)
        self.assertIn('不收', self.b._resolve_inbox_ask('owner', '不删'))
        actual = self.b.batch_store.get('owner', batch['id'])
        self.assertEqual((actual['pending'], actual['failed']), (0, 1))
        with self.assertRaises(BatchError):
            self.b.batch_store.seal('owner', batch['id'])

    def test_standby_and_stopall_terminate_quota_pending_batch_receipts(self):
        for operation in ('standby', 'stopall'):
            with self.subTest(operation=operation):
                self.b._awake.add('owner')
                batch = self.b.batch_store.create('owner', '等待配额')
                msg = self.message(items=[dict(file_item=dict(file_name='one.txt'))])
                self.b._batch_begin_inbox(msg, 'owner')
                msg._inbox_journal_id, _ = self.b.journal.add('owner', msg.message_id, 'inbox', '等待收件')
                self.b._inbox_pending['owner'] = dict(msg=msg, at=time.time(), plan=[], body='')
                if operation == 'standby':
                    command = self.message('结束对话')
                    self.assertTrue(self.b._standby_gate(command, 'owner', command.text))
                else:
                    self.b._stop_running('owner', all_of_queue=True)
                actual = self.b.batch_store.get('owner', batch['id'])
                self.assertEqual((actual['pending'], actual['failed']), (0, 1))
                self.assertNotIn('owner', self.b._inbox_pending)
                self.assertTrue(self.b.batch_store.cancel('owner', batch['id']))

    def test_batch_queue_edit_rejected_move_cancel_allowed_without_input_mutation(self):
        batch = self.ready()
        msg = self.run_command(batch)
        inputs = self.b.journal.inputs('owner', msg._journal_id)
        answer = self.b._admin_queue_manage('owner', 'edit', msg._journal_id, 'replacement')
        self.assertIn('只有普通文字', answer)
        self.assertEqual(self.b.queue.queue[0][1], '汇总附件')
        self.assertEqual(self.b.journal.inputs('owner', msg._journal_id), inputs)
        answer = self.b._admin_queue_manage('owner', 'move', msg._journal_id, 1)
        self.assertIn('第 1 条', answer)
        answer = self.b._admin_queue_manage('owner', 'cancel', msg._journal_id, None)
        self.assertIn('已取消', answer)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(self.b.journal.inputs('owner', msg._journal_id), inputs)
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['state'], 'used')

    def test_real_inbox_worker_drains_and_persists_completed_batch_intake(self):
        batch = self.b.batch_store.create('owner', '线程收件')
        entry = self.entry()
        msg = self.message(items=[dict(file_item=dict(file_name='one.txt'))])
        taken, note = self.b._inbox_start(msg, 'owner', '本正文不运行')
        self.assertTrue(taken); self.assertIsNone(note)
        self.b._inbox_q.put(None)
        info = dict(kind='文件', name=entry['orig'], size=entry['bytes'])
        with patch('bridge_inbox.ilink_client.media_download_info', return_value=info), \
                patch.object(self.b, '_inbox_save_one', return_value=(entry, '')):
            thread = threading.Thread(target=self.b._inbox_loop)
            thread.start(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.b._inbox_q.unfinished_tasks, 0)
        self.assertTrue(msg._task_control.finished.is_set())
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['pending'], 0)
        self.assertEqual(len(self.b.batch_store.get('owner', batch['id'])['files']), 1)
        with self.b.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (msg._inbox_journal_id,)).fetchone()[0], 'completed')
        self.b.runner.run.assert_not_called()

    def test_recent_cancelled_before_preparation_cannot_execute_model(self):
        self.entry()
        msg = self.message('/batch recent 1 准备时停止')
        self.b._batch_command(msg, msg.text)
        self.b._inbox_q.get_nowait(); self.b._inbox_q.task_done()
        msg._task_control = TaskControl(); msg._task_control.cancel()
        msg._inbox_summary = ''; msg._inbox_outputs = []
        self.assertEqual(self.b._process_batch_recent(msg), 'cancelled')
        actual = self.b.batch_store.get('owner', msg._batch_intake_id)
        self.assertEqual((actual['pending'], actual['failed'], actual['files']), (0, 1, []))
        self.b.runner.run.assert_not_called()

    def test_input_history_reader_is_read_only_and_old_schema_compatible(self):
        batch = self.ready()
        msg = self.run_command(batch)
        before = Path(self.b.journal.path).read_bytes()
        rows = read_history(self.root)
        batchrow = next(row for row in rows if row['id'] == msg._journal_id)
        self.assertEqual(batchrow['inputs'][0]['path'], str(self.inbox / 'one.txt'))
        self.assertEqual(batchrow['inputs'][0]['role'], 'input')
        self.assertEqual(Path(self.b.journal.path).read_bytes(), before)
        oldroot = self.root / 'old'; (oldroot / '.runtime').mkdir(parents=True)
        with closing(sqlite3.connect(oldroot / '.runtime/tasks.sqlite')) as db, db:
            db.execute('CREATE TABLE tasks(id TEXT,label TEXT,kind TEXT,state TEXT,updated REAL)')
            db.execute("INSERT INTO tasks VALUES ('old','旧任务','task','completed',0)")
        self.assertEqual(read_history(oldroot)[0]['inputs'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
