"""config.json 里 bot_token 的加密存放，以及旧明文配置的迁移。

DeepSeek 的 Key 一直走 DPAPI，bot_token 却在同一个目录里明文躺着——同一个威胁
模型两套标准。这些用例钉住三点：写盘不落明文、读回还是原来那个、旧明文配置
照样读得进（`selftest_base._cfg` 那种直接塞明文 token 的夹具全靠这条）。
"""
import json
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge_config
import wechat_bridge
from key_vault import MAGIC, is_protected, unprotect_text

TOKEN = "06000000000000000000000000000000000000"


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class BotTokenVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def raw(self):
        return self.path.read_text(encoding="utf-8-sig")

    def test_save_config_encrypts_and_load_config_decrypts(self):
        cfg = dict(bridge_config.DEFAULTS, workdir=self.tmp.name,
                   bot_token=TOKEN, account_id="acc")
        bridge_config.save_config(cfg, str(self.path))
        self.assertTrue(is_protected(json.loads(self.raw())["bot_token"]))
        self.assertNotIn(TOKEN, self.raw())
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(bridge_config.load_config(str(self.path))["bot_token"], TOKEN)

    def test_legacy_plaintext_is_read_and_reads_never_write(self):
        self.write({"workdir": self.tmp.name, "bot_token": TOKEN})
        before = self.path.read_bytes()
        self.assertEqual(bridge_config.load_config(str(self.path))["bot_token"], TOKEN)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_ciphertext_is_reported_not_silently_empty(self):
        self.write({"workdir": self.tmp.name, "bot_token": MAGIC.decode() + "AAAA"})
        with self.assertRaises(SystemExit):
            bridge_config.load_config(str(self.path))

    def test_login_replaces_unreadable_old_token_from_cli_entry(self):
        self.write({"bot_token": MAGIC.decode() + "AAAA", "allowed_user_ids": ["owner"]})
        got = {"bot_token": TOKEN, "account_id": "acc", "base_url": "https://example.invalid"}
        with patch.object(wechat_bridge, "load_config", side_effect=lambda **kwargs:
                          bridge_config.load_config(self.path, **kwargs)), \
                patch.object(wechat_bridge, "save_config", side_effect=lambda cfg:
                             bridge_config.save_config(cfg, self.path)), \
                patch.object(wechat_bridge, "qr_login", return_value=got) as qr, \
                patch.object(wechat_bridge, "setup_logging"), \
                patch.object(wechat_bridge, "HERE", self.tmp.name), redirect_stdout(io.StringIO()):
            self.assertEqual(wechat_bridge.main(["wechat_bridge.py", "login"]), 0)
        qr.assert_called_once()
        current = bridge_config.load_config(self.path)
        self.assertEqual(current["bot_token"], TOKEN)
        self.assertEqual(current["allowed_user_ids"], ["owner"])
        self.assertNotIn(TOKEN, self.raw())

    def test_login_still_rejects_invalid_config_structure(self):
        self.path.write_text("{ broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            bridge_config.load_config(self.path, for_login=True)

    def test_migration_is_idempotent_and_touches_nothing_else(self):
        self.write({"workdir": self.tmp.name, "bot_token": TOKEN,
                    "custom": {"keep": [1, 2, "原样"]}})
        changed, message = bridge_config.secure_bot_token(str(self.path))
        self.assertTrue(changed)
        self.assertIn("加密", message)
        disk = json.loads(self.raw())
        self.assertEqual(disk["custom"], {"keep": [1, 2, "原样"]})
        self.assertEqual(unprotect_text(disk["bot_token"]), TOKEN)
        self.assertNotIn(TOKEN, self.raw())
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        before = self.path.read_bytes()
        changed, message = bridge_config.secure_bot_token(str(self.path))
        self.assertFalse(changed)
        self.assertIn("已经是", message)
        self.assertEqual(self.path.read_bytes(), before)

    def test_migration_reports_a_config_without_a_token(self):
        self.write({"workdir": self.tmp.name})
        changed, message = bridge_config.secure_bot_token(str(self.path))
        self.assertFalse(changed)
        self.assertIn("没有", message)

    def test_migration_leaves_a_broken_config_alone(self):
        self.path.write_text("{ not json", encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(SystemExit):
            bridge_config.secure_bot_token(str(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_validation_failure_keeps_original_for_both_credential_writers(self):
        for writer in (bridge_config.secure_bot_token,
                       lambda path: bridge_config.save_config({"bot_token": TOKEN}, path)):
            for damaged in (MAGIC.decode() + "AAAA", bridge_config.key_vault.protect_text("wrong-token")):
                with self.subTest(writer=writer, corrupted=damaged.endswith("AAAA")):
                    self.write({"bot_token": TOKEN, "custom": "keep"})
                    before = self.path.read_bytes()
                    with patch("key_vault.protect_text", return_value=damaged):
                        with self.assertRaises(SystemExit):
                            writer(self.path)
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_change_is_preserved_for_both_credential_writers(self):
        real_protect = bridge_config.key_vault.protect_text
        newer = {"bot_token": "newer-offline-token", "custom": "newer"}
        def changed_while_encrypting(value):
            self.write(newer)
            return real_protect(value)
        for writer in (bridge_config.secure_bot_token,
                       lambda path: bridge_config.save_config({"bot_token": TOKEN}, path)):
            with self.subTest(writer=writer):
                self.write({"bot_token": TOKEN, "custom": "old"})
                with patch("key_vault.protect_text", side_effect=changed_while_encrypting):
                    with self.assertRaisesRegex(SystemExit, "其他程序修改"):
                        writer(self.path)
                self.assertEqual(json.loads(self.raw()), newer)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_replace_failure_preserves_original_and_removes_own_temporary_file(self):
        self.write({"bot_token": TOKEN})
        before = self.path.read_bytes()
        with patch("bridge_config.os.replace", side_effect=OSError("offline disk failure")):
            with self.assertRaises(OSError):
                bridge_config.secure_bot_token(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
