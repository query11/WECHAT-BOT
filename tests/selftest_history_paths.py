"""Isolated task-history source selection; never use real config or credentials."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import task_history
from task_history_path import DEFAULT_JOURNAL_FILE, resolve_journal_path


class HistoryPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='历史 路径 ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def config(self, value):
        (self.root / 'config.json').write_text(json.dumps({
            'task_journal_file': value, 'bot_token': 'dpapi:deliberately-unreadable',
            'deepseek': {'api_key': 'never-read-this-key'}}, ensure_ascii=False), encoding='utf-8-sig')

    def database(self, relative='.runtime/tasks.sqlite', label='配置库', old=False):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE tasks(id TEXT,label TEXT,kind TEXT,state TEXT,updated REAL,payload TEXT)')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)',
                       ('a' * 32, label, 'task', 'running', 1, 'preserve-body'))
            if not old:
                db.execute("ALTER TABLE tasks ADD COLUMN summary TEXT DEFAULT '结果正文'")
                db.execute("ALTER TABLE tasks ADD COLUMN outputs TEXT DEFAULT '[]'")
        db.close()
        return path

    def status(self, source=None, **changes):
        path = self.root / '.runtime' / 'status.json'
        path.parent.mkdir(exist_ok=True)
        data = {'pid': os.getpid(), 'heartbeat': time.time(), 'state': 'working'}
        if source is not None:
            data['journal_source'] = source
        data.update(changes)
        path.write_text(json.dumps(data), encoding='utf-8')

    def test_configured_relative_path_does_not_read_old_default(self):
        self.database(label='过时默认库')
        self.database('自定义 目录/任务 数据.sqlite')
        self.config('自定义 目录/任务 数据.sqlite')
        self.assertEqual([r['label'] for r in task_history.read_history(self.root)], ['配置库'])

    def test_configured_absolute_path(self):
        path = self.database('另一处 路径/记录.sqlite')
        self.config(str(path))
        self.assertEqual([r['label'] for r in task_history.read_history(self.root)], ['配置库'])

    def test_disabled_does_not_show_old_default(self):
        self.database(label='过时默认库')
        self.config('')
        self.assertEqual(task_history.read_history(self.root), [])
        self.assertEqual(task_history.read_history_view(self.root)['state'], 'disabled')

    def test_shared_path_rules_default_relative_absolute_and_disabled(self):
        self.assertEqual(resolve_journal_path(self.root, DEFAULT_JOURNAL_FILE), self.root / '.runtime/tasks.sqlite')
        for raw in ('中文 路径/历史.sqlite', str(self.root / '中文 路径/历史.sqlite')):
            self.assertEqual(resolve_journal_path(self.root, raw), self.root / '中文 路径/历史.sqlite')
        for raw in (None, False, ''):
            self.assertIsNone(resolve_journal_path(self.root, raw))
        with self.assertRaises(ValueError):
            resolve_journal_path(self.root, {'path': 'invalid'})

    def test_default_config_and_legacy_no_config_source_are_explicit(self):
        self.database()
        view = task_history.read_history_view(self.root)
        self.assertEqual(view['source']['kind'], 'default')
        self.assertIn('未找到配置', view['source']['note'])
        self.config(DEFAULT_JOURNAL_FILE)
        view = task_history.read_history_view(self.root)
        self.assertEqual(view['source']['kind'], 'config')
        self.assertEqual(view['state'], 'ready')

    def test_missing_config_field_matches_backend_disabled(self):
        self.database()
        (self.root / 'config.json').write_text('{}', encoding='utf-8')
        self.assertEqual(task_history.read_history_view(self.root)['state'], 'disabled')

    def test_live_runtime_beats_changed_disk_config(self):
        loaded = self.database('已加载 库/任务.sqlite', label='后台实际库')
        self.database('待加载 库/任务.sqlite', label='尚未加载')
        self.config('待加载 库/任务.sqlite')
        self.status({'enabled': True, 'path': str(loaded)})
        view = task_history.read_history_view(self.root)
        self.assertEqual(view['source']['kind'], 'runtime')
        self.assertEqual(view['rows'][0]['label'], '后台实际库')
        # A malformed pending config must not prevent inspecting the running source.
        (self.root / 'config.json').write_text('{invalid', encoding='utf-8')
        self.assertEqual(task_history.read_history(self.root)[0]['label'], '后台实际库')

    def test_live_stale_heartbeat_does_not_guess_or_use_pending_config(self):
        loaded = self.database('loaded.sqlite')
        self.config('never-created.sqlite')
        self.status({'enabled': True, 'path': str(loaded)}, heartbeat=time.time() - 60)
        view = task_history.read_history_view(self.root)
        self.assertEqual(view['state'], 'source_unknown')
        self.assertEqual(view['rows'], [])
        self.assertIn('暂未刷新', view['message'])

    def test_live_runtime_disabled_ignores_enabled_pending_config(self):
        self.database()
        self.config(DEFAULT_JOURNAL_FILE)
        self.status({'enabled': False, 'path': ''})
        view = task_history.read_history_view(self.root)
        self.assertEqual(view['state'], 'disabled')
        self.assertEqual(view['source']['kind'], 'runtime')

    def test_live_old_or_invalid_status_source_does_not_guess(self):
        self.database()
        self.config(DEFAULT_JOURNAL_FILE)
        for source in (None, {'enabled': 'true', 'path': DEFAULT_JOURNAL_FILE},
                       {'enabled': True, 'path': DEFAULT_JOURNAL_FILE},
                       {'enabled': True, 'path': ''}):
            with self.subTest(source=source):
                self.status(source)
                view = task_history.read_history_view(self.root)
                self.assertEqual(view['state'], 'source_unknown')
                self.assertEqual(view['rows'], [])

    def test_stopped_or_dead_runtime_uses_offline_config(self):
        self.database('config.sqlite')
        self.config('config.sqlite')
        self.status({'enabled': True, 'path': str(self.root / 'old.sqlite')}, state='stopped')
        self.assertEqual(task_history.read_history_view(self.root)['source']['kind'], 'config')
        self.status({'enabled': True, 'path': str(self.root / 'old.sqlite')})
        with patch.object(task_history, '_process_alive', return_value=False):
            view = task_history.read_history_view(self.root)
        self.assertEqual(view['source']['kind'], 'config')
        self.assertEqual(view['rows'][0]['label'], '配置库')

    def test_missing_custom_source_never_falls_back_or_creates_default(self):
        self.config('不存在 目录/new.sqlite')
        view = task_history.read_history_view(self.root)
        self.assertEqual(view['state'], 'missing')
        self.assertFalse((self.root / '.runtime').exists())
        self.assertFalse((self.root / '不存在 目录').exists())
        self.database(label='过时默认库')
        self.assertEqual(task_history.read_history_view(self.root)['state'], 'missing')
        self.assertEqual(task_history.read_history(self.root), [])

    def test_unreadable_or_corrupt_config_never_falls_back(self):
        self.database()
        for raw in ('{invalid', '[]', '{"task_journal_file": 42}'):
            (self.root / 'config.json').write_text(raw, encoding='utf-8')
            view = task_history.read_history_view(self.root)
            self.assertEqual(view['state'], 'config_error')
            self.assertEqual(view['rows'], [])

    def test_corrupt_status_reports_unknown_instead_of_pending_config(self):
        self.database()
        self.config(DEFAULT_JOURNAL_FILE)
        (self.root / '.runtime/status.json').write_text('{invalid', encoding='utf-8')
        self.assertEqual(task_history.read_history_view(self.root)['state'], 'source_unknown')

    def test_database_failures_and_empty_are_distinct(self):
        path = self.database('custom.sqlite')
        self.config(str(path))
        with patch.object(task_history.sqlite3, 'connect', side_effect=PermissionError('do not leak detail')):
            view = task_history.read_history_view(self.root)
        self.assertEqual(view['state'], 'unavailable')
        self.assertNotIn('do not leak detail', json.dumps(view))
        path.write_bytes(b'not a database')
        self.assertEqual(task_history.read_history_view(self.root)['state'], 'unavailable')
        path.unlink()
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE unrelated(id TEXT)')
        db.close()
        self.assertEqual(task_history.read_history_view(self.root)['state'], 'schema_error')
        path.unlink()
        self.database('custom.sqlite')
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM tasks')
        db.close()
        view = task_history.read_history_view(self.root)
        self.assertEqual(view['state'], 'ready')
        self.assertEqual(view['message'], '暂无任务记录。')

    def test_old_columns_read_only_without_recovery_migration_or_decryption(self):
        path = self.database('旧 库/任务.sqlite', old=True)
        self.config(str(path))
        before, config_before = path.read_bytes(), (self.root / 'config.json').read_bytes()
        with (patch('task_journal.TaskJournal', side_effect=AssertionError('do not recover')),
              patch('bridge_config.load_config', side_effect=AssertionError('do not decrypt'))):
            view = task_history.read_history_view(self.root)
        self.assertEqual(view['state'], 'ready')
        self.assertEqual(view['missing_columns'], ['summary', 'outputs'])
        self.assertEqual(view['rows'][0]['state'], 'running')
        self.assertEqual(view['rows'][0]['summary'], '')
        self.assertEqual(view['rows'][0]['outputs'], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.root / 'config.json').read_bytes(), config_before)
        self.assertFalse((self.root / '.runtime').exists())
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT state,payload FROM tasks').fetchone(), ('running', 'preserve-body'))
            self.assertNotIn('summary', {r[1] for r in db.execute('PRAGMA table_info(tasks)')})
        db.close()

    def test_locked_database_returns_bounded_error_then_recovers(self):
        path = self.database('locked.sqlite')
        self.config(str(path))
        db = sqlite3.connect(path)
        try:
            db.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            view = task_history.read_history_view(self.root)
            self.assertEqual(view['state'], 'unavailable')
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            db.rollback()
            db.close()
        self.assertEqual(task_history.read_history_view(self.root)['state'], 'ready')

    def test_cli_view_keeps_expected_failures_structured(self):
        self.config('missing.sqlite')
        result = subprocess.run([sys.executable, str(Path(task_history.__file__)), '--root', str(self.root), '--view'],
                                capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0)
        view = json.loads(result.stdout)
        self.assertEqual(view['state'], 'missing')
        self.assertEqual(view['source']['path'], str(self.root / 'missing.sqlite'))


if __name__ == '__main__':
    unittest.main()
