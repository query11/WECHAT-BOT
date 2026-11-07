"""A broken/locked history must not hide a live worker or block desktop control."""
import json
import os
import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests import selftest_runtime
from bridge_runtime import atomic_json
from task_journal import TaskJournal


class HistoryResilienceTests(unittest.TestCase):
    def setUp(self):
        fixture = selftest_runtime.RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.rt, self.b = fixture.rt, fixture.b
        self.rt.connected()
        self.b.journal = TaskJournal(Path(fixture.temp.name) / 'history.sqlite')
        self.b.journal.add('owner', 'synthetic', 'task', '历史测试', payload='synthetic')

    def status(self):
        return json.loads((self.rt.root / 'status.json').read_text(encoding='utf-8'))

    def test_history_error_does_not_abort_status_and_does_not_claim_empty(self):
        with patch.object(self.b.journal, 'describe', side_effect=sqlite3.DatabaseError('secret must not be logged')):
            with self.assertLogs('bridge', level='WARNING') as logs:
                self.rt.tick()
        state = self.status()
        self.assertEqual(state['state'], 'standby')
        self.assertIn('无法读取', state['history'])
        self.assertNotIn('暂无任务记录', state['history'])
        self.assertNotIn('secret', '\n'.join(logs.output))

    def test_locked_database_has_bounded_read_and_control_still_replies(self):
        lock = sqlite3.connect(self.b.journal.path)
        self.addCleanup(lock.close)
        lock.execute('BEGIN EXCLUSIVE')
        atomic_json(self.rt.root / 'command-probe.json', {'pid': os.getpid(), 'at': time.time(), 'action': 'wake'})
        started = time.perf_counter()
        with self.assertLogs('bridge', level='WARNING'):
            self.rt.tick()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, 'history lock blocked desktop heartbeat')
        self.assertEqual(self.status()['state'], 'working')
        self.assertIn('无法读取', self.status()['history'])
        self.assertIn('已唤醒', (self.rt.root / 'reply-probe.json').read_text(encoding='utf-8'))
        lock.rollback()

    def test_cache_is_marked_stale_then_recovers_and_diagnostics_are_throttled(self):
        with patch('bridge_runtime.HISTORY_REFRESH_SECONDS', 0):
            self.rt.tick()
            with patch.object(self.b.journal, 'describe', side_effect=sqlite3.DatabaseError('synthetic')):
                with self.assertLogs('bridge', level='WARNING') as logs:
                    self.rt.tick()
                    heartbeat = self.status()['heartbeat']
                    self.rt.tick()
                    self.rt.tick()
                self.assertEqual(len(logs.output), 1)
                self.assertGreater(self.status()['heartbeat'], heartbeat)
                self.assertIn('上次', self.status()['history'])
                self.assertIn('历史测试', self.status()['history'])
            self.rt.tick()
        self.assertNotIn('无法读取', self.status()['history'])
        self.assertIn('历史测试', self.status()['history'])

    def test_missing_database_is_not_created_by_snapshot(self):
        Path(self.b.journal.path).unlink()
        with self.assertLogs('bridge', level='WARNING'):
            self.rt.tick()
        self.assertFalse(Path(self.b.journal.path).exists())
        self.assertIn('无法读取', self.status()['history'])

    def test_loaded_journal_source_uses_live_path_not_pending_config(self):
        self.b.cfg['task_journal_file'] = 'pending-change.sqlite'
        self.rt.tick()
        self.assertEqual(self.status()['journal_source'], {
            'enabled': True, 'path': str(Path(self.b.journal.path).resolve())})
        self.b.journal = None
        self.rt.tick()
        self.assertEqual(self.status()['journal_source'], {'enabled': False, 'path': ''})
        self.assertEqual(self.status()['history'], '')


if __name__ == '__main__':
    unittest.main()
