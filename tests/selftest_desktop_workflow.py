"""Desktop preview/confirm and checklist races against real temporary stores."""
import json
import time
import unittest
from unittest.mock import patch

from attachment_batches import collect_input_records
from desktop_workflow import workflow_action, result_card, overview, attention_items
from task_history import read_history_view
from tests import selftest_workflow_actions as fixtures
from task_templates import TaskTemplateStore
from template_desktop import request_templates


class DesktopWorkflowTests(unittest.TestCase):
    entry = fixtures.WorkflowTests.entry

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.b.allowed.add('owner')
        self.b.template_store = TaskTemplateStore(self.b.journal.path)
        self.b._last_ctx['owner'] = ('owner', 'fixture-context')
        self.definition = dict(inputs='指定输入', steps='汇总', output_dir='工作目录', output_format='TXT')
        self.template = self.b.template_store.save('owner', '周报', self.definition)

    def call(self, operation, **kwargs):
        return workflow_action(self.b, dict(operation=operation, uid='owner', journal_path=self.b.journal.path, **kwargs))

    def preview(self, **kwargs):
        result = self.call('preview', name='周报', expected_revision=1, **kwargs)
        self.assertTrue(result['ok'], result)
        return result

    def batch(self, two=False):
        entries = [self.entry()]
        if two:
            entries.append(self.entry('two.txt'))
        return self.b.batch_store.create_recent('owner', '本次材料', collect_input_records(entries, self.inbox, [self.root], 'owner'))

    def test_preview_never_runs_and_duplicate_confirmation_queues_once(self):
        p = self.preview(body='按项目汇总')
        self.assertEqual(self.b.queue.qsize(), 0)
        first = self.call('run', token=p['token'])
        self.assertTrue(first['ok'], first)
        self.assertEqual(first, self.call('run', token=p['token']))
        self.assertEqual(self.b.queue.qsize(), 1)
        self.b.client.send_text.assert_not_called()
        self.b.runner.run.assert_not_called()

    def test_preview_pins_definition_after_template_deleted(self):
        p = self.preview()
        self.b.template_store.delete('owner', '周报')
        result = self.call('run', token=p['token'])
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.b.queue.queue[0][2]['template']['revision'], 1)

    def test_changed_template_before_preview_rejected(self):
        self.b.template_store.save('owner', '周报', self.definition)
        self.assertFalse(self.call('preview', name='周报', expected_revision=1)['ok'])

    def test_confirm_rechecks_awake_context_and_expiry(self):
        p = self.preview()
        self.b._awake.clear()
        self.assertIn('开始对话', self.call('run', token=p['token'])['message'])
        self.b._awake.add('owner')
        self.b._last_ctx.clear()
        self.assertIn('回复通道', self.call('run', token=p['token'])['message'])
        self.b._desktop_plans[p['token']]['at'] -= 901
        self.assertIn('过期', self.call('run', token=p['token'])['message'])
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_workdir_change_invalidates_confirm(self):
        p = self.preview()
        self.b.workdir = str(self.root / 'elsewhere')
        self.assertIn('工作目录', self.call('run', token=p['token'])['message'])
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_batch_requires_seal_and_cannot_run_after_other_consumption(self):
        batch = self.batch()
        self.assertFalse(self.call('preview', name='周报', expected_revision=1, batch_id=batch['id'])['ok'])
        self.b.batch_store.seal('owner', batch['id'])
        p = self.preview(batch_id=batch['id'])
        self.b.batch_store.cancel('owner', batch['id'])
        self.assertFalse(self.call('run', token=p['token'])['ok'])
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_batch_run_preserves_exact_inputs(self):
        batch = self.batch()
        self.b.batch_store.seal('owner', batch['id'])
        p = self.preview(batch_id=batch['id'])
        self.entry('not-selected.txt')
        result = self.call('run', token=p['token'])
        self.assertTrue(result['ok'], result)
        files = self.b.journal.inputs('owner', result['task_id'])['files']
        self.assertEqual([r['name'] for r in files], ['one.txt'])

    def test_stale_file_index_cannot_remove_different_file(self):
        batch = self.batch(two=True)
        version = self.b.batch_store.desktop_version(batch)
        self.b.batch_store.repair('owner', batch['id'], 'remove', 1)
        result = self.call('remove', batch_id=batch['id'], index=1, expected_version=version)
        self.assertFalse(result['ok'])
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['files'][0]['name'], 'two.txt')
        self.assertTrue((self.inbox / 'one.txt').exists())

    def test_seal_rejects_new_file_or_pending_intake(self):
        batch = self.batch()
        version = self.b.batch_store.desktop_version(batch)
        self.b.batch_store.begin_intake('owner', batch['id'], 'new-intake')
        self.assertFalse(self.call('seal', batch_id=batch['id'], expected_version=version)['ok'])
        current = self.b.batch_store.get('owner', batch['id'])
        self.assertFalse(self.call('seal', batch_id=batch['id'], expected_version=self.b.batch_store.desktop_version(current))['ok'])

    def test_repair_and_seal_keep_original_files(self):
        batch = self.batch(two=True)
        result = self.call('remove', batch_id=batch['id'], index=1, expected_version=self.b.batch_store.desktop_version(batch))
        self.assertTrue(result['ok'], result)
        batch = self.b.batch_store.get('owner', batch['id'])
        self.assertTrue(self.call('seal', batch_id=batch['id'], expected_version=self.b.batch_store.desktop_version(batch))['ok'])
        self.assertTrue((self.inbox / 'one.txt').exists())

    def test_pin_limit_survives_reopen_and_delete_does_not_pin_recreated(self):
        store = self.b.template_store
        for i in range(6):
            t = store.save('owner', '模板' + str(i), self.definition)
            result = self.call('pin', name=t['name'], enabled=True, expected_revision=1)
            self.assertEqual(result['ok'], i < 5)
        self.assertEqual(len(TaskTemplateStore(store.path).pinned('owner')), 5)
        store.delete('owner', '模板0')
        store.save('owner', '模板0', self.definition)
        self.assertEqual(len(store.pinned('owner')), 4)

    def test_user_and_source_isolation(self):
        p = self.preview()
        foreign = dict(operation='run', uid='other', token=p['token'], journal_path=self.b.journal.path)
        self.b.allowed.add('other')
        self.assertFalse(workflow_action(self.b, foreign)['ok'])
        foreign.update(uid='owner', journal_path=str(self.root / 'other.sqlite'))
        self.assertFalse(workflow_action(self.b, foreign)['ok'])
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_result_cards_keep_execution_and_checks_separate(self):
        row = dict(id='a'*32, label='报表', state='completed', updated=time.time(), summary='生成了文件',
                   artifacts=[dict(path=str(self.workdir/'a.csv'), name='a.csv', reason='存在')],
                   outputs=[str(self.workdir/'a.csv')], result_checks=[dict(status='failed')])
        result = result_card(row)
        self.assertIn('未通过', result['conclusion'])
        self.assertEqual(len(result['files']), 1)

    def test_client_rejects_restarted_runtime_before_run(self):
        source = dict(kind='runtime', enabled=True, pid=100, path=self.b.journal.path, desktop_workflow=True)
        with patch('template_desktop._source', return_value=(source, None, '')), patch('template_desktop.atomic_json') as write:
            result = request_templates(self.root, dict(operation='run', source=dict(pid=99, path=self.b.journal.path)), workflow=True)
        self.assertFalse(result['ok'])
        write.assert_not_called()

    def test_lost_confirmation_after_enqueue_never_repeats_submission(self):
        p = self.preview()
        enqueue = self.b._enqueue
        def accepted_then_failed(*args, **kwargs):
            enqueue(*args, **kwargs)
            raise OSError('simulated lost confirmation')
        with patch.object(self.b, '_enqueue', side_effect=accepted_then_failed) as call:
            self.assertFalse(self.call('run', token=p['token'])['ok'])
            self.assertFalse(self.call('run', token=p['token'])['ok'])
            self.assertEqual(call.call_count, 1)
        self.assertEqual(self.b.queue.qsize(), 1)

    def test_view_never_exposes_another_users_results(self):
        self.b.allowed.add('other')
        ident, _ = self.b.journal.add('other', 'other-result', 'task', 'foreign-private-label')
        self.b.journal.update(ident, 'completed', summary='foreign-private-summary')
        view = self.call('view')
        self.assertTrue(view['ok'], view)
        self.assertFalse(view['results'])

    def receipt(self, state, label='测试任务', uid='owner'):
        ident, _ = self.b.journal.add(uid, None, 'task', label, payload='private-task-payload')
        self.b.journal.update(ident, state, summary='仅用于测试')
        return ident

    def test_attention_deduplicates_reasons_and_removes_resolved_items(self):
        ident = self.receipt('uncertain')
        reply = self.b.journal.prepare_reply(ident, 'private-reply-body', 'completed')
        self.b.journal.record_delivery(reply, 0, 1, 'unconfirmed')
        checks = [dict(file='test.csv', kind='column_sum', status='failed', detail='合计不同')]
        with self.b.journal.connect() as db:
            db.execute('UPDATE tasks SET result_checks=? WHERE id=?', (json.dumps(checks), ident))
        items = attention_items(self.b)
        self.assertEqual(len(items), 1)
        self.assertIn('发送未确认', items[0]['detail'])
        self.assertIn('检查待核对', items[0]['detail'])
        snapshot = json.dumps(overview(self.b))
        self.assertNotIn('private-reply-body', snapshot)
        self.assertNotIn('private-task-payload', snapshot)
        self.b.journal.resolve('owner', ident, 'checked')
        self.assertEqual(attention_items(self.b), [])
        self.b.runner.run.assert_not_called()
        self.b.client.send_text.assert_not_called()

    def test_attention_excludes_live_and_removed_users_and_keeps_failures(self):
        self.receipt('failed', 'removed-user-private', 'removed')
        for state in ('queued', 'running', 'waiting', 'completed', 'cancelled'):
            self.receipt(state)
        failed = self.receipt('failed')
        recoverable = self.receipt('recoverable')
        self.assertEqual({i['id'] for i in attention_items(self.b)}, {failed, recoverable})
        self.assertNotIn('removed-user-private', json.dumps(overview(self.b)))
        self.b.journal.resolve('owner', failed, 'archived')
        self.b.journal.resolve('owner', recoverable, 'abandoned')
        self.assertEqual(attention_items(self.b), [])

    def test_attention_batch_waits_for_download_and_clears_after_seal(self):
        batch = self.batch()
        self.assertIn('清单待确认', attention_items(self.b)[0]['detail'])
        self.b.batch_store.begin_intake('owner', batch['id'], 'pending-download')
        self.assertEqual(attention_items(self.b), [])
        self.b.batch_store.finish_intake('owner', batch['id'], 'pending-download', [], failed=1)
        self.assertIn('缺失待处理', attention_items(self.b)[0]['detail'])
        current = self.b.batch_store.get('owner', batch['id'])
        self.b.batch_store.repair('owner', batch['id'], 'dismiss', current['failures'][0]['id'])
        self.b.batch_store.seal('owner', batch['id'])
        self.assertEqual(attention_items(self.b), [])

    def test_old_unresolved_receipt_remains_visible_and_can_be_opened(self):
        ident = self.receipt('recoverable')
        with self.b.journal.connect() as db:
            db.executemany("INSERT INTO tasks(id,uid,label,state,updated) VALUES (?,'owner','近期完成','completed',?)",
                           [(f'{i:032x}', time.time() + i) for i in range(205)])
        self.assertEqual(attention_items(self.b)[0]['id'], ident)
        self.assertNotIn(ident, [r['id'] for r in read_history_view(self.root)['rows']])
        view = read_history_view(self.root, task_id=ident)
        self.assertIn(ident, [r['id'] for r in view['rows']])

    def test_focus_selects_correct_owner_and_rejects_removed_owner(self):
        ident = self.receipt('failed', uid='other')
        request = dict(operation='view', item_id=ident, journal_path=self.b.journal.path)
        self.assertFalse(workflow_action(self.b, request)['ok'])
        self.b.allowed.add('other')
        response = workflow_action(self.b, request)
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['uid'], 'other')
        self.assertEqual(response['results'][0]['id'], ident)
        self.assertFalse(self.call('view', item_id='not-an-id')['ok'])
        self.assertFalse(workflow_action(self.b, dict(request, item_id='f'*32))['ok'])
        self.assertFalse(self.call('view', item_id=ident)['results'])

    def test_focus_keeps_older_batch_outside_recent_ten(self):
        batch = self.batch()
        with self.b.journal.connect() as db:
            db.executemany("INSERT INTO attachment_batches(id,uid,name,state,created,updated) "
                           "VALUES (?,'owner','旧清单','cancelled',?,?)",
                           [(f'{i:032x}', time.time()+i, time.time()+i) for i in range(12)])
        view = workflow_action(self.b, dict(operation='view', item_id=batch['id'], journal_path=self.b.journal.path))
        self.assertTrue(view['ok'], view)
        self.assertEqual(view['batches'][0]['id'], batch['id'])
        self.assertEqual(attention_items(self.b)[0]['id'], batch['id'])

    def test_overview_read_failure_is_distinct_from_empty(self):
        self.assertEqual(overview(self.b)['attention'], dict(total=0, items=[]))
        with patch('desktop_workflow.attention_items', side_effect=OSError('locked')):
            view = overview(self.b)
        self.assertIsNone(view['attention'])
        self.assertIn('暂不可读', view['message'])

    def test_result_card_selects_failure_action_and_text_only_action(self):
        row = dict(id='a'*32, label='任务', state='failed', updated=time.time())
        card = result_card(row)
        self.assertEqual(card['tone'], 'error')
        self.assertEqual(card['action_label'], '查看失败原因')
        self.assertEqual(card['files'], [])
        row['state'] = 'completed'
        self.assertEqual(result_card(row)['action_label'], '查看完整回复')


if __name__ == '__main__':
    unittest.main()
