"""Isolated configuration-switch regression tests; never changes live config."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.set_backend import main, set_backend


class BackendSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.key = "sk-offline-placeholder-123456789"
        self.config = {"backend": "claude", "bot_token": "untouched-token",
                       "claude_extra_args": ["--permission-mode", "acceptEdits"],
                       "deepseek": {"model": "deepseek-flash", "key_file": "private.key"},
                       "custom": {"keep": [1, 2, "原样"]}}
        self.write(self.config)
        (self.path.parent / "private.key").write_text(self.key, encoding="utf-8")

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_roundtrip_preserves_every_other_setting(self):
        with patch.dict(os.environ, {}, clear=True):
            set_backend("deepseek", self.path)
        expected = dict(self.config, backend="deepseek")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8-sig")), expected)
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        set_backend("claude", self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8-sig")), self.config)

    def test_invalid_model_or_missing_key_does_not_write(self):
        for options in ({"model": "bad"}, {"key_file": "missing.key"}, {"key_file": None}, {"key_file": "unsafe.txt"}):
            self.write(dict(self.config, deepseek=options))
            original = self.path.read_bytes()
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                set_backend("deepseek", self.path)
            self.assertEqual(self.path.read_bytes(), original)

    def test_environment_key_and_no_secret_output(self):
        self.write(dict(self.config, deepseek={"key_file": "missing.key"}))
        output = io.StringIO()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), contextlib.redirect_stdout(output):
            self.assertEqual(main(["deepseek", "--config", str(self.path)]), 0)
        self.assertNotIn(self.key, output.getvalue())
        self.assertNotIn("untouched-token", output.getvalue())
        self.assertIn("尚未重启", output.getvalue())

    def test_environment_key_cannot_bypass_unsafe_filename(self):
        self.write(dict(self.config, deepseek={"key_file": "unsafe.txt"}))
        original = self.path.read_bytes()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.key}), self.assertRaises(ValueError):
            set_backend("deepseek", self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_json_and_nonobject_untouched(self):
        for value in ("not json", "[]"):
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                set_backend("claude", self.path)
            self.assertEqual(self.path.read_text(), value)

    def test_failed_atomic_replace_preserves_original(self):
        original = self.path.read_bytes()
        with patch("tools.set_backend.os.replace", side_effect=OSError("simulated")), self.assertRaises(ValueError):
            set_backend("claude", self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob(".backend-*.tmp")))


if __name__ == "__main__":
    unittest.main()
