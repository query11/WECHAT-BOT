import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from key_vault import (MAGIC,is_encrypted,is_protected,migrate_secret,
                       protect_text,read_secret,unprotect_text,write_secret)

@unittest.skipUnless(os.name=='nt','Windows DPAPI')
class KeyVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'api.key';self.value='sk-offline-key-vault-placeholder-123456'
    def test_current_user_roundtrip_no_plaintext_on_disk(self):
        write_secret(self.path,self.value)
        self.assertTrue(is_encrypted(self.path));self.assertNotIn(self.value.encode(),self.path.read_bytes())
        self.assertEqual(read_secret(self.path),self.value)
    def test_legacy_read_and_atomic_idempotent_migration(self):
        self.path.write_text(self.value,encoding='utf-8-sig')
        self.assertEqual(read_secret(self.path),self.value);self.assertTrue(migrate_secret(self.path))
        before=self.path.read_bytes();self.assertFalse(migrate_secret(self.path));self.assertEqual(before,self.path.read_bytes())
    def test_failed_migration_keeps_plain_original(self):
        self.path.write_text(self.value);before=self.path.read_bytes()
        with patch('key_vault.os.replace',side_effect=OSError('disk')):
            with self.assertRaises(OSError):migrate_secret(self.path)
        self.assertEqual(before,self.path.read_bytes());self.assertEqual(list(self.path.parent.iterdir()),[self.path])
    def test_corruption_never_falls_back_to_plaintext(self):
        self.path.write_bytes(MAGIC+b'AAAA')
        with self.assertRaises(OSError):read_secret(self.path)
    def test_exclusive_write_does_not_replace_existing_key(self):
        write_secret(self.path,self.value,exclusive=True);before=self.path.read_bytes()
        with self.assertRaises(FileExistsError):write_secret(self.path,'other',exclusive=True)
        self.assertEqual(before,self.path.read_bytes())

    def test_protected_text_keeps_plaintext_out_of_json(self):
        blob=protect_text(self.value)
        self.assertTrue(is_protected(blob));self.assertNotIn(self.value,blob)
        self.assertEqual(unprotect_text(blob),self.value)
        self.assertNotIn(self.value,json.dumps({'bot_token':blob}))
    def test_plaintext_passes_through_so_old_configs_still_load(self):
        self.assertFalse(is_protected(self.value))
        self.assertEqual(unprotect_text(self.value),self.value)
    def test_corrupt_text_never_falls_back_to_plaintext(self):
        for bad in (MAGIC.decode()+'!!!!',MAGIC.decode()+'AAAA'):
            with self.assertRaises(OSError):unprotect_text(bad)

if __name__=='__main__':unittest.main()
