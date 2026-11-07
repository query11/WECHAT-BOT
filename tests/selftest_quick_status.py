"""Read-only quick status routing, privacy and actual journal queries."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from tests import selftest_standby
from bridge_admin import is_quick_status
from ilink_client import Message
from task_journal import TaskJournal


class QuickStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = selftest_standby.StandbyTests(); self.fixture.setUp()
        self.b = self.fixture.bridge
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b.journal = TaskJournal(Path(self.temp.name)/'tasks.sqlite')

    def test_standby_query_does_not_wake_or_enqueue(self):
        self.fixture.send('现在怎么样？')
        text = self.b.reply.call_args.args[1]
        self.assertIn('状态：待机', text)
        self.assertIn('暂无执行中的任务', text)
        self.assertFalse(self.b._awake)
        self.assertTrue(self.b.queue.empty())
        self.b._dispatch_free.assert_not_called()

    def test_draining_shows_stage_time_and_keeps_mode(self):
        self.b._draining.add('owner')
        self.b._running = {'uid':'owner', 'label':'整理报告', 'stage':'正在上传', 'started_at':time.time()-125}
        self.fixture.send('还在处理吗')
        text = self.b.reply.call_args.args[1]
        self.assertIn('收尾后待机', text)
        self.assertIn('2 分 5 秒', text)
        self.assertIn('正在上传', text)
        self.assertIn('owner', self.b._draining)

    def test_other_user_text_and_history_are_private(self):
        self.b._running = {'uid':'other', 'label':'秘密任务', 'stage':'秘密目录', 'started_at':time.time()}
        other = Message({'from_user_id':'other'})
        own = Message({'from_user_id':'owner'})
        self.b.queue.put((other, '他人正文', None)); self.b.queue.put((own, '我的正文', None))
        ident, _ = self.b.journal.add('other', 'm', 'task', '他人结果')
        self.b.journal.update(ident, 'completed')
        text = self.b._quick_status('owner')
        self.assertIn('全机 2 项（你的 1 项）', text)
        self.assertIn('另一个用户的任务', text)
        for secret in ('秘密任务','秘密目录','他人正文','他人结果'):
            self.assertNotIn(secret, text)

    def test_recent_result_does_not_treat_running_as_complete(self):
        ident, _ = self.b.journal.add('owner', 'finished', 'task', '此前报告')
        self.b.journal.update(ident, 'failed')
        active, _ = self.b.journal.add('owner', 'active', 'task', '当前任务')
        self.b.journal.update(active, 'running')
        text = self.b._quick_status('owner')
        self.assertIn('失败 · 此前报告', text)
        self.assertNotIn('处理结束 · 当前任务', text)

    def test_compound_requests_are_not_intercepted(self):
        for value in ('现在怎么样，帮我打开微信', '请解释还在处理吗是什么意思', '/brief 帮我发文件', '进度怎么样\n再生成一份报告'):
            self.assertFalse(is_quick_status(value, '/cc'))
        for value in ('/brief', '/BRIEF', '/cc 现在怎么样？', '进度怎么样！'):
            self.assertTrue(is_quick_status(value, '/cc'))

    def test_unknown_user_gets_no_status(self):
        self.fixture.send('现在怎么样', uid='stranger')
        self.b.reply.assert_not_called()

    def test_journal_error_preserves_live_status(self):
        with patch.object(self.b.journal, 'latest_outcome', side_effect=OSError('disk unavailable')):
            with self.assertLogs('bridge', level='ERROR'):
                text = self.b._quick_status('owner')
        self.assertIn('状态：待机', text)
        self.assertIn('暂时无法读取', text)

    def test_recovery_hint_does_not_replay(self):
        ident, _ = self.b.journal.add('owner', 'queued', 'task', '旧任务', payload='原文')
        self.b.journal.update(ident, 'recoverable')
        self.fixture.send('/brief')
        self.assertIn('/tasks', self.b.reply.call_args.args[1])
        self.assertEqual(self.b.journal.recent()[0]['state'], 'recoverable')
        self.assertTrue(self.b.queue.empty())

    def test_attachment_message_is_not_consumed_as_status(self):
        self.fixture.send('现在怎么样', attachments=['图片'])
        self.assertNotIn('当前概览', self.b.reply.call_args.args[1])

    def test_credentials_are_masked_before_label_is_shortened(self):
        secret = 'synthetic-test-secret-' * 10
        self.b.cfg['bot_token'] = secret
        self.b._running = {'uid':'owner', 'label':secret, 'started_at':time.time()}
        text = self.b._quick_status('owner')
        self.assertNotIn('synthetic-test-secret', text)
        self.assertIn('***', text)


if __name__ == '__main__':
    unittest.main()
