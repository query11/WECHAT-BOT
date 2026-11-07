"""Offline standby tests: no real desktop or WeChat calls."""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from wechat_bridge import Bridge


class StandbyTests(unittest.TestCase):
    def setUp(self):
        b = self.bridge = Bridge.__new__(Bridge)
        b._desktop_lock = threading.RLock()
        b._desktop_stop = threading.Event()
        b.standby_enabled = True
        b._awake = set()
        b._draining = set()
        b.journal = None
        b.cfg = {}
        b._worker = None
        b._recovery_notified = set()
        b.allowed = {"owner"}
        b.client = SimpleNamespace(account_id="bot")
        b._is_duplicate = Mock(return_value=False)
        b._peek = Mock(return_value="redacted")
        b._msg_count = 0
        b._last_ctx = {}
        b._notes = {}
        b.prefix = "/cc"
        b.chat_mode_enable = True
        b._chat = {}
        b._chat_expired = set()
        b._pending = {}
        b._pending_meeting = {}
        b._await_code = {}
        b._inbox_pending = {}
        b.queue = queue.Queue()
        b._inbox_q = queue.Queue()
        b._running = None
        b.reply = Mock()
        b._inbox_start = Mock()
        b._dispatch_free = Mock(return_value="handled")
        b._try_admin = Mock(return_value="status")

    def send(self, text, uid="owner", attachments=None):
        self.bridge.handle(SimpleNamespace(message_type=1, from_user_id=uid,
            message_id="m", text=text, context_token="test", attachments=attachments or []))

    def test_standby_blocks_business_entries(self):
        for text in ("/cc 关机", "/see", "看看画面", "确认", "删掉", "/reload", ""):
            self.send(text, attachments=["图片"])
        self.bridge._inbox_start.assert_not_called()
        self.bridge._dispatch_free.assert_not_called()
        self.assertFalse(self.bridge._awake)

    def test_wake_work_and_standby(self):
        self.send("启动助手")
        self.assertIn("owner", self.bridge._chat)
        self.send("test task")
        self.bridge._dispatch_free.assert_called_once()
        self.bridge._pending["owner"] = {"click": True}
        self.send("停止助手")
        self.assertFalse(self.bridge._awake)
        self.assertFalse(self.bridge._pending)
        self.assertFalse(self.bridge._chat)

    def test_dequeued_unfinished_job_blocks_standby(self):
        self.send("启动助手")
        self.bridge.queue.put("job")
        self.bridge.queue.get()
        self.send("停止助手")
        self.assertNotIn("owner", self.bridge._awake)
        self.assertIn("owner", self.bridge._draining)

    def test_downloading_blocks_standby(self):
        self.send("启动助手")
        self.bridge._inbox_q.put("download")
        self.send("停止助手")
        self.assertIn("owner", self.bridge._draining)

    def test_unknown_sender_cannot_wake(self):
        self.send("启动助手", uid="stranger")
        self.assertFalse(self.bridge._awake)
        self.bridge.reply.assert_not_called()

    def test_status_does_not_wake(self):
        self.send("/status")
        self.assertFalse(self.bridge._awake)
        self.assertIn("待机中", self.bridge.reply.call_args.args[1])

    def test_chat_aliases_wake_and_sleep(self):
        for word in ("开始对话", "开启会话", "进入聊天模式", "请打开对话谢谢", "/on", "/chat"):
            self.send(word)
            self.assertIn("owner", self.bridge._awake, word)
            self.assertIn("owner", self.bridge._chat, word)
            self.send("结束对话")
            self.assertFalse(self.bridge._awake)
            self.assertFalse(self.bridge._chat)
        self.send("开始对话")
        self.bridge.queue.put("busy")
        self.send("结束对话")
        self.assertNotIn("owner", self.bridge._awake)
        self.assertIn("owner", self.bridge._draining)

    def test_exact_wake_only(self):
        self.send("启动助手然后关机")
        self.assertFalse(self.bridge._awake)
        self.send("/cc 启动助手")
        self.assertIn("owner", self.bridge._awake)


if __name__ == "__main__":
    unittest.main()
