"""Offline regression checks for cursor persistence and dispatch failures."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ilink_client import ILinkClient
from wechat_bridge import Bridge


class ReliabilityTests(unittest.TestCase):
    def test_cursor_failure_keeps_old_state_and_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ILinkClient("https://example.invalid", "", "", state_dir=directory)
            client.commit_cursor("old")
            with patch("ilink_client.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    client.commit_cursor("new")
            self.assertEqual(client.cursor, "old")
            self.assertEqual(Path(client.cursor_file).read_text(encoding="utf-8"), "old")
            client.commit_cursor("new")
            restarted = ILinkClient("https://example.invalid", "", "", state_dir=directory)
            self.assertEqual(restarted.cursor, "new")

    def test_dispatch_failure_reports_uncertain_result_and_continues(self):
        first = SimpleNamespace(message_id="one", from_user_id="owner")
        second = SimpleNamespace(message_id="two", from_user_id="owner")
        bridge = Bridge.__new__(Bridge)
        bridge.allowed = {"owner"}
        bridge.client = Mock()
        bridge.client.get_updates.return_value = ([first, second], "next")
        bridge.handle = Mock(side_effect=[RuntimeError("failed"), None])
        bridge.reply = Mock()
        with self.assertLogs("bridge", level="ERROR"):
            self.assertEqual(bridge.poll_once(), 2)
        self.assertEqual(bridge.handle.call_count, 2)
        bridge.reply.assert_called_once()
        self.assertIn("结果未确认", bridge.reply.call_args.args[1])
        bridge.client.commit_cursor.assert_called_once_with("next")

    def test_failed_unknown_sender_gets_no_reply(self):
        bridge = Bridge.__new__(Bridge)
        bridge.allowed = {"owner"}
        bridge.client = Mock()
        bridge.client.get_updates.return_value = ([SimpleNamespace(
            message_id="one", from_user_id="stranger")], "next")
        bridge.handle = Mock(side_effect=RuntimeError("failed"))
        bridge.reply = Mock()
        with self.assertLogs("bridge", level="ERROR"):
            bridge.poll_once()
        bridge.reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
