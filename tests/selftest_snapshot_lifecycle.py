"""Isolated snapshot lease/crash/security tests; no backend or user data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import snapshot_lifecycle as S


class SnapshotLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='snapshot-lifecycle-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.work = self.base / 'custom-work'
        self.work.mkdir()
        self.registry = self.base / 'assistant' / '.runtime' / 'snapshot-lifecycle'

    def recover(self, **options):
        return S.reclaim_snapshots(self.registry, allowed_roots=[self.work], **options)

    def orphan(self, *, live=False):
        code = '''import os,sys
from pathlib import Path
from snapshot_lifecycle import managed_snapshot
with managed_snapshot(sys.argv[1], 'input.txt', registry_dir=sys.argv[2], task_id='journal-task-123', kind='batch') as path:
    Path(path).write_bytes(b'frozen bytes')
    print(path, flush=True)
    if sys.argv[3] == 'live': sys.stdin.readline()
    else: os._exit(0)
'''
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.work),
                                  str(self.registry), 'live' if live else 'crash'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def close_child():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(close_child)
        path = Path(child.stdout.readline().strip())
        self.assertTrue(path.is_file())
        if not live:
            child.wait(timeout=5)
            self.assertEqual(child.returncode, 0)
        return path, child

    def test_hard_exit_recovers_custom_workdir_without_touching_source_or_legacy(self):
        source = self.work / 'original.txt'
        source.write_bytes(b'original')
        legacy = self.work / '.wechat-batch-inputs-old-unregistered'
        legacy.mkdir()
        (legacy / 'unowned.txt').write_bytes(b'leave alone')
        path, _ = self.orphan()
        marker = json.loads((path.parent / S.MARKER).read_text())
        self.assertEqual(marker['task_id'], 'journal-task-123')
        self.assertRegex(marker['run_id'], '^[0-9a-f]{32}$')
        self.assertEqual(self.recover()['removed'], 1)
        self.assertFalse(path.parent.exists())
        self.assertEqual(source.read_bytes(), b'original')
        self.assertEqual((legacy / 'unowned.txt').read_bytes(), b'leave alone')

    def test_other_active_backend_is_protected_until_context_exits(self):
        path, child = self.orphan(live=True)
        self.assertEqual(self.recover()['active'], 1)
        self.assertEqual(path.read_bytes(), b'frozen bytes')
        child.communicate('\n', timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(path.exists())

    def test_marker_tampering_is_retained_until_explicit_repair(self):
        path, _ = self.orphan()
        marker = path.parent / S.MARKER
        saved = marker.read_bytes()
        record = json.loads(saved)
        record['task_id'] = 'different-task'
        marker.write_text(json.dumps(record))
        self.assertEqual(self.recover()['retained'], 1)
        self.assertTrue(path.exists())
        marker.write_bytes(saved)
        self.assertEqual(self.recover()['removed'], 1)

    def test_unknown_file_prevents_any_deletion(self):
        path, _ = self.orphan()
        extra = path.parent / 'user-notes.txt'
        extra.write_bytes(b'user file')
        self.assertEqual(self.recover()['retained'], 1)
        self.assertEqual(path.read_bytes(), b'frozen bytes')
        self.assertEqual(extra.read_bytes(), b'user file')

    def test_receipt_and_marker_cannot_redirect_recovery_outside_allowed_roots(self):
        path, _ = self.orphan()
        record = json.loads((path.parent / S.MARKER).read_text())
        outside = self.base / 'user-area'
        outside.mkdir()
        target = outside / path.parent.name
        target.mkdir()
        (target / 'input.txt').write_bytes(b'user data')
        record.update(root=str(outside), directory=str(target))
        (target / S.LEASE).write_bytes(b'1')
        (target / S.MARKER).write_text(json.dumps(record))
        (self.registry / (record['id'] + '.json')).write_text(json.dumps(record))
        self.assertEqual(self.recover()['retained'], 1)
        self.assertEqual((target / 'input.txt').read_bytes(), b'user data')
        self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_junction_directory_is_never_followed(self):
        path, _ = self.orphan()
        original = path.parent
        saved = original.with_name(original.name + '-saved')
        original.rename(saved)
        outside = self.base / 'user-area'
        outside.mkdir()
        (outside / 'input.txt').write_bytes(b'user data')
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(original), str(outside)],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        try:
            self.assertEqual(self.recover()['retained'], 1)
            self.assertEqual((outside / 'input.txt').read_bytes(), b'user data')
        finally:
            os.rmdir(original)  # Exact known junction; never recursively traverse it.

    @unittest.skipUnless(os.name == 'nt', 'Windows share-lock regression')
    def test_share_lock_preserves_success_then_next_recovery_removes_copy(self):
        held = None
        try:
            with S.managed_snapshot(self.work, 'input.txt', registry_dir=self.registry) as name:
                path = Path(name)
                path.write_bytes(b'sent')
                held = open(path, 'rb')
                outcome = 'accepted'
            self.assertEqual(outcome, 'accepted')
            self.assertTrue(path.exists())
            self.assertEqual(self.recover()['retained'], 1)
        finally:
            if held:
                held.close()
        self.assertEqual(self.recover()['removed'], 1)
        self.assertFalse(path.parent.exists())

    def test_original_exception_survives_cleanup_failure(self):
        with patch.object(S, '_cleanup', side_effect=PermissionError('fixture sharing violation')):
            with self.assertRaisesRegex(RuntimeError, 'original model failure'):
                with S.managed_snapshot(self.work, 'input.txt', registry_dir=self.registry) as name:
                    Path(name).write_bytes(b'input')
                    raise RuntimeError('original model failure')
        self.assertEqual(self.recover()['removed'], 1)

    def test_registry_lock_and_deadline_do_not_block_startup(self):
        path, _ = self.orphan()
        with S._lock(str(self.registry / '.registry.lock')):
            started = time.monotonic()
            self.recover()
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(path.exists())
        with patch.object(S, '_cleanup', wraps=S._cleanup) as cleanup:
            self.recover(max_seconds=0)
            cleanup.assert_not_called()
        self.assertTrue(path.exists())
        self.assertEqual(self.recover()['removed'], 1)

    def test_recovery_count_is_bounded_and_unregistered_names_are_ignored(self):
        self.registry.mkdir(parents=True)
        for n in range(20):
            (self.registry / ('%032x.json' % n)).write_text('{}')
        with patch.object(S, '_cleanup', side_effect=ValueError('invalid record')) as cleanup:
            self.recover(max_records=3)
            self.assertLessEqual(cleanup.call_count, 3)
        self.assertEqual(len(list(self.registry.glob('*.json'))), 20)

    def test_partial_cleanup_is_retryable_at_each_metadata_boundary(self):
        for target in (S.LEASE, S.MARKER, 'directory'):
            with self.subTest(target=target):
                path, _ = self.orphan()
                operation = 'rmdir' if target == 'directory' else 'unlink'
                original = getattr(os, operation)
                fired = []
                def fail_once(candidate, *args, **kwargs):
                    if not fired and (str(candidate) == str(path.parent) if target == 'directory'
                                      else os.path.basename(candidate) == target):
                        fired.append(True)
                        raise PermissionError('fixture transient metadata lock')
                    return original(candidate, *args, **kwargs)
                with patch.object(S.os, operation, side_effect=fail_once):
                    self.assertEqual(self.recover()['retained'], 1)
                self.assertTrue(fired)
                self.assertEqual(self.recover()['removed'], 1)
                self.assertFalse(path.parent.exists())

    def test_deep_json_is_retained_without_raising_from_cleanup(self):
        path, _ = self.orphan()
        receipt = next(self.registry.glob('*.json'))
        receipt.write_text('[' * 2000 + '0' + ']' * 2000)
        self.assertEqual(self.recover()['retained'], 1)
        self.assertTrue(path.exists())

    def test_production_poll_entry_reclaims_only_configured_fixture_roots(self):
        from wechat_bridge import Bridge
        bridge = Bridge.__new__(Bridge)
        bridge.workdir = str(self.work)
        bridge.client = SimpleNamespace(base_url='fixture.invalid', account_id='fixture')
        bridge.require_prefix = False
        bridge.allowed = set()
        bridge.chat_mode_enable = False
        bridge._desktop_stop = threading.Event()
        bridge._desktop_stop.set()
        bridge.poll_once = Mock(side_effect=AssertionError('network must not be polled'))
        here = str(self.registry.parent.parent)
        with patch('wechat_bridge.HERE', here), patch('wechat_bridge.reclaim_snapshots') as reclaim:
            bridge.poll_forever()
        reclaim.assert_called_once_with(os.path.join(here, '.runtime', 'snapshot-lifecycle'),
                                        allowed_roots=[here, str(self.work)], max_records=32, max_seconds=.15)
        bridge.poll_once.assert_not_called()


if __name__ == '__main__':
    unittest.main()
