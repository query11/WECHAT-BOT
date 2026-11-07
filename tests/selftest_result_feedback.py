"""Result evidence tests: temporary files, fake transport, no real desktop actions."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from claude_runner import ClaudeRunner, TaskCancelled
from ilink_client import ILinkError, Message
from task_journal import TaskJournal
from wechat_bridge import Bridge
from tests import selftest_standby


class ResultFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        self.b = fixture.bridge
        self.b.send_roots = [str(self.root)]
        self.b.send_file_limit = 3
        self.b.send_file_max_mb = 20
        self.b._set_stage = Mock()
        self.b.client = Mock()
        self.msg = Message({'from_user_id': 'owner', 'message_id': 'task', 'context_token': 'fresh'})
        self.msg._receipt_outcome = 'completed'
        self.path = self.root / 'report.txt'
        self.path.write_text('verified file bytes', encoding='utf-8')

    def replies(self):
        return '\n'.join(c.args[1] for c in self.b.reply.call_args_list)

    def test_file_receipt_only_after_transport_acceptance(self):
        def send(*args, **kwargs):
            self.assertNotIn('接口已接受', self.replies())
            self.assertEqual(kwargs['context_token'], 'fresh')
            return self.path.name, self.path.stat().st_size
        self.b.client.send_file.side_effect = send
        counts = self.b._send_files(self.msg, [str(self.path)])
        self.assertEqual(counts, {'accepted': 1, 'failed': 0, 'unconfirmed': 0, 'requested': 1, 'not_sent': 0})
        self.assertIn('接口已接受', self.replies())
        self.assertIn(str(self.path), self.replies())
        self.assertIn('19 字节', self.replies())
        self.assertEqual(self.msg._receipt_outcome, 'completed')

    def test_transport_failure_is_unconfirmed_not_delivered(self):
        self.b.client.send_file.side_effect = ILinkError('response lost')
        with self.assertLogs('bridge', level='ERROR'):
            counts = self.b._send_files(self.msg, [str(self.path)])
        self.assertEqual(counts['unconfirmed'], 1)
        self.assertIn('未获得发送成功确认', self.replies())
        self.assertNotIn('接口已接受', self.replies())
        self.assertEqual(self.msg._receipt_outcome, 'uncertain')

    def test_missing_file_is_not_uploaded(self):
        self.path.unlink()
        counts = self.b._send_files(self.msg, [str(self.path)])
        self.assertEqual(counts['failed'], 1)
        self.b.client.send_file.assert_not_called()
        self.assertEqual(self.msg._receipt_outcome, 'failed')

    def test_mixed_files_keep_uncertain_outcome(self):
        self.b.client.send_file.side_effect = [ILinkError('lost response'), ('report.txt', 19)]
        with self.assertLogs('bridge', level='ERROR'):
            counts = self.b._send_files(self.msg, [str(self.path), str(self.path), str(self.root/'missing.txt')])
        self.assertEqual(counts, {'accepted': 1, 'failed': 1, 'unconfirmed': 1, 'requested': 3, 'not_sent': 0})
        self.assertEqual(self.msg._receipt_outcome, 'uncertain')

    def test_file_receipt_send_failure_remains_uncertain(self):
        self.b.reply = Bridge.reply.__get__(self.b)
        self.b._worker = threading.current_thread()
        self.b.client.send_file.return_value = ('report.txt', 19)
        self.b.client.send_text.side_effect = ILinkError('reply lost')
        with self.assertLogs('bridge', level='ERROR'):
            counts = self.b._send_files(self.msg, [str(self.path)])
        self.assertEqual(counts['accepted'], 1)
        self.assertEqual(self.msg._receipt_outcome, 'uncertain')

    def test_cli_nonzero_with_text_is_failure(self):
        runner = ClaudeRunner(workdir=str(self.root), session_file=str(self.root/'sessions.json'))
        proc = Mock()
        proc.communicate.return_value = (json.dumps({'result': 'partial output'}).encode(), b'')
        proc.returncode = 7
        with patch('claude_runner.subprocess.Popen', return_value=proc):
            out = runner.run('owner', 'request')
        self.assertTrue(out.startswith('❌'))
        self.assertIn('返回码 7', out)
        self.assertIn('partial output', out)

    def test_cli_error_flag_is_failure_even_with_success_exit(self):
        text, session = ClaudeRunner._parse(json.dumps({'is_error': True, 'result': 'budget exhausted', 'session_id': 's'}))
        self.assertTrue(text.startswith('❌'))
        self.assertIn('budget exhausted', text)
        self.assertEqual(session, 's')

    def test_error_output_cannot_send_partial_file_marker(self):
        self.b.enable_typing = False
        self.b.notice_after = 0
        self.b.runner = Mock()
        self.b.runner.run.return_value = '❌ 执行失败\n[[发文件]]' + str(self.path)
        self.b._send_files = Mock()
        self.b._run_task(self.msg, 'request')
        self.b._send_files.assert_not_called()
        self.assertEqual(self.msg._receipt_outcome, 'failed')
        self.assertNotIn('[[发文件]]', self.replies())

    def test_stop_request_does_not_claim_execution_finished(self):
        self.b.runner = Mock()
        self.b.runner.cancel.return_value = True
        self.b._running = {'uid': 'owner', 'label': 'task', 'started_at': time.time()}
        text = self.b._stop_running('owner')
        self.assertIn('已请求停止', text)
        self.assertNotIn('已叫停', text)
        self.assertIn('退出后', text)

    def test_cancelled_screen_task_is_recorded_and_worker_survives(self):
        self.b.journal = TaskJournal(self.root/'tasks.sqlite')
        self.b._mask = str
        self.b._enqueue((self.msg, '', {'look': True}), self.msg)
        item = self.b.queue.get_nowait()
        self.b.queue.get = Mock(side_effect=[item, StopIteration])
        self.b._run_look = Mock(side_effect=TaskCancelled())
        self.b.reply.side_effect = RuntimeError('reply unavailable')
        with self.assertLogs('bridge', level='ERROR'), self.assertRaises(StopIteration):
            self.b._loop_worker()
        self.assertEqual(self.b.journal.recent()[0]['state'], 'cancelled')
        self.assertEqual(self.b.queue.unfinished_tasks, 0)

    def prepare_model_file_reply(self, text='✅ 已发送'):
        self.b.enable_typing = False
        self.b.notice_after = 0
        self.b.send_file_enable = True
        self.b.runner = Mock()
        self.b.runner.run.return_value = text + '\n[[发文件]]' + str(self.path)

    def test_model_sent_claim_waits_for_real_upload(self):
        self.prepare_model_file_reply()
        def send(*args, **kwargs):
            self.assertNotIn('已发送', self.replies())
            return self.path.name, 19
        self.b.client.send_file.side_effect = send
        self.b._run_task(self.msg, 'send this file')
        replies = [c.args[1] for c in self.b.reply.call_args_list]
        self.assertTrue(replies[0].startswith('📎 微信接口已接受'))
        self.assertEqual(replies[-1], '✅ 已发送')

    def test_model_sent_claim_hidden_when_upload_unconfirmed(self):
        self.prepare_model_file_reply()
        self.b.client.send_file.side_effect = ILinkError('connection lost')
        with self.assertLogs('bridge', level='ERROR'):
            self.b._run_task(self.msg, 'send this file')
        self.assertNotIn('已发送', self.replies())
        self.assertIn('未获得发送成功确认', self.replies())
        self.assertEqual(self.msg._receipt_outcome, 'uncertain')

    def test_disabled_upload_never_repeats_model_success(self):
        self.prepare_model_file_reply()
        self.b.send_file_enable = False
        self.b._run_task(self.msg, 'send this file')
        self.assertNotIn('已发送', self.replies())
        self.assertIn('没有上传', self.replies())
        self.b.client.send_file.assert_not_called()

    def test_file_marker_only_uses_actual_receipt(self):
        self.prepare_model_file_reply('')
        self.b.client.send_file.return_value = (self.path.name, 19)
        self.b._run_task(self.msg, 'send this file')
        self.assertIn('接口已接受', self.replies())
        self.assertNotIn('正在发送文件', self.replies())


if __name__ == '__main__':
    unittest.main()
