"""Real isolated SQLite template regressions. No model, inbox or transport."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import task_templates as T


class TaskTemplateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / 'tasks.sqlite'
        self.store = T.TaskTemplateStore(self.path)
        self.definition = dict(inputs='收集发票和模板文件', steps='逐张读取金额\n核对日期，汇总表格',
                               output_dir='D:/报销结果', output_format='Excel 表格及核对说明')

    def saved(self, uid='alice', name='报销汇总', **changes):
        return self.store.save(uid, name, dict(self.definition, **changes))

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(sql, params).fetchall()

    def test_roundtrip_chinese_multiline_and_restart(self):
        saved = self.saved()
        self.assertEqual(saved['definition'], self.definition)
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(len(saved['id']), 32)
        reopened = T.TaskTemplateStore(self.path)
        self.assertEqual(reopened.get('alice', '报销汇总'), saved)
        self.assertEqual(reopened.list('alice'), [saved])

    def test_constructor_creates_parent_directory(self):
        path = self.base / 'nested' / 'new' / 'tasks.sqlite'
        store = T.TaskTemplateStore(path)
        self.assertEqual(store.list('alice'), [])
        self.assertTrue(path.is_file())

    def test_constructor_keeps_new_and_legacy_tasks_untouched(self):
        for name, schema in (('legacy.sqlite', 'id TEXT,state TEXT,payload TEXT'),
                             ('new.sqlite', 'id TEXT,state TEXT,payload TEXT,inputs TEXT')):
            with self.subTest(schema=schema):
                path = self.base / name
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('CREATE TABLE tasks (' + schema + ')')
                    values = ('keep', 'queued', 'original') + (('fixed-inputs',) if name == 'new.sqlite' else ())
                    db.execute('INSERT INTO tasks VALUES (' + ','.join('?' for _ in values) + ')', values)
                T.TaskTemplateStore(path)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT * FROM tasks').fetchall(), [values])

    def test_existing_template_schema_gains_revision_ledger(self):
        old = self.saved()
        self.execute('DROP TABLE task_template_revisions')
        reopened = T.TaskTemplateStore(self.path)
        current = reopened.save('alice', old['name'], self.definition)
        self.assertEqual(current['revision'], 2)
        self.assertEqual(current['id'], old['id'])

    def test_incompatible_schema_rejected_atomically(self):
        path = self.base / 'incompatible.sqlite'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE task_templates (id TEXT,uid TEXT,name TEXT)')
            db.execute('CREATE TABLE tasks (id TEXT,state TEXT)')
            db.execute("INSERT INTO tasks VALUES ('keep','queued')")
        with self.assertRaises(T.TemplateError):
            T.TaskTemplateStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT * FROM tasks').fetchall(), [('keep', 'queued')])
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='task_template_revisions'").fetchall(), [])

    def test_other_owner_cannot_read_delete_or_overwrite(self):
        alice = self.saved()
        self.assertIsNone(self.store.get('bob', alice['name']))
        self.assertEqual(self.store.list('bob'), [])
        self.assertFalse(self.store.delete('bob', alice['name']))
        bob = self.saved('bob', steps='另一个用户的要求')
        self.assertNotEqual(bob['id'], alice['id'])
        self.assertEqual(bob['revision'], 1)
        self.assertEqual(self.store.get('alice', alice['name']), alice)

    def test_update_preserves_identity_and_created_increments_revision(self):
        first = self.saved()
        second = self.saved(steps='新的明确步骤')
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['created'], first['created'])
        self.assertGreaterEqual(second['updated'], first['updated'])
        self.assertEqual(second['revision'], 2)
        self.assertEqual(second['definition']['steps'], '新的明确步骤')

    def test_clock_correction_does_not_corrupt_metadata(self):
        with patch.object(T.time, 'time', return_value=100):
            first = self.saved()
        with patch.object(T.time, 'time', return_value=20):
            second = self.saved()
        self.assertEqual((first['created'], second['updated']), (100, 100))
        self.assertEqual(self.store.get('alice', first['name']), second)

    def test_delete_missing_and_recreate_keeps_increasing_version(self):
        first = self.saved()
        self.assertFalse(self.store.delete('alice', '不存在'))
        self.assertTrue(self.store.delete('alice', first['name']))
        self.assertIsNone(self.store.get('alice', first['name']))
        self.assertFalse(self.store.delete('alice', first['name']))
        reopened = T.TaskTemplateStore(self.path)
        current = reopened.save('alice', first['name'], self.definition)
        self.assertNotEqual(first['id'], current['id'])
        self.assertEqual(current['revision'], 2)

    def test_limit_is_per_user_and_update_allowed_at_capacity(self):
        for index in range(T.MAX_TEMPLATES):
            self.saved(name='模板%d' % index)
        with self.assertRaisesRegex(T.TemplateError, '30'):
            self.saved(name='超出上限')
        updated = self.saved(name='模板0', steps='容量满仍可更新')
        self.assertEqual(updated['revision'], 2)
        self.saved('bob', '另一个用户的模板')
        self.assertEqual(len(self.store.list('alice')), 30)
        self.assertTrue(self.store.delete('alice', '模板1'))
        self.saved(name='新增模板')
        self.assertEqual(len(self.store.list('alice')), 30)

    def test_list_order_updated_then_name(self):
        with patch.object(T.time, 'time', return_value=100):
            self.saved(name='B')
            self.saved(name='A')
        with patch.object(T.time, 'time', return_value=200):
            self.saved(name='C')
        self.assertEqual([r['name'] for r in self.store.list('alice')], ['C', 'A', 'B'])

    def test_input_and_returned_dict_mutations_do_not_alter_catalog(self):
        provided = dict(self.definition)
        saved = self.store.save('alice', '快照', provided)
        expected = copy.deepcopy(saved)
        provided['steps'] = '原始输入已经变化'
        saved['definition']['steps'] = '返回值已经变化'
        loaded = self.store.get('alice', '快照')
        self.assertEqual(loaded, expected)
        loaded['definition']['steps'] = '读取返回值也已变化'
        listed = self.store.list('alice')
        listed[0]['definition']['steps'] = '清单返回值已变化'
        self.assertEqual(self.store.get('alice', '快照'), expected)

    def test_snapshot_remains_valid_after_catalog_update_and_delete(self):
        snapshot = T.validate_snapshot(self.saved(), 'alice')
        original = copy.deepcopy(snapshot)
        self.saved(steps='后来修改的模板')
        self.store.delete('alice', snapshot['name'])
        self.assertEqual(T.validate_snapshot(snapshot, 'alice'), original)
        second = T.validate_snapshot(snapshot, 'alice')
        second['definition']['steps'] = '调用者变化'
        self.assertEqual(snapshot, original)

    def test_snapshot_rejects_foreign_owner_and_malformed_fields(self):
        saved = self.saved()
        with self.assertRaisesRegex(T.TemplateError, '不属于'):
            T.validate_snapshot(saved, 'bob')
        invalid = [dict(saved, extra='unexpected'), {k: v for k, v in saved.items() if k != 'name'},
                   dict(saved, id='not-an-id'), dict(saved, revision=True), dict(saved, revision=0),
                   dict(saved, definition=[]), dict(saved, updated=-1), dict(saved, created=float('nan')),
                   dict(saved, name='x\n'), dict(saved, uid=None), dict(saved, revision='1')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(T.TemplateError):
                T.validate_snapshot(value, 'alice')

    def test_invalid_owner_rejected_for_every_operation(self):
        for uid in ('', None, [], 'a' * 301, 'line\nbreak', 'x\x00', '\ud800'):
            with self.subTest(uid=uid):
                for action in (lambda: self.store.save(uid, '任务', self.definition),
                               lambda: self.store.get(uid, '任务'), lambda: self.store.list(uid),
                               lambda: self.store.delete(uid, '任务')):
                    with self.assertRaises(T.TemplateError):
                        action()

    def test_invalid_name_rejected_and_name_spaces_normalized(self):
        for name in ('', '   ', None, [], 'a' * 41, 'x/y', 'x\\y', 'x\ny', 'x\ry',
                     'x\ty', 'x\x00y', 'x\x7fy', 'x\u202ey', '\ud800', '周报 模板', '周报\u00a0模板'):
            with self.subTest(name=name):
                for action in (lambda: self.store.save('alice', name, self.definition),
                               lambda: self.store.get('alice', name), lambda: self.store.delete('alice', name)):
                    with self.assertRaises(T.TemplateError):
                        action()
        saved = self.saved(name='  收入汇总  ')
        self.assertEqual(saved['name'], '收入汇总')
        self.assertEqual(self.store.get('alice', ' 收入汇总 '), saved)

    def test_definition_requires_exact_nonempty_text_fields(self):
        bad = [None, [], 'text', {}, dict(self.definition, extra='x'),
               {k: v for k, v in self.definition.items() if k != 'inputs'}]
        for field in T.DEFINITION_LIMITS:
            bad.extend(dict(self.definition, **{field: value}) for value in
                       (None, [], {}, 0, True, '', ' \n\t ', 'x\x00y', 'x\x01y', '\ud800'))
        for value in bad:
            with self.subTest(value=value), self.assertRaises(T.TemplateError):
                self.store.save('alice', '非法模板', value)
        self.assertEqual(self.store.list('alice'), [])

    def test_definition_field_boundaries(self):
        bounded = {key: '字' * limit for key, limit in T.DEFINITION_LIMITS.items()}
        self.assertEqual(self.store.save('alice', '最大定义', bounded)['definition'], bounded)
        for field, limit in T.DEFINITION_LIMITS.items():
            with self.subTest(field=field), self.assertRaises(T.TemplateError):
                self.store.save('alice', '最大定义', dict(bounded, **{field: '字' * (limit + 1)}))
        self.assertEqual(self.store.get('alice', '最大定义')['revision'], 1)

    def test_text_paths_and_shell_fragments_are_not_executed_or_opened(self):
        sentinel = self.base / 'must-not-exist'
        body = '$(Remove-Item anything); __import__("os").remove("secret")'
        definition = dict(self.definition, steps=body,
                          output_dir=str(sentinel / 'missing' / 'folder'))
        saved = self.store.save('alice', '纯文本', definition)
        self.assertEqual(saved['definition'], definition)
        self.assertFalse(sentinel.exists())

    def test_sql_like_user_and_name_remain_parameterized_text(self):
        uid, name = "x' OR 1=1 --", "x';SELECT(1);--"
        saved = self.saved(uid, name)
        self.assertEqual(self.store.get(uid, name), saved)
        self.assertEqual(self.store.list('alice'), [])
        self.assertTrue(self.store.delete(uid, name))
        self.assertEqual(self.store.list(uid), [])

    def test_corrupt_json_and_types_fail_closed_without_payload_leak(self):
        saved = self.saved()
        malformed = ('{"inputs":"secret-private-path"', '[]', 'null', 'true', '{}',
                     json.dumps(dict(self.definition, steps=['secret-private-path'])),
                     '{"inputs":"one","inputs":"secret-private-path",'
                     '"steps":"s","output_dir":"d","output_format":"f"}',
                     'x' * (T.MAX_DEFINITION_JSON + 1))
        for raw in malformed:
            with self.subTest(raw=raw[:80]):
                self.execute('UPDATE task_templates SET definition=? WHERE id=?', (raw, saved['id']))
                for action in (lambda: self.store.get('alice', saved['name']),
                               lambda: self.store.list('alice'), lambda: self.saved()):
                    with self.assertRaises(T.TemplateError) as caught:
                        action()
                    self.assertNotIn('secret-private-path', str(caught.exception))
                self.assertEqual(self.execute('SELECT revision FROM task_templates')[0][0], 1)
        self.assertTrue(self.store.delete('alice', saved['name']))
        self.assertEqual(self.saved()['revision'], 2)

    def test_corrupt_metadata_fails_closed(self):
        saved = self.saved()
        cases = (('id', 'bad-id'), ('revision', 'bad-version'), ('revision', 0),
                 ('created', -1), ('updated', -1), ('updated', None))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                # SQLite correctly rejects NULL on the newer schema itself.
                if value is None:
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.execute('UPDATE task_templates SET ' + field + '=?', (value,))
                    continue
                self.execute('UPDATE task_templates SET ' + field + '=?', (value,))
                with self.assertRaises(T.TemplateError):
                    self.store.get('alice', saved['name'])
                self.execute('UPDATE task_templates SET ' + field + '=?', (saved[field],))

    def test_revision_ledger_corruption_and_overflow_do_not_change_definition(self):
        saved = self.saved()
        for revision in ('broken-secret', 0, 2 ** 63 - 2):
            with self.subTest(revision=revision):
                self.execute('UPDATE task_template_revisions SET revision=?', (revision,))
                with self.assertRaises(T.TemplateError):
                    self.saved(steps='不能保存')
                self.assertEqual(self.store.get('alice', saved['name']), saved)

    def test_concurrent_updates_have_atomic_monotonic_revisions(self):
        barrier = threading.Barrier(8)

        def save(index):
            barrier.wait()
            return self.saved(steps='步骤%d' % index)

        with ThreadPoolExecutor(max_workers=8) as executor:
            values = list(executor.map(save, range(8)))
        self.assertEqual(sorted(value['revision'] for value in values), list(range(1, 9)))
        self.assertEqual(len({value['id'] for value in values}), 1)
        self.assertEqual(self.store.get('alice', '报销汇总')['revision'], 8)

    def test_save_insert_failure_rolls_back_template_and_revision(self):
        self.execute("CREATE TRIGGER prevent_ledger BEFORE INSERT ON task_template_revisions "
                     "BEGIN SELECT RAISE(ABORT,'private-db-internal'); END")
        with self.assertRaises(T.TemplateError) as caught:
            self.saved()
        self.assertNotIn('private-db-internal', str(caught.exception))
        self.assertEqual(self.store.list('alice'), [])
        self.assertEqual(self.execute('SELECT * FROM task_template_revisions'), [])

    def test_save_update_failure_rolls_back_both_definition_and_revision(self):
        saved = self.saved()
        self.execute("CREATE TRIGGER prevent_ledger BEFORE UPDATE ON task_template_revisions "
                     "BEGIN SELECT RAISE(ABORT,'secret internal trigger'); END")
        with self.assertRaises(T.TemplateError):
            self.saved(steps='更新被拒绝')
        self.assertEqual(self.store.get('alice', saved['name']), saved)
        self.assertEqual(self.execute('SELECT revision FROM task_template_revisions'), [(1,)])

    def test_delete_failure_keeps_template_and_revision(self):
        saved = self.saved()
        self.execute("CREATE TRIGGER prevent_delete BEFORE DELETE ON task_templates "
                     "BEGIN SELECT RAISE(ABORT,'private secret'); END")
        with self.assertRaises(T.TemplateError):
            self.store.delete('alice', saved['name'])
        self.assertEqual(self.store.get('alice', saved['name']), saved)
        self.assertEqual(self.execute('SELECT revision FROM task_template_revisions'), [(1,)])

    def test_busy_database_returns_safe_error_without_mutation(self):
        saved = self.saved()
        with closing(sqlite3.connect(self.path)) as locked:
            locked.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(T.TemplateError, '请稍后重试'):
                self.saved(steps='被锁定的更改')
            locked.rollback()
        self.assertEqual(self.store.get('alice', saved['name']), saved)

    def test_database_disappearance_and_non_database_return_safe_errors(self):
        self.path.unlink()
        with self.assertRaises(T.TemplateError):
            self.store.get('alice', '报销汇总')
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b'private non sqlite payload')
        with self.assertRaises(T.TemplateError) as caught:
            T.TaskTemplateStore(self.path)
        self.assertNotIn('private non sqlite payload', str(caught.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
