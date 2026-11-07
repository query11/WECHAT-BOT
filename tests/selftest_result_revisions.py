"""Revision preview, lineage and real worker integration with fake transport."""
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_workflow import workflow_action
from ilink_client import Message
from runner_result import RunnerResult
from tests import selftest_desktop_workflow as desktop_fixture
from tests import selftest_template_integration as template_fixture
from task_artifacts import validate_artifact
from task_control import TaskQueue
from task_history import _read_rows
from task_journal import TaskJournal
from task_revisions import revision_source
from template_desktop import request_templates


class ResultRevisionTests(unittest.TestCase):
    setUp = desktop_fixture.DesktopWorkflowTests.setUp
    entry = desktop_fixture.DesktopWorkflowTests.entry
    worker_once = template_fixture.TemplateIntegrationTests.worker_once
    replies = template_fixture.TemplateIntegrationTests.replies

    def source(self, uid='owner'):
        path = self.workdir / '原报告.txt'; path.write_text('第一版报告', encoding='utf-8')
        ident, _ = self.b.journal.add(uid, None, 'task', '整理报告')
        self.b.journal.update(ident, 'completed', summary='第一版已生成')
        record = validate_artifact(str(path), **self.b._artifact_options())
        self.assertEqual(record['status'], 'valid', record)
        self.b.journal.save_artifacts(uid, ident, [record])
        self.b.journal.prepare_reply(ident, '原文字回复', 'completed')
        return ident, path

    def call(self, operation, **kw):
        return workflow_action(self.b, dict(operation=operation, uid='owner', journal_path=self.b.journal.path, **kw))

    def preview(self, ident, **kw):
        result = self.call('revision_preview', **dict(dict(task_id=ident, mode='files', indexes=[1], body='缩短内容，保留数据'), **kw))
        self.assertTrue(result['ok'], result)
        return result

    def confirm(self, preview):
        result = self.call('revision_run', token=preview['token'])
        self.assertTrue(result['ok'], result)
        return result

    def test_preview_does_not_write_or_run_and_duplicate_confirm_is_once(self):
        ident, original = self.source(); preview = self.preview(ident)
        self.assertFalse((self.workdir/'修订结果').exists())
        self.assertEqual(self.b.queue.qsize(), 0)
        response = self.confirm(preview)
        self.assertEqual(response, self.call('revision_run', token=preview['token']))
        self.assertEqual(self.b.queue.qsize(), 1)
        snapshot = self.b.queue.queue[0][2]['revision']
        self.assertEqual(Path(snapshot['files'][0]['path']).read_bytes(), original.read_bytes())
        self.assertNotEqual(snapshot['files'][0]['path'], str(original))
        self.assertEqual(snapshot['parent_id'], ident)
        self.b.runner.run.assert_not_called(); self.b.client.send_text.assert_not_called()

    def test_original_change_after_preview_rejects_confirmation(self):
        ident, original = self.source(); preview = self.preview(ident)
        original.write_text('别人修改后的版本', encoding='utf-8')
        response = self.call('revision_run', token=preview['token'])
        self.assertFalse(response['ok']); self.assertEqual(self.b.queue.qsize(), 0)
        self.assertFalse((self.workdir/'修订结果').exists())

    def test_worker_uses_preserved_copy_and_keeps_original_and_lineage(self):
        ident, original = self.source(); response = self.confirm(self.preview(ident))
        msg, _, job = self.b.queue.queue[0]; snapshot = job['revision']
        archive = Path(snapshot['files'][0]['path'])
        original.write_text('用户随后改过原文件', encoding='utf-8')
        output = Path(snapshot['output_dir'])/'修改版.txt'
        paths = []
        def runner(uid, prompt):
            self.assertIn('缩短内容', prompt)
            self.assertNotIn(str(original), prompt)
            self.assertNotIn(str(archive), prompt)
            paths.extend(f['path'] for f in msg._batch_frozen)
            self.assertEqual(Path(paths[0]).read_text(encoding='utf-8'), '第一版报告')
            output.write_text('第二版', encoding='utf-8')
            return RunnerResult('修改结束\n[[产物]]'+json.dumps(dict(path=str(output)), ensure_ascii=False))
        self.b.runner.run.side_effect = runner
        self.worker_once()
        self.assertTrue(all(not Path(p).exists() for p in paths))
        self.assertEqual(archive.read_text(encoding='utf-8'), '第一版报告')
        self.assertEqual(original.read_text(encoding='utf-8'), '用户随后改过原文件')
        rows, _ = _read_rows(Path(self.b.journal.path), 1, ident=response['task_id'])
        self.assertEqual(rows[0]['state'], 'completed')
        self.assertEqual(rows[0]['revision']['parent_id'], ident)
        self.assertEqual(rows[0]['artifacts'][0]['path'], str(output))
        self.assertEqual(len(revision_source(self.b, 'owner', ident)['versions']), 2)
        next_response = self.confirm(self.preview(response['task_id']))
        self.assertNotEqual(next_response['task_id'], response['task_id'])
        next_snapshot = self.b.queue.queue[0][2]['revision']
        self.assertEqual(next_snapshot['parent_id'], response['task_id'])
        self.assertEqual(next_snapshot['root_id'], ident)
        self.assertEqual(Path(next_snapshot['files'][0]['path']).read_text(encoding='utf-8'), '第二版')

    def test_changed_archive_fails_before_model(self):
        ident, _ = self.source(); self.confirm(self.preview(ident))
        archive = Path(self.b.queue.queue[0][2]['revision']['files'][0]['path'])
        archive.write_text('篡改副本', encoding='utf-8')
        self.worker_once(); self.b.runner.run.assert_not_called()

    def test_text_revision_preserves_cached_reply_and_expires_before_confirmation(self):
        ident, _ = self.source(); preview = self.preview(ident, mode='text', indexes=[])
        self.confirm(preview)
        snapshot = self.b.queue.queue[0][2]['revision']
        self.assertEqual(Path(snapshot['files'][0]['path']).read_text(encoding='utf-8'), '原文字回复')
        preview = self.preview(ident, mode='text', indexes=[])
        with self.b.journal.connect() as db:
            db.execute('UPDATE task_replies SET created=0 WHERE task_id=?', (ident,))
        self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        self.assertEqual(self.b.queue.qsize(), 1)

    def test_ownership_confirm_expiry_context_and_changed_workdir(self):
        foreign, _ = self.source('other')
        self.assertFalse(self.call('revision_preview', task_id=foreign, mode='files', indexes=[1], body='修改')['ok'])
        ident, _ = self.source(); preview = self.preview(ident)
        self.b._awake.clear()
        self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        self.b._awake.add('owner'); self.b._last_ctx.clear()
        self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        self.b._last_ctx['owner'] = ('owner', 'fixture')
        self.b._revision_plans[preview['token']]['at'] -= 901
        self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        preview = self.preview(ident); self.b.workdir = str(self.root/'elsewhere')
        self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_queue_full_creates_no_archive_and_can_confirm_later(self):
        ident, _ = self.source(); preview = self.preview(ident)
        self.b.queue = TaskQueue(maxsize=1); self.b.queue.put((SimpleNamespace(), '', None))
        self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        self.assertFalse((self.workdir/'修订结果').exists())
        self.b.queue.get_nowait(); self.b.queue.task_done(); self.confirm(preview)

    def test_lost_reply_after_enqueue_never_duplicates(self):
        ident, _ = self.source(); preview = self.preview(ident); enqueue = self.b._enqueue
        def lost(*args):
            enqueue(*args); raise OSError('lost reply')
        with patch.object(self.b, '_enqueue', side_effect=lost):
            self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        self.assertFalse(self.call('revision_run', token=preview['token'])['ok'])
        self.assertEqual(self.b.queue.qsize(), 1)

    def test_restart_restores_revision_and_pruning_keeps_lineage(self):
        ident, _ = self.source(); response = self.confirm(self.preview(ident))
        journal = TaskJournal(self.b.journal.path)
        entry = journal.claim_entry('owner', response['task_id'])
        self.assertEqual(entry['revision']['parent_id'], ident)
        journal.update(response['task_id'], 'completed')
        with journal.connect() as db:
            db.executemany("INSERT INTO tasks(id,uid,label,state,updated) VALUES (?,'owner','近期','completed',?)",
                           [(f'{i:032x}', time.time()+i) for i in range(205)])
            journal._prune_history(db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks WHERE id IN (?,?)', (ident, response['task_id'])).fetchone()[0], 2)

    def test_only_declared_files_are_eligible_and_outside_outputs_not_delivered(self):
        ident, original = self.source()
        self.assertFalse(self.call('revision_preview', task_id=ident, mode='files', indexes=[2], body='修改')['ok'])
        self.confirm(self.preview(ident))
        self.b.runner.run.return_value = RunnerResult('完成\n[[产物]]'+json.dumps(dict(path=str(original))))
        self.worker_once()
        self.b.client.send_file.assert_not_called()
        self.assertIn('未全部通过', self.replies())

    def test_control_client_forwards_focus_and_revision_fields(self):
        source = dict(kind='runtime', enabled=True, pid=100, path=self.b.journal.path, desktop_workflow=True)
        request = dict(operation='revision_preview', source={'pid':100, 'path':self.b.journal.path},
                       uid='owner', task_id='a'*32, item_id='b'*32, mode='files', indexes=[1], body='修改')
        with patch('template_desktop._source', return_value=(source,None,'')), patch('template_desktop.atomic_json') as write, \
             patch('template_desktop.Path.read_text', return_value='{"data":{"ok":true}}'), patch('template_desktop.Path.unlink'):
            result = request_templates(self.root, request, workflow=True)
        self.assertTrue(result['ok'])
        action = write.call_args.args[1]['action']
        for key in ('task_id','item_id','mode','indexes','body'):
            self.assertEqual(action[key], request[key])

    def test_real_recovery_keeps_revision_job_and_cancel_does_not_become_failure(self):
        from claude_runner import TaskCancelled
        ident, _ = self.source(); response = self.confirm(self.preview(ident))
        self.b.queue.get_nowait(); self.b.queue.task_done()
        self.b.journal = TaskJournal(self.b.journal.path)
        msg = Message(dict(from_user_id='owner', context_token='fixture', item_list=[]))
        self.assertIsNone(self.b._enqueue((msg, '', None), msg, recovery_id=response['task_id']))
        self.assertEqual(self.b.queue.queue[0][2]['revision']['parent_id'], ident)
        self.b.runner.run.side_effect = TaskCancelled('用户取消')
        self.worker_once()
        rows, _ = _read_rows(Path(self.b.journal.path), 1, ident=response['task_id'])
        self.assertEqual(rows[0]['state'], 'cancelled')

    def test_invalid_saved_revision_never_falls_back_to_plain_task(self):
        ident, _ = self.source(); response = self.confirm(self.preview(ident))
        journal = TaskJournal(self.b.journal.path)
        with journal.connect() as db:
            snapshot = json.loads(db.execute('SELECT revision FROM tasks WHERE id=?', (response['task_id'],)).fetchone()[0])
            snapshot['id'] = 'a'*32
            db.execute('UPDATE tasks SET revision=? WHERE id=?', (json.dumps(snapshot), response['task_id']))
        with self.assertRaises(ValueError):
            journal.claim_entry('owner', response['task_id'])
        with journal.connect(read_only=True) as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (response['task_id'],)).fetchone()[0], 'recoverable')


if __name__ == '__main__':
    unittest.main()
