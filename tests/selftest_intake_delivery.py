"""Fault and recovery regressions; no real WeChat, model, config or user data."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from bridge_runtime import Runtime
from claude_runner import ClaudeRunner
from deepseek_runner import DeepSeekRunner
from claude_api_runner import ClaudeAPIRunner
from ilink_client import ILinkClient, ILinkError, Message, split_text
from runner_result import RunnerResult
from tests import selftest_standby
from task_control import TaskQueue
from task_journal import TaskJournal, RESULT_RETENTION_SECONDS
from task_history import read_history_view
from wechat_bridge import Bridge


class IntakeDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.journal = TaskJournal(self.root / 'tasks.sqlite')
        self.b = self.bridge()

    def bridge(self):
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        b = fixture.bridge
        b.journal = self.journal
        b._mask = str
        b._awake.add('owner')
        b._dispatch_free.return_value = None
        b._dispatch_gated = Mock(return_value=None)
        b.require_prefix = False
        b.queue_max = 8
        b.enable_typing = False
        b.notice_after = 0
        b.send_file_enable = False
        b.send_file_limit = 3
        b._set_stage = Mock()
        b.client = Mock(account_id='bot')
        b.reply = Bridge.reply.__get__(b)
        return b

    def message(self, text='ordinary task', mid='probe', uid='owner'):
        return Message({'message_id': mid, 'from_user_id': uid, 'context_token': 'synthetic-context',
                        'item_list': [{'type': 1, 'text_item': {'text': text}}]})

    def poll(self, messages, cursor='next'):
        self.b.client.get_updates.return_value = (messages, cursor)
        return self.b.poll_once()

    def finished(self, text='long result'):
        msg = self.message()
        ident, _ = self.journal.add('owner', msg.message_id, 'task', 'test result')
        msg._journal_id = ident
        msg._receipt_outcome = msg._execution_state = 'completed'
        self.journal.update(ident, 'running')
        self.b._worker = threading.current_thread()
        return msg, ident

    def result_row(self, ident):
        with self.journal.connect(read_only=True) as db:
            row = db.execute('SELECT state,execution_state,delivery_state,resolution,resolved_at,payload FROM tasks WHERE id=?', (ident,)).fetchone()
        return dict(zip(('state','execution','delivery','resolution','resolved_at','payload'), row))

    def test_slow_acceptance_does_not_block_desktop_cancel_or_heartbeat(self):
        self.b.cfg['task_progress'] = True
        self.b.queue = TaskQueue()
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.b.client.send_text.side_effect = lambda *a, **k: (entered.set(), release.wait(4))
        self.b._stop_running = Mock(return_value='stop requested')
        runtime = Runtime.__new__(Runtime)
        runtime.bridge, runtime.root, runtime.event = self.b, self.root, Mock()
        runtime.snapshot = Mock(return_value={'heartbeat': 'new'})
        msg = self.message()
        producer = threading.Thread(target=lambda: self.b._enqueue((msg, 'task', None), msg))
        producer.start()
        try:
            self.assertTrue(entered.wait(2))
            command = self.root / 'command-cancel.json'
            command.write_text(json.dumps({'action':'cancel','pid':os.getpid(),'at':time.time()}))
            errors = []
            def tick():
                try: runtime.tick()
                except Exception as exc: errors.append(exc)
                finally: done.set()
            thread = threading.Thread(target=tick)
            thread.start()
            self.assertTrue(done.wait(1), 'network request still holds the desktop lock')
            thread.join(1)
            self.assertEqual(errors, [])
            self.b._stop_running.assert_called_once()
            self.assertTrue((self.root / 'status.json').exists())
            self.assertFalse(msg._dispatch_ready.is_set())
        finally:
            release.set(); producer.join(3)
        self.assertFalse(producer.is_alive())
        self.assertTrue(msg._dispatch_ready.is_set())

    def test_worker_waiting_for_acceptance_can_cancel_without_model_execution(self):
        self.b.cfg['task_progress'] = True
        self.b.queue = TaskQueue()
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def send(uid, text, context):
            if text.startswith('⏳ 任务已接收'):
                entered.set(); release.wait(4)
            elif '已叫停' in text:
                cancelled.set()
        self.b.client.send_text.side_effect = send
        self.b._run_task = Mock()
        self.b.runner = Mock()
        self.b._finish_draining = Mock(side_effect=SystemExit)
        msg = self.message()
        producer = threading.Thread(target=lambda: self.b._enqueue((msg, 'task', None), msg))
        producer.start()
        self.assertTrue(entered.wait(2))
        worker = self.b._worker = threading.Thread(target=self.b._loop_worker)
        worker.start()
        try:
            msg._task_control.cancel()
            self.assertTrue(cancelled.wait(2))
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.b._run_task.assert_not_called()
            self.assertEqual(self.b.queue.unfinished_tasks, 0)
            self.assertEqual(self.result_row(msg._journal_id)['state'], 'cancelled')
        finally:
            release.set(); producer.join(3); worker.join(3)

    def test_history_read_error_does_not_prevent_wechat_stop(self):
        self.b._try_admin = Bridge._try_admin.__get__(self.b)
        self.b._dispatch_free = Bridge._dispatch_free.__get__(self.b)
        self.b._stop_running = Mock(return_value='stop requested')
        with patch.object(self.journal, 'unresolved', side_effect=sqlite3.DatabaseError('read fault')) as read:
            self.b.handle(self.message('/stop'))
        read.assert_not_called()
        self.b._stop_running.assert_called_once_with('owner', all_of_queue=False)

    def test_history_database_lock_is_bounded_for_tasks_and_brief(self):
        connection = sqlite3.connect(self.journal.path)
        try:
            connection.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            self.b.handle(self.message('/tasks', 'tasks'))
            with self.assertLogs('bridge', level='ERROR'):
                text = self.b._quick_status('owner')
            self.assertLess(time.monotonic() - started, .8)
            self.assertIn('暂时无法读取', text)
            self.assertIn('暂时无法读取', self.b.client.send_text.call_args.args[1])
        finally:
            connection.rollback(); connection.close()
        self.b.handle(self.message('/tasks', 'after-unlock'))
        self.assertIn('暂无任务', self.b.client.send_text.call_args.args[1])

    def test_failed_initial_intake_write_and_reply_keep_cursor_then_accept_once(self):
        msg = self.message()
        self.b.client.send_text.side_effect = ILinkError('reply unavailable')
        with patch.object(self.journal, 'begin_message', side_effect=sqlite3.OperationalError('disk unavailable')):
            with self.assertLogs('bridge', level='ERROR'):
                self.poll([msg])
        self.assertTrue(self.b._receiving_blocked)
        self.assertTrue(self.b.queue.empty())
        self.b.client.commit_cursor.assert_not_called()
        self.b.client.send_text.side_effect = None
        self.poll([msg]); self.poll([msg])
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertFalse(self.b._receiving_blocked)
        self.b.client.commit_cursor.assert_called_with('next')

    def test_failed_task_write_can_commit_only_after_failure_receipt_is_saved(self):
        self.b.client.send_text.side_effect = ILinkError('reply unavailable')
        with patch.object(self.journal, 'add', side_effect=sqlite3.OperationalError('task write failed')):
            with self.assertLogs('bridge', level='ERROR'):
                self.poll([self.message()])
        self.assertEqual(self.journal.recent('owner')[0]['state'], 'uncertain')
        self.assertTrue(self.b.queue.empty())
        self.b.client.commit_cursor.assert_called_once_with('next')
        self.poll([self.message()])
        self.assertTrue(self.b.queue.empty())

    def test_failure_receipt_write_recovery_never_replays_partial_dispatch(self):
        action = Mock(side_effect=RuntimeError('failed after external action'))
        self.b._dispatch_gated = action
        self.b.client.send_text.side_effect = ILinkError('reply unavailable')
        msg = self.message()
        with patch.object(self.journal, 'fail_message', side_effect=sqlite3.OperationalError('receipt unavailable')):
            with self.assertLogs('bridge', level='ERROR'):
                self.poll([msg])
            self.b.client.commit_cursor.assert_not_called()
        self.poll([msg])
        self.assertEqual(action.call_count, 1)
        self.assertEqual(self.journal.recent('owner')[0]['state'], 'uncertain')
        self.b.client.commit_cursor.assert_called_once_with('next')

    def test_crash_after_dispatch_guard_keeps_uncertainty_across_restart(self):
        script = """import os,sys\nfrom task_journal import TaskJournal\nfrom pathlib import Path\nj=TaskJournal(sys.argv[1]);j.begin_message('owner','crash','test external action')\nPath(sys.argv[2]).write_text('changed once')\nos._exit(0)\n"""
        effect = self.root / 'effect.txt'
        done = subprocess.run([sys.executable, '-c', script, self.journal.path, str(effect)], cwd=Path(__file__).parent.parent, capture_output=True, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.journal = TaskJournal(self.journal.path)
        self.b = self.bridge()
        self.b.handle(self.message('same action', 'crash'))
        self.b._dispatch_gated.assert_not_called()
        self.assertEqual(effect.read_text(), 'changed once')
        self.assertEqual(self.journal.recent('owner')[0]['state'], 'uncertain')

    def test_finish_receipt_failure_does_not_duplicate_an_already_queued_task(self):
        msg = self.message()
        with patch.object(self.journal, 'finish_message', side_effect=sqlite3.OperationalError('disk unavailable')):
            with self.assertLogs('bridge', level='ERROR'):
                self.poll([msg])
        self.poll([msg])
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertEqual(self.journal.recent()[0]['state'], 'queued')

    def test_restart_closes_stale_execution_and_delivery_states_without_losing_receipts(self):
        queued, _ = self.journal.add('owner', 'queued', 'task', 'unstarted', payload='original prompt')
        running, _ = self.journal.add('owner', 'running', 'task', 'unknown result')
        inbox, _ = self.journal.add('owner', 'inbox', 'inbox', 'interrupted download')
        completed, _ = self.journal.add('owner', 'sending', 'task', 'known model result')
        self.journal.update(running, 'running')
        self.journal.update(inbox, 'running')
        self.journal.update(completed, 'running')
        reply = self.journal.prepare_reply(completed, 'retained full result', 'completed', 8)
        self.journal.record_delivery(reply, 1, 3, 'pending')

        self.journal = TaskJournal(self.journal.path)
        self.assertEqual(self.result_row(queued)['execution'], 'recoverable')
        self.assertEqual(self.result_row(running)['execution'], 'uncertain')
        self.assertEqual(self.result_row(inbox)['execution'], 'interrupted')
        self.assertEqual(self.result_row(completed)['state'], 'uncertain')
        self.assertEqual(self.result_row(completed)['execution'], 'completed')
        self.assertEqual(self.result_row(completed)['delivery'], 'unconfirmed')
        cached = self.journal.replies('owner', completed, only_pending=True)[0]
        self.assertEqual((cached['body'], cached['accepted_chunks'], cached['chunk_size']),
                         ('retained full result', 1, 8))
        self.assertEqual(cached['state'], 'unconfirmed')
        self.assertEqual(self.journal.claim('owner', queued), 'original prompt')
        self.assertEqual(self.result_row(queued)['execution'], 'queued')

    def test_no_journal_failed_notice_keeps_cursor_and_does_not_repeat_action(self):
        self.b.journal = None
        self.b._dispatch_gated.side_effect = RuntimeError('partial action')
        self.b.client.send_text.side_effect = ILinkError('no reply')
        with self.assertLogs('bridge', level='ERROR'):
            self.poll([self.message()])
        with self.assertLogs('bridge', level='ERROR'):
            self.poll([self.message()])
        self.assertEqual(self.b._dispatch_gated.call_count, 1)
        self.b.client.commit_cursor.assert_not_called()
        self.b.client.send_text.side_effect = None
        self.poll([self.message()])
        self.b.client.commit_cursor.assert_called_once()

    def test_completed_model_result_with_error_or_stop_emoji_stays_completed(self):
        for lead in ('❌ 是叉号', '⏹ 是停止按钮'):
            with self.subTest(lead=lead):
                text, _ = ClaudeRunner._parse(json.dumps({'type':'result','result':lead,'is_error':False}))
                self.assertIsInstance(text, RunnerResult)
                msg = self.message(mid=lead)
                msg._receipt_outcome = 'completed'
                self.b.runner = Mock()
                self.b.runner.run.return_value = text
                self.b._worker = threading.current_thread()
                self.b._run_task(msg, 'explain symbol')
                self.assertEqual(msg._receipt_outcome, 'completed')
                self.assertEqual(msg._execution_state, 'completed')

    def test_all_three_backends_keep_structured_status_through_redaction(self):
        for kind in (ClaudeRunner, DeepSeekRunner, ClaudeAPIRunner):
            with self.subTest(kind=kind.__name__):
                runner = kind.__new__(kind)
                runner._api_key = 'synthetic-private-key'
                runner.backend_name = 'test backend'
                result, _ = runner._read_result(json.dumps({'type':'result','result':'❌ symbol synthetic-private-key','is_error':False}))
                self.assertEqual(result.state, 'completed')
                if kind is not ClaudeRunner:
                    self.assertNotIn('synthetic-private-key', result)
                failed, _ = runner._read_result(json.dumps({'type':'result','result':'success prose','permission_denials':[{}]}))
                self.assertEqual(failed.state, 'failed')

    def test_full_result_and_execution_status_survive_failed_text_delivery(self):
        msg, ident = self.finished()
        body = '中文 full result ' * 500
        self.b.client.send_text.side_effect = ILinkError('reply lost')
        with self.assertLogs('bridge', level='ERROR'):
            self.b.reply(msg, body)
        self.journal.update(ident, msg._receipt_outcome, summary=msg._result_summary, execution_state=msg._execution_state)
        self.assertEqual(self.result_row(ident)['execution'], 'completed')
        self.assertEqual(self.result_row(ident)['delivery'], 'unconfirmed')
        self.assertEqual(self.journal.replies('owner', ident)[0]['body'], body)
        self.assertEqual(len(msg._result_summary), 1000)

    def test_progress_notice_failure_is_not_execution_failure_or_cached_result(self):
        msg, ident = self.finished()
        self.b.client.send_text.side_effect = ILinkError('progress lost')
        with self.assertLogs('bridge', level='ERROR'):
            self.b.reply(msg, '⏳ progress', result=False)
        self.assertEqual(msg._receipt_outcome, 'completed')
        with self.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_replies').fetchone()[0], 0)

    def test_partial_reply_resend_skips_accepted_chunks_and_preserves_original_size(self):
        msg, ident = self.finished()
        client = ILinkClient('https://example.invalid', '', '', state_dir=str(self.root), max_reply_chars=120)
        self.b.client = client
        body = 'chunked text ' * 50
        sent = []
        def first_send(uid, text, context):
            sent.append(text)
            if len(sent) == 2: raise ILinkError('second chunk response lost')
        client.send_chunk = first_send
        with patch('ilink_client.time.sleep'), self.assertLogs('bridge', level='ERROR'):
            self.b.reply(msg, body)
        self.journal.update(ident, 'uncertain', execution_state='completed')
        saved = self.journal.replies('owner', ident, only_pending=True)[0]
        self.assertEqual(saved['accepted_chunks'], 1)
        client.max_reply_chars = 500  # Changing settings must not shift cached chunks.
        resent = []
        client.send_chunk = lambda uid, text, context: resent.append(text)
        self.b._worker = None
        self.b.runner = Mock()
        self.b._send_files = Mock()
        with patch('ilink_client.time.sleep'):
            self.b.handle(self.message('/resend ' + ident, 'resend'))
        data = [text for text in resent if text.startswith('（')]
        self.assertEqual(len(data), len(split_text(body, 120)) - 1)
        self.assertTrue(data[0].startswith('（2/'))
        self.assertEqual(data[0], sent[1])
        self.assertEqual(self.result_row(ident)['delivery'], 'accepted')
        self.assertEqual(self.result_row(ident)['execution'], 'completed')
        self.b.runner.run.assert_not_called()
        self.b._send_files.assert_not_called()

    def test_only_owner_can_view_or_resend_and_duplicate_request_does_not_execute(self):
        msg, ident = self.finished()
        self.b.reply(msg, 'saved plain result')
        self.journal.update(ident, 'completed')
        self.b._worker = None
        self.b._history_command(self.message('/result ' + ident, uid='other'), '/result ' + ident)
        self.assertNotIn('saved plain result', self.b.client.send_text.call_args.args[1])
        self.b.client.send_text.reset_mock()
        self.b.handle(self.message('/resend ' + ident, 'resend-already-accepted'))
        self.assertNotIn('saved plain result', self.b.client.send_text.call_args.args[1])

    def test_redacted_result_uses_identical_text_and_chunk_boundaries_on_resend(self):
        msg, ident = self.finished()
        client = ILinkClient('https://example.invalid', '', '', state_dir=str(self.root), max_reply_chars=120)
        self.b.client = client
        self.b._mask = lambda text: str(text).replace('synthetic-secret-value', '***')
        body = 'private synthetic-secret-value result ' * 60
        sent = []
        def first_send(uid, text, context):
            sent.append(text)
            if len(sent) == 2:
                raise ILinkError('second response lost')
        client.send_chunk = first_send
        with patch('ilink_client.time.sleep'), self.assertLogs('bridge', level='ERROR'):
            self.b.reply(msg, body)
        self.journal.update(ident, 'uncertain', execution_state='completed')
        entry = self.journal.replies('owner', ident, only_pending=True)[0]
        self.assertEqual(entry['body'], self.b._mask(body))
        self.assertNotIn('synthetic-secret-value', ''.join(sent))
        resent = []
        client.send_chunk = lambda uid, text, context: resent.append(text)
        self.b._worker = None
        with patch('ilink_client.time.sleep'):
            self.b.reply(self.message('/resend ' + ident, 'redacted-resend'), entry['body'], result=False, retained=entry)
        self.assertEqual(resent[0], sent[1])
        self.assertEqual(self.result_row(ident)['delivery'], 'accepted')

    def test_delivery_receipt_write_failure_keeps_text_available_for_manual_check(self):
        msg, ident = self.finished()
        with patch.object(self.journal, 'record_delivery', side_effect=sqlite3.OperationalError('disk busy')):
            with self.assertLogs('bridge', level='WARNING'):
                self.assertTrue(self.b.reply(msg, 'accepted but receipt not persisted'))
        self.journal.update(ident, 'completed', execution_state='completed')
        self.journal = TaskJournal(self.journal.path)
        entry = self.journal.replies('owner', ident, only_pending=True)[0]
        self.assertEqual(entry['accepted_chunks'], 0)
        self.assertEqual(entry['state'], 'unconfirmed')
        self.assertEqual(entry['body'], 'accepted but receipt not persisted')
        self.assertEqual(self.result_row(ident)['execution'], 'completed')

    def test_expired_result_cache_is_unavailable_and_pruned_on_next_write(self):
        msg, ident = self.finished()
        self.b.reply(msg, 'old result')
        self.journal.update(ident, 'completed')
        with self.journal.connect() as db:
            db.execute('UPDATE task_replies SET created=?', (time.time() - RESULT_RETENTION_SECONDS - 1,))
        self.assertEqual(self.journal.replies('owner', ident), [])
        self.b.reply(msg, 'new result')
        with self.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT body FROM task_replies').fetchall(), [('new result',)])

    def test_cache_write_failure_does_not_stop_result_delivery(self):
        msg, _ = self.finished()
        with patch.object(self.journal, 'prepare_reply', side_effect=sqlite3.OperationalError('full disk')):
            with self.assertLogs('bridge', level='WARNING'):
                self.assertTrue(self.b.reply(msg, 'completed result'))
        self.assertEqual(msg._receipt_outcome, 'completed')
        self.assertIn('缓存未能保存', self.b.client.send_text.call_args.args[1])

    def test_manual_resolution_preserves_original_state_and_stops_reminders(self):
        ident, _ = self.journal.add('owner', 'uncertain', 'task', 'check manually')
        self.journal.update(ident, 'uncertain', execution_state='completed')
        self.assertFalse(self.journal.resolve('other', ident, 'checked'))
        self.assertTrue(self.journal.resolve('owner', ident, 'checked'))
        self.assertTrue(self.journal.resolve('owner', ident, 'checked'))
        self.assertFalse(self.journal.resolve('owner', ident, 'archived'))
        row = self.result_row(ident)
        self.assertEqual(row['state'], 'uncertain')
        self.assertEqual(row['execution'], 'completed')
        self.assertEqual(row['resolution'], 'checked')
        self.assertGreater(row['resolved_at'], 0)
        self.assertFalse(self.journal.unresolved('owner'))

    def test_resolved_recovery_description_does_not_offer_resume(self):
        ident, _ = self.journal.add('owner', 'abandon-description', 'task', 'not started', payload='task')
        self.journal.update(ident, 'recoverable')
        self.journal.resolve('owner', ident, 'abandoned')
        self.assertNotIn('/resume ' + ident, self.journal.describe('owner'))
        self.assertIn('已放弃恢复', self.journal.describe('owner'))

    def test_abandoned_or_archived_recovery_cannot_be_claimed(self):
        for action in ('abandoned', 'archived'):
            ident, _ = self.journal.add('owner', action, 'task', 'never started', payload='original task')
            self.journal.update(ident, 'recoverable')
            self.assertTrue(self.journal.resolve('owner', ident, action))
            self.assertIsNone(self.journal.claim('owner', ident))
            self.assertIsNone(self.result_row(ident)['payload'])
            self.assertEqual(self.result_row(ident)['state'], 'recoverable')

    def test_active_task_cannot_be_resolved(self):
        ident, _ = self.journal.add('owner', 'active', 'task', 'running')
        self.journal.update(ident, 'running')
        for action in ('checked','abandoned','archived'):
            self.assertFalse(self.journal.resolve('owner', ident, action))

    def test_history_view_exposes_full_result_delivery_and_resolution_read_only(self):
        msg, ident = self.finished()
        self.b.reply(msg, 'full result ' * 300)
        self.journal.update(ident, 'uncertain', execution_state='completed')
        self.journal.resolve('owner', ident, 'checked')
        (self.root / 'config.json').write_text(json.dumps({'task_journal_file':self.journal.path}))
        before = Path(self.journal.path).read_bytes()
        view = read_history_view(self.root)
        row = view['rows'][0]
        self.assertEqual(row['execution_state'], 'completed')
        self.assertEqual(row['delivery_state'], 'accepted')
        self.assertEqual(row['resolution'], 'checked')
        self.assertEqual(row['result_text'], 'full result ' * 300)
        self.assertEqual(Path(self.journal.path).read_bytes(), before)

    def test_prefixed_result_command_is_handled_without_enqueuing_a_model_task(self):
        msg, ident = self.finished()
        self.b.reply(msg, 'saved result')
        self.journal.update(ident, 'completed')
        self.b._worker = None
        self.b.handle(self.message('/cc /result ' + ident, 'prefixed-result'))
        self.assertIn('saved result', self.b.client.send_text.call_args.args[1])
        self.assertTrue(self.b.queue.empty())

    def test_history_advertises_actions_only_for_a_capable_live_backend(self):
        state_dir = self.root / '.runtime'; state_dir.mkdir()
        status = dict(pid=os.getpid(), heartbeat=time.time(), state='standby',
                      journal_source={'enabled':True,'path':self.journal.path})
        (state_dir / 'status.json').write_text(json.dumps(status))
        self.assertEqual(read_history_view(self.root)['source']['actions'], [])
        status['history_actions'] = ['result','resend','resolve']
        (state_dir / 'status.json').write_text(json.dumps(status))
        self.assertEqual(read_history_view(self.root)['source']['actions'], ['result','resend','resolve'])


if __name__ == '__main__':
    unittest.main()
