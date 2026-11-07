"""Template editor persistence and snapshot boundaries with isolated stores."""
import copy
import json
import unittest
from unittest.mock import Mock

from tests import selftest_batch_integration as fixtures
from desktop_templates import template_action
from task_templates import TaskTemplateStore, TemplateError
from template_desktop import request_templates


class DesktopTemplateTests(unittest.TestCase):
    message = fixtures.BatchIntegrationTests.message

    def setUp(self):
        fixtures.BatchIntegrationTests.setUp(self)
        self.b.template_store = TaskTemplateStore(self.b.journal.path)
        self.definition = dict(inputs='已确认材料', steps='汇总材料', output_dir='工作目录/周报', output_format='TXT')

    def request(self, operation='view', **values):
        return template_action(self.b, dict(operation=operation, uid='owner', journal_path=self.b.journal.path, **values))

    def save(self, name='周报', revision=0, definition=None):
        result = self.request('save', name=name, expected_revision=revision, definition=definition or self.definition)
        self.assertTrue(result['ok'], result)
        return result['template']

    def test_create_edit_copy_delete_preserve_monotonic_revisions(self):
        first = self.save()
        updated = self.save(revision=1, definition=dict(self.definition, steps='新要求'))
        self.assertEqual(updated['revision'], 2)
        self.save('周报副本', definition=updated['definition'])
        self.assertTrue(self.request('delete', name='周报', expected_revision=2)['ok'])
        recreated = self.save()
        self.assertEqual(recreated['revision'], 3)
        self.assertEqual(self.b.template_store.get('owner', '周报副本')['definition']['steps'], '新要求')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()
        self.b.reply.assert_not_called()

    def test_wechat_update_conflicts_with_stale_editor_save_and_delete(self):
        first = self.save()
        self.b.template_store.save('owner', '周报', dict(self.definition, steps='微信刚修改'))
        for operation in ('save', 'delete'):
            result = self.request(operation, name='周报', expected_revision=first['revision'], definition=self.definition)
            self.assertFalse(result['ok'])
            self.assertIn('其他地方', result['message'])
        self.assertEqual(self.b.template_store.get('owner', '周报')['definition']['steps'], '微信刚修改')

    def test_new_name_cannot_overwrite_existing_without_read(self):
        self.save()
        result = self.request('save', name='周报', expected_revision=0, definition=self.definition)
        self.assertFalse(result['ok'])
        self.assertEqual(self.b.template_store.get('owner', '周报')['revision'], 1)

    def test_same_revision_cannot_overwrite_after_delete_recreate(self):
        self.save()
        self.b.template_store.delete('owner', '周报')
        self.b.template_store.save('owner', '周报', self.definition)
        result = self.request('save', name='周报', expected_revision=1, definition=self.definition)
        self.assertFalse(result['ok'])

    def test_owner_and_source_are_validated(self):
        for changes in (dict(uid='stranger'), dict(journal_path=str(self.root / 'wrong.sqlite'))):
            payload=dict(operation='save', uid='owner', journal_path=self.b.journal.path, name='周报', expected_revision=0, definition=self.definition)
            payload.update(changes)
            self.assertFalse(template_action(self.b, payload)['ok'])
        self.assertEqual(self.b.template_store.list('owner'), [])

    def test_invalid_checks_and_outside_output_are_rejected(self):
        for changes in (dict(checks='随便检查一下'), dict(output_dir=str(self.root / 'outside')), dict(inputs='')):
            result=self.request('save', name='周报', expected_revision=0, definition=dict(self.definition, **changes))
            self.assertFalse(result['ok'], changes)
        self.assertEqual(self.b.template_store.list('owner'), [])

    def test_edit_and_delete_leave_accepted_snapshot_unchanged(self):
        first=self.save()
        msg=self.message('/template run 周报')
        self.b._template_command(msg,msg.text)
        before=copy.deepcopy(self.b.queue.queue[0][2]['template'])
        self.save(revision=1, definition=dict(self.definition, steps='之后的版本'))
        self.request('delete', name='周报', expected_revision=2)
        self.assertEqual(self.b.queue.queue[0][2]['template'], before)

    def test_seed_template_task_uses_accepted_version(self):
        self.save()
        msg=self.message('/template run 周报')
        self.b._template_command(msg,msg.text)
        self.b.journal.update(msg._journal_id,'completed')
        self.save(revision=1, definition=dict(self.definition, steps='新版'))
        result=self.request('seed',task_id=msg._journal_id)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['draft']['definition']['steps'],'汇总材料')
        self.assertEqual(result['draft']['name'],'周报副本')

    def test_seed_ordinary_task_is_explicit_incomplete_draft(self):
        ident,_=self.b.journal.add('owner','ordinary','task','生成报告')
        self.b.journal.update(ident,'completed',summary='结果包含另外的操作，不用作指令')
        result=self.request('seed',task_id=ident)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['draft']['definition']['steps'],'生成报告')
        self.assertEqual(result['draft']['definition']['output_format'],'')
        self.assertIn('只保留摘要', result['message'])
        self.assertEqual(self.b.template_store.list('owner'),[])

    def test_active_task_and_disallowed_owner_cannot_seed(self):
        for uid,state in (('owner','running'),('other','completed')):
            ident,_=self.b.journal.add(uid,uid,'task','任务摘要')
            self.b.journal.update(ident,state)
            self.assertFalse(self.request('seed',task_id=ident)['ok'])

    def test_stopped_or_old_runtime_cannot_create_control_request(self):
        result=request_templates(self.root,dict(operation='view'))
        self.assertFalse(result['ok'])
        self.assertFalse(list((self.root/'.runtime').glob('command-*.json')))

    def test_deleted_template_conflicts_with_edit(self):
        saved=self.save()
        self.b.template_store.delete('owner','周报')
        self.assertFalse(self.request('save',name='周报',expected_revision=saved['revision'],definition=self.definition)['ok'])


if __name__ == '__main__':
    unittest.main()
