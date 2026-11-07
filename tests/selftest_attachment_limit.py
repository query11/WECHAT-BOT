"""Attachment cap receipts through the real Bridge path, isolated transport."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ilink_client import ILinkError, Message
from task_journal import TaskJournal
from task_control import TransferCancelled
from wechat_bridge import Bridge
from tests import selftest_standby


class AttachmentLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        self.b = fixture.bridge
        self.b._mask = str
        self.b.enable_typing = False
        self.b.notice_after = 0
        self.b.send_file_enable = True
        self.b.send_file_limit = 3
        self.b.send_file_max_mb = 20
        self.b.send_roots = [str(self.root)]
        self.b._set_stage = Mock()
        self.b.runner = Mock()
        self.b.client = Mock()
        self.b.client.send_file.side_effect = lambda uid, path, **kw: (Path(path).name, 4)
        self.msg = Message({'from_user_id': 'owner', 'message_id': 'test'})
        self.msg._receipt_outcome = 'completed'
        self.paths = [self.root / ('file-%s.txt' % i) for i in range(4)]
        for path in self.paths:
            path.write_text('test', encoding='utf-8')

    def run_files(self, paths):
        self.b.runner.run.return_value = '全部请求已完成' + ''.join('\n[[发文件]]' + str(p) for p in paths)
        self.b._run_task(self.msg, 'send requested files')
        return '\n'.join(c.args[1] for c in self.b.reply.call_args_list)

    def test_overflow_not_silent_or_completed_and_journal_has_summary(self):
        self.b.journal = TaskJournal(self.root / 'tasks.sqlite')
        self.b.runner.run.return_value = '全部请求已完成' + ''.join('\n[[发文件]]' + str(p) for p in self.paths)
        self.assertIsNone(self.b._enqueue((self.msg, 'send', None), self.msg))
        item = self.b.queue.get_nowait()
        self.b.queue.get = Mock(side_effect=[item, StopIteration])
        with self.assertRaises(StopIteration):
            self.b._loop_worker()
        replies = '\n'.join(c.args[1] for c in self.b.reply.call_args_list)
        self.assertEqual(self.b.client.send_file.call_count, 3)
        self.assertNotIn('全部请求已完成', replies)
        self.assertIn('上限', replies)
        self.assertIn('file-3.txt', replies)
        self.assertEqual(self.b.journal.recent()[0]['state'], 'failed')
        with self.b.journal.connect() as db:
            summary, outputs = db.execute('SELECT summary,outputs FROM tasks').fetchone()
        self.assertIn('未发送', summary)
        self.assertNotIn('file-3.txt', outputs)

    def test_exact_limit_duplicates_and_empty_marks(self):
        replies = self.run_files(self.paths[:3] + [self.paths[0], ''])
        self.assertEqual(self.b.client.send_file.call_count, 3)
        self.assertIn('全部请求已完成', replies)
        self.assertEqual(self.msg._receipt_outcome, 'completed')

    def test_disabled_sends_nothing(self):
        self.b.send_file_enable = False
        replies = self.run_files(self.paths)
        self.b.client.send_file.assert_not_called()
        self.assertNotIn('全部请求已完成', replies)
        self.assertEqual(self.msg._receipt_outcome, 'failed')

    def test_overflow_and_unknown_delivery_keeps_uncertain(self):
        self.b.client.send_file.side_effect = [ILinkError('synthetic lost response'), ('file-1.txt', 4), ('file-2.txt', 4)]
        with self.assertLogs('bridge', level='ERROR'):
            replies = self.run_files(self.paths)
        self.assertNotIn('全部请求已完成', replies)
        self.assertIn('file-3.txt', replies)
        self.assertEqual(self.msg._receipt_outcome, 'uncertain')

    def test_local_failure_plus_overflow_is_reported(self):
        self.paths[0].unlink()
        replies = self.run_files(self.paths)
        self.assertEqual(self.b.client.send_file.call_count, 2)
        self.assertNotIn('全部请求已完成', replies)
        self.assertIn('file-3.txt', replies)
        self.assertEqual(self.msg._receipt_outcome, 'failed')

    def test_zero_and_one_attachment(self):
        for count in (0, 1):
            with self.subTest(count=count):
                self.b.client.send_file.reset_mock()
                self.b.reply.reset_mock()
                replies = self.run_files(self.paths[:count])
                self.assertEqual(self.b.client.send_file.call_count, count)
                self.assertIn('全部请求已完成', replies)

    def test_cancel_during_publication_keeps_uncertain_and_stops_later_files(self):
        self.b.journal = TaskJournal(self.root / 'tasks.sqlite')
        self.b.runner.run.return_value = '全部请求已完成' + ''.join('\n[[发文件]]' + str(p) for p in self.paths)
        self.b.client.send_file.side_effect = TransferCancelled(uncertain=True)
        self.assertIsNone(self.b._enqueue((self.msg, 'send', None), self.msg))
        item = self.b.queue.get_nowait()
        self.b.queue.get = Mock(side_effect=[item, StopIteration])
        with self.assertRaises(StopIteration):
            self.b._loop_worker()
        self.assertEqual(self.b.client.send_file.call_count, 1)
        self.assertEqual(self.b.journal.recent()[0]['state'], 'uncertain')
        replies = '\n'.join(c.args[1] for c in self.b.reply.call_args_list)
        self.assertNotIn('全部请求已完成', replies)
        self.assertIn('是否送达尚未确认', replies)


if __name__ == '__main__':
    unittest.main()
