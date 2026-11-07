import queue
import threading
import time
import unittest
from unittest.mock import Mock
from ilink_client import Message
from runtime_messages import explain_error
from tests import selftest_runtime
from tests import selftest_standby


class ExperienceTests(unittest.TestCase):
    def test_fault_types_are_plain_language(self):
        for raw, expected in (
            ('[SSL: UNEXPECTED_EOF_WHILE_READING] private detail', '加密连接'),
            ('getaddrinfo failed', '解析'), ('HTTP 401 secret', '登录'),
            ('timed out', '响应'), ('CERTIFICATE_VERIFY_FAILED', '证书')):
            title, advice = explain_error(raw)
            self.assertIn(expected, title)
            self.assertNotIn('secret', title + advice)
            self.assertNotIn('private detail', title + advice)

    def test_persistent_fault_then_recovery(self):
        fixture = selftest_runtime.RuntimeTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rt = fixture.rt
        rt.failed('SSL EOF')
        count = len(rt.events)
        rt.failed('SSL EOF')
        self.assertEqual(len(rt.events), count)
        rt._outage_since_mono = time.monotonic() - 181
        self.assertEqual(rt.snapshot()['state'], 'error')
        rt.connected()
        self.assertEqual(rt.snapshot()['state'], 'standby')
        self.assertEqual(rt.snapshot()['error'], '')

    def test_receipt_precedes_enqueue_and_full_queue_has_no_receipt(self):
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        b = fixture.bridge; b.cfg['task_progress'] = True
        b.queue = queue.Queue(maxsize=1); b.queue_max = 1
        msg = Message({'from_user_id': 'owner', 'message_id': 'one'})
        b.reply = Mock(side_effect=lambda *args: self.assertTrue(b.queue.empty()))
        self.assertIsNone(b._enqueue((msg, 'request', None), msg))
        b.reply.assert_called_once()
        b.reply.reset_mock()
        self.assertIn('这条我没接', b._enqueue((msg, 'again', None), msg))
        b.reply.assert_not_called()

    def test_stage_and_elapsed_are_observable_without_percentages(self):
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        b = fixture.bridge; b.cfg['task_progress'] = True
        b._worker = threading.current_thread(); b._running = {'label': 'task'}
        msg = Message({'from_user_id': 'owner'})
        b._set_stage(msg, '正在加密并上传第 1 个文件')
        msg._progress_started = time.monotonic() - 62
        text = b._progress_notice(msg)
        self.assertIn('62 秒', text)
        self.assertIn('上传', b._running['stage'])
        self.assertNotIn('%', text)


if __name__ == '__main__':
    unittest.main()
