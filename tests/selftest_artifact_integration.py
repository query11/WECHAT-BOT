"""Real temporary files/journal/queue with fake model/transport; no user data."""
import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ilink_client import Message, ILinkError
from runner_result import RunnerResult
from task_control import TaskQueue
from task_journal import TaskJournal
from task_history import read_history
from wechat_bridge import Bridge
from tests import selftest_standby


class ArtifactIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch('bridge_artifacts.HERE', str(self.root)).start()
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        self.b = fixture.bridge
        self.b.queue = TaskQueue()
        self.b.workdir = str(self.root)
        self.b.send_roots = [str(self.root)]
        self.b.send_file_enable = True
        self.b.send_file_limit = 3
        self.b.send_file_max_mb = 20
        self.b.queue_max = 20
        self.b.enable_typing = False
        self.b.notice_after = 0
        self.b._mask = str
        self.b._set_stage = Mock()
        self.b.runner = Mock()
        self.b.client = Mock(account_id='bot')
        self.b._awake.add('owner')
        self.b.journal = TaskJournal(self.root / '.runtime/tasks.sqlite')
        self.msg = self.message('model')
        self.ident, _ = self.b.journal.add('owner', 'model', 'task', '生成文件', payload='generate')
        self.msg._journal_id = self.ident
        self.msg._receipt_outcome = 'completed'
        self.path = self.root / 'report.txt'
        self.path.write_text('first version', encoding='utf-8')

    def message(self, mid='command', text='', uid='owner'):
        return Message({'from_user_id': uid, 'message_id': mid, 'context_token': 'current',
                        'item_list': [{'type': 1, 'text_item': {'text': text}}]})

    def replies(self):
        return '\n'.join(c.args[1] for c in self.b.reply.call_args_list)

    def generate(self, text=None):
        text = text if text is not None else ('已生成\n[[产物]]' + json.dumps(
            {'path': str(self.path), 'label': '结果文件'}, ensure_ascii=False))
        self.b.runner.run.return_value = RunnerResult(text)
        self.b._run_task(self.msg, '生成文件')
        self.b.journal.update(self.ident, self.msg._receipt_outcome,
                              outputs=getattr(self.msg, '_output_files', []))

    def get(self, selector='1', ident=None):
        msg = self.message(text='/getfile %s %s' % (ident or self.ident, selector))
        self.assertTrue(self.b._artifact_command(msg, msg.text))
        return msg

    def test_marker_recorded_before_reply_and_without_upload(self):
        def reply(*args, **kwargs):
            self.assertEqual(self.b.journal.artifacts('owner', self.ident)['records'][0]['status'], 'valid')
        self.b.reply.side_effect = reply
        self.generate()
        self.b.client.send_file.assert_not_called()
        self.assertIn(str(self.path), self.msg._output_files)
        self.assertNotIn('[[产物]]', self.replies())
        self.assertIn('/getfile ' + self.ident, self.replies())

    def test_listing_owner_only_and_does_not_call_model(self):
        self.generate(); self.b.runner.reset_mock()
        self.b._artifact_command(self.message(uid='other'), '/artifacts ' + self.ident)
        self.assertIn('未找到自己的', self.replies())
        self.b.runner.run.assert_not_called()
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_retrieval_uploads_frozen_version_and_reports_original(self):
        self.generate(); self.b.reply.reset_mock(); self.b.runner.reset_mock()
        msg = self.get()
        item = self.b.queue.get_nowait()
        def upload(uid, snapshot, **options):
            self.assertNotEqual(str(self.path), snapshot)
            self.path.write_text('changed after snapshot', encoding='utf-8')
            self.assertEqual(Path(snapshot).read_text(encoding='utf-8'), 'first version')
            self.assertEqual(options['file_name'], 'report.txt')
            self.assertEqual(options['context_token'], 'current')
            return 'report.txt', 13
        self.b.client.send_file.side_effect = upload
        self.b._run_sendfile(item[0], item[2]); self.b.queue.task_done()
        self.assertIn(str(self.path), self.replies())
        self.assertNotIn('artifact-transfers', self.replies())
        self.assertEqual(list((self.root / '.runtime/artifact-transfers').rglob('*')), [])
        self.b.runner.run.assert_not_called()
        with self.b.journal.connect() as db:
            self.assertEqual(db.execute('SELECT kind FROM tasks WHERE id=?', (msg._journal_id,)).fetchone()[0], 'artifact_send')

    def test_changed_file_never_uploaded(self):
        self.generate(); msg = self.get(); item = self.b.queue.get_nowait()
        self.path.write_text('later version', encoding='utf-8')
        self.b._run_sendfile(item[0], item[2]); self.b.queue.task_done()
        self.b.client.send_file.assert_not_called()
        self.assertIn('已改变', self.replies())
        self.assertEqual(msg._receipt_outcome, 'failed')

    def test_deleted_file_is_not_recreated_or_uploaded(self):
        self.generate(); self.path.unlink(); self.b.runner.reset_mock()
        self.get()
        self.assertIn('找不到', self.replies())
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()

    def test_missing_declared_file_does_not_repeat_model_success(self):
        self.path.unlink(); self.generate()
        self.assertEqual(self.msg._receipt_outcome, 'failed')
        self.assertNotIn('已生成\n', self.replies())
        self.assertIn('尚未生成', self.replies())
        self.assertEqual(self.b.journal.artifacts('owner', self.ident)['records'][0]['status'], 'missing')

    def test_ordinary_prose_path_is_not_an_artifact(self):
        self.generate('输入资料在 ' + str(self.path))
        self.assertFalse(self.b.journal.artifacts('owner', self.ident)['records'])

    def test_failure_markers_are_stripped_without_file_inspection(self):
        self.b.runner.run.return_value = RunnerResult('bad\n[[产物]]{"path":"bad"}', 'failed')
        with patch('bridge_artifacts.collect_artifacts') as collect:
            self.b._run_task(self.msg, 'generate')
            collect.assert_not_called()
        self.assertNotIn('[[产物]]', self.replies())
        self.b.client.send_file.assert_not_called()

    def test_catalog_write_failure_explained_without_fake_retrieval(self):
        with patch.object(self.b.journal, 'save_artifacts', side_effect=OSError('locked')):
            self.generate()
        self.assertIn('产物清单未保存', self.replies())
        self.assertNotIn('/getfile', self.replies())
        self.assertFalse(self.b.journal.artifacts('owner', self.ident)['records'])

    def test_latest_empty_task_does_not_return_older_outputs(self):
        self.generate()
        ident, _ = self.b.journal.add('owner', 'new', 'task', 'plain answer')
        self.b.journal.update(ident, 'completed')
        catalog = self.b.journal.artifacts('owner')
        self.assertEqual(catalog['id'], ident)
        self.assertEqual(catalog['records'], [])

    def test_fetch_receipt_does_not_replace_latest_model_task(self):
        self.generate(); msg = self.get()
        self.b.journal.update(msg._journal_id, 'completed')
        self.assertEqual(self.b.journal.artifacts('owner')['id'], self.ident)

    def test_disabled_history_and_transport_explained(self):
        self.generate(); self.b.send_file_enable = False
        self.get(); self.assertIn('发文件功能未启用', self.replies())
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.journal = None
        self.b._artifact_command(self.message(), '/artifacts latest')
        self.assertIn('任务记录未启用', self.replies())

    def test_new_catalog_survives_restart_and_history_is_read_only(self):
        self.generate()
        self.b.journal = TaskJournal(self.b.journal.path)
        self.assertEqual(self.b.journal.artifacts('owner')['records'][0]['sha256'], self.msg._artifacts[0]['sha256'])
        before = Path(self.b.journal.path).read_bytes()
        rows = read_history(self.root)
        self.assertEqual(rows[0]['artifacts'][0]['name'], 'report.txt')
        self.assertEqual(before, Path(self.b.journal.path).read_bytes())

    def test_malformed_selector_never_enqueues_business(self):
        self.generate(); self.b.runner.reset_mock()
        for command in ('/getfile', '/getfile ' + self.ident + ' 0', '/getfile ' + self.ident + ' 1 extra', '/artifacts latest extra'):
            self.assertTrue(self.b._artifact_command(self.message(), command))
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()

    def test_standby_retrieval_requires_wake_but_listing_is_readable(self):
        self.generate(); self.b.runner.reset_mock(); self.b.reply.reset_mock()
        self.b._awake.clear()
        self.b.handle(self.message('retrieve', '把刚才生成的文件发给我'))
        self.assertFalse(self.b._awake)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertIn('先说“开始对话”', self.replies())
        self.b.handle(self.message('list', '查看刚才的产物'))
        self.assertIn('任务产物', self.replies())
        self.b.runner.run.assert_not_called()

    def test_draining_queue_management_reaches_control_channel(self):
        self.b._draining.add('owner')
        self.b._dispatch_free = Bridge._dispatch_free.__get__(self.b)
        self.b._try_admin = Bridge._try_admin.__get__(self.b)
        msg = self.message('pending'); self.b._enqueue((msg, 'old', None), msg)
        command = '/queue edit %s replacement' % msg._journal_id
        self.b.handle(self.message('edit', command))
        self.assertEqual(self.b.queue.queue[0][1], 'replacement')
        self.assertIn('已修改', self.replies())
        self.assertIn('owner', self.b._draining)

    def test_multiple_artifacts_need_selector_and_respect_limit(self):
        paths = [self.root / ('part%d.txt' % n) for n in range(4)]
        for path in paths: path.write_text('data')
        self.generate('\n'.join('[[产物]]' + json.dumps({'path':str(path)}) for path in paths))
        self.b._artifact_command(self.message(), '/getfile ' + self.ident)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.get('all'); self.assertIn('超过单次', self.replies())
        self.assertEqual(self.b.queue.qsize(), 0)
        self.get('2'); self.assertEqual(self.b.queue.queue[0][2]['sendfile'], [str(paths[1])])

    def test_upload_uncertain_is_not_reported_as_accepted(self):
        self.generate(); self.b.reply.reset_mock()
        self.get(); item = self.b.queue.get_nowait()
        self.b.client.send_file.side_effect = ILinkError('lost receipt')
        self.b._run_sendfile(item[0], item[2]); self.b.queue.task_done()
        self.assertIn('未获得发送成功确认', self.replies())
        self.assertNotIn('接口已接受', self.replies())
        self.assertEqual(list((self.root / '.runtime/artifact-transfers').rglob('*')), [])

    def test_workdir_outside_assistant_does_not_break_trusted_snapshot(self):
        with tempfile.TemporaryDirectory() as work:
            self.path = Path(work) / 'outside.txt'
            self.path.write_text('outside data')
            self.b.send_roots = [work]
            self.b.workdir = work
            self.generate(); self.get()
            item = self.b.queue.get_nowait()
            self.b.client.send_file.return_value = ('outside.txt', 12)
            self.b._run_sendfile(item[0], item[2]); self.b.queue.task_done()
            self.b.client.send_file.assert_called_once()
            snapshot = self.b.client.send_file.call_args.args[1]
            self.assertFalse(Path(snapshot).exists())

    def test_invalid_marker_can_be_listed_with_missing_size(self):
        self.generate('[[产物]]not-json')
        self.assertEqual(self.msg._receipt_outcome, 'failed')
        self.b._artifact_command(self.message(), '/artifacts ' + self.ident)
        self.assertIn('产物声明格式无效', self.replies())

    @unittest.skipUnless(os.name == 'nt', 'Windows share-lock regression')
    def test_accepted_upload_stays_successful_when_snapshot_cleanup_is_locked(self):
        from snapshot_lifecycle import reclaim_snapshots
        self.generate()
        msg = self.get()
        item = self.b.queue.get_nowait()
        held, snapshots = [], []
        def accepted(uid, snapshot, **options):
            snapshots.append(Path(snapshot))
            held.append(open(snapshot, 'rb'))
            return options['file_name'], snapshots[-1].stat().st_size
        self.b.client.send_file.side_effect = accepted
        try:
            self.b._run_sendfile(item[0], item[2])
            self.assertEqual(msg._receipt_outcome, 'completed')
            self.assertEqual(msg._execution_state, 'completed')
            self.assertIn('微信接口已接受', self.replies())
            self.assertNotIn('读不出来', self.replies())
            self.assertNotIn('artifact-send-', self.replies())
            self.assertTrue(snapshots[0].exists())
        finally:
            for stream in held:
                stream.close()
            self.b.queue.task_done()
        result = reclaim_snapshots(self.root / '.runtime/snapshot-lifecycle', allowed_roots=[self.root])
        self.assertEqual(result['removed'], 1)
        self.assertFalse(snapshots[0].exists())

    def test_overflow_receipt_survives_persistence(self):
        paths = [self.root / ('file%d.txt' % n) for n in range(21)]
        for path in paths: path.write_text('data')
        self.generate('\n'.join('[[产物]]' + json.dumps({'path':str(path)}) for path in paths))
        records = self.b.journal.artifacts('owner', self.ident)['records']
        self.assertEqual(len(records), 21)
        self.assertEqual(records[-1]['status'], 'rejected')
        self.b._artifact_command(self.message(), '/artifacts ' + self.ident)
        self.assertIn('超过本次 20 个', self.replies())


if __name__ == '__main__':
    unittest.main()
