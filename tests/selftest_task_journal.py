import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from task_journal import TaskJournal
from ilink_client import Message
from tests import selftest_standby


class TaskReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'tasks.sqlite'
        self.journal = TaskJournal(self.path)

    def test_restart_marks_pending_uncertain_without_replaying(self):
        queued, _ = self.journal.add('owner', 'm1', 'task', '排队任务')
        running, _ = self.journal.add('owner', 'm2', 'task', '处理中')
        done, _ = self.journal.add('other', 'm3', 'task', '完成任务')
        self.journal.update(running, 'running')
        self.journal.update(done, 'completed')
        restarted = TaskJournal(self.path)
        self.assertEqual({r['id'] for r in restarted.recent('owner', uncertain=True)}, {queued, running})
        self.assertEqual(restarted.recent('other')[0]['state'], 'completed')
        self.assertEqual(restarted.add('owner', 'm1', 'task', '重投'), (queued, False))

    def test_worker_records_completion_and_drains_without_second_command(self):
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        b = fixture.bridge; b.journal = self.journal; b._mask = str
        started, release = threading.Event(), threading.Event()
        def task(msg, body):
            started.set(); release.wait(3)
        b._run_task = task
        b._worker = threading.Thread(target=b._loop_worker, daemon=True)
        b._worker.start()
        msg = Message({'from_user_id': 'owner', 'message_id': 'accepted'})
        self.assertIsNone(b._enqueue((msg, '原任务', None), msg))
        self.assertTrue(started.wait(2))
        fixture.send('结束对话')
        self.assertIn('owner', b._draining)
        fixture.send('新任务')
        b._dispatch_free.assert_not_called()
        release.set(); b.queue.join()
        b._finish_draining()
        self.assertFalse(b._draining)
        self.assertFalse(b._awake)
        self.assertEqual(self.journal.recent('owner')[0]['state'], 'completed')

    def test_resume_cancels_pending_standby(self):
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        b = fixture.bridge
        b.queue.put('existing')
        fixture.send('结束对话'); fixture.send('开始对话')
        self.assertFalse(b._draining)
        b.queue.get(); b.queue.task_done(); b._finish_draining()
        self.assertIn('owner', b._awake)

    def test_inbox_balances_queue_even_on_failure(self):
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        b = fixture.bridge
        b._process_inbox = Mock(side_effect=RuntimeError('test failure'))
        b._inbox_q.put((SimpleNamespace(), 'owner', ''))
        b._inbox_q.put(None)
        with self.assertLogs('bridge.inbox', level='ERROR'):
            thread = threading.Thread(target=b._inbox_loop)
            thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(b._inbox_q.unfinished_tasks, 0)

    def bridge(self):
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        b = fixture.bridge
        b.journal = self.journal
        b._mask = str
        b.queue_max = 1
        return b

    def test_old_database_migration_does_not_assume_task_unstarted(self):
        old = Path(self.temp.name) / 'old.sqlite'
        with sqlite3.connect(old) as db:
            db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, uid TEXT, message_id TEXT, kind TEXT, label TEXT, state TEXT, updated REAL)')
            db.execute("INSERT INTO tasks VALUES ('old','owner','m','task','旧任务','queued',0)")
        db.close()
        journal = TaskJournal(old)
        self.assertEqual(journal.recent()[0]['state'], 'uncertain')
        self.assertIsNone(journal.claim('owner', 'old'))

    def test_restart_separates_unstarted_running_and_downloads(self):
        pending, _ = self.journal.add('owner', 'p', 'task', '普通任务', payload='完整任务正文')
        active, _ = self.journal.add('owner', 'r', 'task', '已开始', payload='不能重放')
        attachment, _ = self.journal.add('owner', 'a', 'inbox', '附件')
        self.journal.update(active, 'running')
        self.journal.update(attachment, 'running')
        restarted = TaskJournal(self.path)
        states = {r['id']: r['state'] for r in restarted.recent()}
        self.assertEqual(states, {pending: 'recoverable', active: 'uncertain', attachment: 'interrupted'})
        self.assertIsNone(restarted.claim('other', pending))
        self.assertIsNone(restarted.claim('owner', active))
        self.assertEqual(restarted.claim('owner', pending), '完整任务正文')
        self.assertIsNone(restarted.claim('owner', pending))
        self.assertEqual(TaskJournal(self.path).recent('owner')[0]['state'], 'recoverable')

    def test_resume_uses_new_message_and_keeps_original_body(self):
        b = self.bridge()
        ident, _ = self.journal.add('owner', 'original', 'task', '原任务', payload='原任务完整正文')
        b.journal = TaskJournal(self.path)
        msg = Message({'from_user_id': 'owner', 'message_id': 'resume', 'context_token': 'fresh'})
        self.assertIsNone(b._enqueue((msg, '', None), msg, recovery_id=ident))
        actual, body, job = b.queue.get_nowait(); b.queue.task_done()
        self.assertIs(actual, msg)
        self.assertEqual(actual.context_token, 'fresh')
        self.assertEqual(body, '原任务完整正文')
        self.assertIsNone(job)
        self.assertEqual(actual._journal_id, ident)
        self.assertIsNotNone(b._enqueue((msg, '', None), msg, recovery_id=ident))

    def test_full_queue_does_not_consume_recovery(self):
        b = self.bridge(); b.queue = queue.Queue(maxsize=1); b.queue.put('existing')
        ident, _ = self.journal.add('owner', 'p', 'task', '待恢复', payload='正文')
        b.journal = TaskJournal(self.path)
        msg = Message({'from_user_id': 'owner'})
        self.assertIsNotNone(b._enqueue((msg, '', None), msg, recovery_id=ident))
        self.assertEqual(b.journal.recent()[0]['state'], 'recoverable')

    def test_receipt_failure_never_accepts_or_executes_task(self):
        b = self.bridge(); msg = Message({'from_user_id': 'owner'})
        with patch.object(self.journal, 'add', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                b._enqueue((msg, 'task', None), msg)
        self.assertTrue(b.queue.empty())
        b.reply.assert_not_called()

    def test_ack_failure_does_not_lose_durable_task(self):
        b = self.bridge(); b.cfg['task_progress'] = True
        b.reply.side_effect = RuntimeError('network unavailable')
        msg = Message({'from_user_id': 'owner', 'message_id': 'ack'})
        with self.assertLogs('bridge', level='ERROR'):
            self.assertIsNone(b._enqueue((msg, '正文', None), msg))
        self.assertEqual(b.queue.qsize(), 1)
        self.assertEqual(TaskJournal(self.path).claim('owner', msg._journal_id), '正文')

    def test_start_write_failure_prevents_execution_and_preserves_recovery(self):
        b = self.bridge(); msg = Message({'from_user_id': 'owner', 'message_id': 'start'})
        b._enqueue((msg, '正文', None), msg)
        item = b.queue.get_nowait()
        b.queue.get = Mock(side_effect=[item, StopIteration])
        b._run_task = Mock()
        with patch.object(self.journal, 'update', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertLogs('bridge', level='ERROR'), self.assertRaises(StopIteration):
                b._loop_worker()
        b._run_task.assert_not_called()
        self.assertEqual(TaskJournal(self.path).claim('owner', msg._journal_id), '正文')
        self.assertEqual(b.queue.unfinished_tasks, 0)

    def test_resume_still_requires_awake_and_owner(self):
        b = self.bridge()
        ident, _ = self.journal.add('owner', 'r', 'task', '恢复', payload='正文')
        b.journal = TaskJournal(self.path)
        def message(uid):
            return Message({'from_user_id': uid, 'item_list': [{'type': 1, 'text_item': {'text': '/resume ' + ident}}]})
        b.handle(message('owner')); self.assertTrue(b.queue.empty())
        b._awake.add('owner')
        b.handle(message('stranger')); self.assertTrue(b.queue.empty())
        b.handle(message('owner')); self.assertEqual(b.queue.qsize(), 1)

    def test_payload_cleared_after_start_or_cancel(self):
        for state in ('running', 'completed', 'cancelled'):
            ident, _ = self.journal.add('owner', state, 'task', '正文', payload='仅待执行时保留')
            self.journal.update(ident, state)
            with self.journal.connect() as db:
                self.assertIsNone(db.execute('SELECT payload FROM tasks WHERE id=?', (ident,)).fetchone()[0])

    def test_cancel_write_failure_keeps_task_pending(self):
        b = self.bridge(); msg = Message({'from_user_id': 'owner', 'message_id': 'cancel'})
        b._enqueue((msg, '正文', None), msg)
        with patch.object(self.journal, 'update', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertLogs('bridge', level='ERROR'):
                self.assertEqual(b._drain_queue(), 0)
        self.assertEqual(b.queue.qsize(), 1)
        self.assertEqual(b.queue.unfinished_tasks, 1)

    def test_download_interruption_notifies_user_and_balances_queue(self):
        b = self.bridge(); msg = Message({'from_user_id': 'owner', 'message_id': 'download'})
        ident, _ = self.journal.add('owner', 'download', 'inbox', '附件')
        msg._inbox_journal_id = ident
        b._process_inbox = Mock(side_effect=RuntimeError('download interrupted'))
        b._inbox_q.put((msg, 'owner', '')); b._inbox_q.put(None)
        with self.assertLogs('bridge.inbox', level='ERROR'):
            b._inbox_loop()
        self.assertEqual(self.journal.recent()[0]['state'], 'interrupted')
        self.assertIn('附件接收中断', b.reply.call_args[0][1])
        self.assertEqual(b._inbox_q.unfinished_tasks, 0)

    def test_abrupt_process_exit_preserves_recovery_boundary(self):
        script = '''
import os, sys
from task_journal import TaskJournal
j = TaskJournal(sys.argv[1])
j.add('owner', 'queued', 'task', '未开始', payload='恢复正文')
ident, _ = j.add('owner', 'running', 'task', '已经开始', payload='不可重放')
j.update(ident, 'running')
os._exit(9)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 9, result.stderr)
        restarted = TaskJournal(self.path)
        rows = {r['label']: r for r in restarted.recent()}
        self.assertEqual(restarted.claim('owner', rows['未开始']['id']), '恢复正文')
        self.assertEqual(rows['已经开始']['state'], 'uncertain')
        self.assertIsNone(restarted.claim('owner', rows['已经开始']['id']))

    def test_concurrent_restore_only_claims_once(self):
        ident, _ = self.journal.add('owner', 'concurrent', 'task', '恢复', payload='唯一任务')
        restarted = TaskJournal(self.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: restarted.claim('owner', ident), range(2)))
        self.assertEqual(results.count('唯一任务'), 1)
        self.assertEqual(results.count(None), 1)

    def test_execution_exception_remains_uncertain_after_real_reply_mapping(self):
        b = self.bridge(); msg = Message({'from_user_id': 'owner', 'message_id': 'partial'})
        b._enqueue((msg, '可能已修改文件', None), msg)
        item = b.queue.get_nowait()
        b.queue.get = Mock(side_effect=[item, StopIteration])
        b._worker = threading.current_thread()
        b.client.send_text = Mock()
        from wechat_bridge import Bridge
        b.reply = Bridge.reply.__get__(b)
        b._run_task = Mock(side_effect=RuntimeError('after partial work'))
        with self.assertLogs('bridge', level='ERROR'), self.assertRaises(StopIteration):
            b._loop_worker()
        self.assertEqual(self.journal.recent()[0]['state'], 'uncertain')
        self.assertIsNone(TaskJournal(self.path).claim('owner', msg._journal_id))


if __name__ == '__main__':
    unittest.main()
