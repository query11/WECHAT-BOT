"""Local file task lifecycle using real files, journal and worker; fake transport."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import selftest_desktop_workflow as desktop_fixture
from tests import selftest_template_integration as template_fixture
from desktop_workflow import workflow_action
from ilink_client import Message
from runner_result import RunnerResult
from task_control import TaskQueue
from task_history import _read_rows
from task_journal import TaskJournal
from template_desktop import request_templates


class DesktopFileTests(unittest.TestCase):
    setUp = desktop_fixture.DesktopWorkflowTests.setUp
    entry = desktop_fixture.DesktopWorkflowTests.entry
    worker_once = template_fixture.TemplateIntegrationTests.worker_once
    replies = template_fixture.TemplateIntegrationTests.replies

    def source(self, name='本机材料.txt', text='原始材料'):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def call(self, operation, **kw):
        return workflow_action(self.b, dict(operation=operation, uid='owner', journal_path=self.b.journal.path, **kw))

    def preview(self, path, **kw):
        args = dict(paths=[str(path)], body='整理内容', delivery='summary')
        args.update(kw)
        result = self.call('files_preview', **args)
        self.assertTrue(result['ok'], result)
        return result

    def confirm(self, preview):
        result = self.call('files_run', token=preview['token'])
        self.assertTrue(result['ok'], result)
        return result

    def row(self, ident):
        return _read_rows(Path(self.b.journal.path), 1, ident=ident)[0][0]

    def test_preview_reads_only_explicit_file_and_confirm_is_once(self):
        path = self.source()
        self.b.send_roots = [str(self.workdir)]
        preview = self.preview(path)
        self.assertFalse((self.workdir/'本机任务').exists())
        self.assertEqual(self.b.queue.qsize(), 0)
        response = self.confirm(preview)
        self.assertEqual(response, self.call('files_run', token=preview['token']))
        self.assertEqual(self.b.queue.qsize(), 1)
        snap = self.b.queue.queue[0][2]['local_files']
        self.assertEqual(Path(snap['files'][0]['path']).read_bytes(), path.read_bytes())
        self.assertEqual(self.b.send_roots, [str(self.workdir)])
        self.b.runner.run.assert_not_called(); self.b.client.send_text.assert_not_called()

    def test_changed_source_before_confirm_creates_nothing(self):
        path = self.source(); preview = self.preview(path)
        path.write_text('稍后编辑', encoding='utf-8')
        response = self.call('files_run', token=preview['token'])
        self.assertFalse(response['ok']); self.assertIn('变化', response['message'])
        self.assertFalse((self.workdir/'本机任务').exists()); self.assertEqual(self.b.queue.qsize(), 0)

    def test_worker_uses_saved_bytes_and_summary_mode_never_uploads(self):
        path = self.source(); response = self.confirm(self.preview(path))
        msg, _, job = self.b.queue.queue[0]; snap = job['local_files']
        path.unlink(); output = Path(snap['output_dir'])/'结果.txt'; frozen = []
        def run(uid, prompt):
            self.assertNotIn('OLD RECENT CONTEXT', prompt)
            self.assertNotIn(str(path), prompt)
            frozen.extend(Path(f['path']) for f in msg._batch_frozen)
            self.assertEqual(frozen[0].read_text(encoding='utf-8'), '原始材料')
            output.write_text('结果', encoding='utf-8')
            return RunnerResult('已整理\n[[产物]]'+json.dumps(dict(path=str(output)))+'\n[[发文件]]'+str(output))
        self.b.runner.run.side_effect = run; self.worker_once()
        self.b.client.send_file.assert_not_called()
        self.assertEqual(self.row(response['task_id'])['state'], 'completed')
        self.assertEqual(self.row(response['task_id'])['local_files']['delivery'], '文件留在本机，摘要发微信')
        self.assertTrue(all(not f.exists() for f in frozen))

    def test_file_delivery_sends_only_verified_new_outputs_without_model_send_markers(self):
        path = self.source(); response = self.confirm(self.preview(path, delivery='files'))
        snap = self.b.queue.queue[0][2]['local_files']; output = Path(snap['output_dir'])/'结果.txt'; uploads = []
        def run(uid, prompt):
            output.write_text('新结果', encoding='utf-8')
            return RunnerResult('已整理\n[[产物]]'+json.dumps(dict(path=str(output)))+'\n[[发文件]]'+str(path))
        def send(uid, upload, **options):
            uploads.append(Path(upload)); self.assertEqual(Path(upload).read_text(encoding='utf-8'), '新结果')
            self.assertEqual(options['file_name'], '结果.txt')
            return options['file_name'], 9
        self.b.runner.run.side_effect = run; self.b.client.send_file.side_effect = send
        self.worker_once(); self.b.client.send_file.assert_called_once()
        self.assertEqual(self.row(response['task_id'])['state'], 'completed')
        self.assertTrue(all(not p.exists() for p in uploads))

    def test_template_version_and_checks_are_pinned(self):
        path = self.source(); definition = dict(self.definition, output_format='CSV', checks='列合计 | 汇总.csv | - | 金额 | 40')
        t = self.b.template_store.save('owner', '汇总', definition)
        preview = self.preview(path, body='', name='汇总', expected_revision=t['revision'])
        self.b.template_store.delete('owner', '汇总')
        response = self.confirm(preview); snap = self.b.queue.queue[0][2]['local_files']
        output = Path(snap['output_dir'])/'汇总.csv'
        def run(uid, prompt):
            self.assertIn('金额 | 40', prompt); self.assertIn('汇总', prompt)
            output.write_text('金额\n10\n30\n', encoding='utf-8')
            return RunnerResult('完成\n[[产物]]'+json.dumps(dict(path=str(output))))
        self.b.runner.run.side_effect = run; self.worker_once()
        row = self.row(response['task_id'])
        self.assertEqual(row['template']['revision'], t['revision'])
        self.assertEqual(row['result_checks'][0]['status'], 'passed')
        self.assertEqual(row['template']['definition']['output_dir'], snap['output_dir'])

    def test_source_guards_and_limits_reject_without_queueing(self):
        path = self.source(); secret = self.source('.env')
        invalid = [[str(secret)], [str(self.root)], [str(self.root/'missing.txt')], [str(path)]*2,
                   ['../relative.txt'], ['\\\\server\\share\\file.txt'], [str(path)]*13]
        for paths in invalid:
            with self.subTest(paths=paths):
                result = self.call('files_preview', paths=paths, body='整理', delivery='summary')
                self.assertFalse(result['ok'], result)
        large = self.source('too-large.txt')
        with large.open('wb') as stream: stream.truncate(20*1024*1024+1)
        self.assertFalse(self.call('files_preview', paths=[str(large)], body='整理', delivery='summary')['ok'])
        self.assertFalse((self.workdir/'本机任务').exists()); self.assertEqual(self.b.queue.qsize(), 0)

    def test_changed_archive_and_outside_result_fail_without_upload(self):
        path = self.source(); response = self.confirm(self.preview(path, delivery='files'))
        snap = self.b.queue.queue[0][2]['local_files']
        Path(snap['files'][0]['path']).write_text('被改过', encoding='utf-8')
        self.worker_once(); self.b.runner.run.assert_not_called()
        self.assertEqual(self.row(response['task_id'])['state'], 'failed')
        response = self.confirm(self.preview(path, delivery='files'))
        self.b.runner.run.return_value = RunnerResult('完成\n[[产物]]'+json.dumps(dict(path=str(path))))
        self.worker_once(); self.b.client.send_file.assert_not_called()
        self.assertEqual(self.row(response['task_id'])['state'], 'failed')

    def test_queue_full_and_delivery_disabled_create_no_copies(self):
        path = self.source(); preview = self.preview(path, delivery='files')
        self.b.send_file_enable = False
        self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        self.b.send_file_enable = True
        self.b.queue = TaskQueue(maxsize=1); self.b.queue.put((SimpleNamespace(), '', None))
        self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        self.assertFalse((self.workdir/'本机任务').exists())
        self.b.queue.get_nowait(); self.b.queue.task_done(); self.confirm(preview)

    def test_owner_expiry_awake_and_workdir_guards(self):
        path = self.source(); preview = self.preview(path)
        self.b.allowed.add('other')
        other = workflow_action(self.b, dict(operation='files_run', uid='other', journal_path=self.b.journal.path, token=preview['token']))
        self.assertFalse(other['ok'])
        self.b._awake.clear(); self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        self.b._awake.add('owner'); self.b._last_ctx.clear()
        self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        self.b._last_ctx['owner'] = ('owner', 'fixture')
        self.b._file_plans[preview['token']]['at'] -= 901
        self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        preview = self.preview(path); self.b.workdir = str(self.root/'new-workdir')
        self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_lost_enqueue_reply_never_resubmits(self):
        path = self.source(); preview = self.preview(path); enqueue = self.b._enqueue
        def lost(*args):
            enqueue(*args); raise OSError('lost response')
        with patch.object(self.b, '_enqueue', side_effect=lost):
            self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        self.assertFalse(self.call('files_run', token=preview['token'])['ok'])
        self.assertEqual(self.b.queue.qsize(), 1)

    def test_restart_and_cancel_keep_file_task_semantics(self):
        from claude_runner import TaskCancelled
        path = self.source(); response = self.confirm(self.preview(path))
        self.b.queue.get_nowait(); self.b.queue.task_done()
        self.b.journal = TaskJournal(self.b.journal.path); path.unlink()
        msg = Message(dict(from_user_id='owner', context_token='fake', item_list=[]))
        self.assertIsNone(self.b._enqueue((msg, '', None), msg, recovery_id=response['task_id']))
        self.assertEqual(self.b.queue.queue[0][2]['local_files']['delivery'], 'summary')
        self.b.runner.run.side_effect = TaskCancelled('取消')
        self.worker_once(); self.assertEqual(self.row(response['task_id'])['state'], 'cancelled')

    def test_corrupt_recovery_never_runs_as_plain_task(self):
        response = self.confirm(self.preview(self.source())); journal = TaskJournal(self.b.journal.path)
        with journal.connect() as db: db.execute("UPDATE tasks SET local_files='{}' WHERE id=?", (response['task_id'],))
        with self.assertRaises(ValueError): journal.claim_entry('owner', response['task_id'])
        self.assertEqual(self.row(response['task_id'])['state'], 'recoverable')

    def test_same_filename_in_different_folders_keeps_distinct_contents(self):
        first = self.source('重复名称.txt', '甲材料')
        folder = self.root/'另一个文件夹'; folder.mkdir()
        second = folder/first.name; second.write_text('乙材料', encoding='utf-8')
        response = self.confirm(self.preview(first, paths=[str(first), str(second)]))
        rows = self.b.queue.queue[0][2]['local_files']['files']
        self.assertEqual([Path(r['path']).read_text(encoding='utf-8') for r in rows], ['甲材料','乙材料'])
        self.assertEqual([r['orig'] for r in rows], [first.name, second.name])
        self.assertNotEqual(rows[0]['path'], rows[1]['path'])
        self.assertEqual(len(self.row(response['task_id'])['local_files']['files']), 2)

    def test_control_client_forwards_paths_as_json_not_shell(self):
        source = dict(kind='runtime', enabled=True, pid=100, path=self.b.journal.path, desktop_workflow=True)
        paths = [str(self.root / '含 空格 $符号.txt')]
        request = dict(operation='files_preview', source={'pid':100, 'path':self.b.journal.path},
                       uid='owner', paths=paths, body='整理', delivery='summary')
        with patch('template_desktop._source', return_value=(source,None,'')), patch('template_desktop.atomic_json') as write, \
             patch('template_desktop.Path.read_text', return_value='{"data":{"ok":true}}'), patch('template_desktop.Path.unlink'):
            result = request_templates(self.root, request, workflow=True)
        self.assertTrue(result['ok'])
        action = write.call_args.args[1]['action']
        self.assertEqual(action['paths'], paths); self.assertEqual(action['delivery'], 'summary')


if __name__ == '__main__':
    unittest.main()
