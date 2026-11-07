"""Copy reply command lines into real offline handlers; no live transport."""
import json
import threading
import unittest
from unittest.mock import patch

from tests import selftest_artifact_integration as artifact_fixture
from tests import selftest_batch_integration as batch_fixture
from tests import selftest_queue_management as queue_fixture
from tests import selftest_template_integration as template_fixture
from bridge_admin import AdminMixin
from ilink_client import ILinkClient, ILinkError, split_text
from runner_result import RunnerResult
from wechat_bridge import Bridge


class CommandGuidanceTests(unittest.TestCase):
    def fixture(self, case):
        fixture = case()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def command(self, text, prefix):
        commands = [line for line in text.splitlines() if line.startswith(prefix)]
        self.assertEqual(len(commands), 1, text)
        return commands[0]

    def test_queue_cancel_line_operates_on_named_owner_task_only(self):
        f = self.fixture(queue_fixture.QueueManagementTests)
        mine = f.enqueue('my request')
        other = f.enqueue('foreign private body', uid='other')
        text = f.b._admin_queue('owner')
        command = self.command(text, '/queue cancel ')
        self.assertEqual(command, '/queue cancel ' + mine._journal_id)
        self.assertNotIn(other._journal_id, text)
        self.assertNotIn('foreign private body', text)
        self.assertTrue(f.b._try_admin('owner', command).startswith('✅'))
        self.assertEqual(f.receipt(mine)[0], 'cancelled')
        self.assertEqual(f.receipt(other)[0], 'queued')

    def test_batch_list_show_and_run_guidance_preserves_required_body(self):
        f = self.fixture(batch_fixture.BatchIntegrationTests)
        own = f.ready()
        foreign = f.b.batch_store.create('other', 'private batch')
        f.b._batch_command(f.message(), '/batch list')
        listing = f.replies()
        self.assertNotIn(foreign['id'], listing)
        show = self.command(listing, '/batch show ')
        self.assertEqual(show, '/batch show ' + own['id'])
        f.b.reply.reset_mock()
        f.b._batch_command(f.message(), show)
        self.assertIn('在末尾补充任务说明后发送', f.replies())
        run = self.command(f.replies(), '/batch run ')
        f.b._batch_command(f.message(), run)
        self.assertEqual(f.b.queue.qsize(), 0)
        f.b.runner.run.assert_not_called()
        f.b.reply.reset_mock()
        msg = f.message()
        f.b._batch_command(msg, run + ' 核对金额')
        self.assertEqual(f.b.queue.qsize(), 1)
        self.assertEqual(f.b.queue.queue[0][1], '核对金额')
        cancel = self.command(f.replies(), '/queue cancel ')
        self.assertEqual(cancel, '/queue cancel ' + msg._journal_id)
        self.assertEqual(self.command(f.replies(), '/result '), '/result ' + msg._journal_id)
        self.assertTrue(AdminMixin._try_admin(f.b, 'owner', cancel).startswith('✅'))
        self.assertTrue(f.b.queue.empty())

    def test_used_batch_cancel_reply_uses_task_id_and_hides_foreign_task(self):
        f = self.fixture(batch_fixture.BatchIntegrationTests)
        own = f.ready()
        msg = f.run_command(own)
        f.b.reply.reset_mock()
        f.b._batch_command(f.message(), '/batch cancel ' + own['id'])
        self.assertEqual(self.command(f.replies(), '/queue cancel '), '/queue cancel ' + msg._journal_id)
        f.b.reply.reset_mock()
        f.b._batch_command(f.message(uid='other'), '/batch cancel ' + own['id'])
        self.assertNotIn(msg._journal_id, f.replies())
        self.assertNotIn('/queue cancel ', f.replies())
        self.assertEqual(f.b.queue.qsize(), 1)

    def test_template_list_show_and_receipt_commands_are_executable(self):
        f = self.fixture(template_fixture.TemplateIntegrationTests)
        f.saved()
        f.saved('私密模板', uid='other')
        f.command('/template list')
        text = f.replies()
        self.assertNotIn('私密模板', text)
        show = self.command(text, '/template show ')
        f.b.reply.reset_mock()
        f.command(show)
        self.assertIn('替换后发送', f.replies())
        run = self.command(f.replies(), '/template run ')
        self.assertEqual(run, '/template run 周报')
        f.b.reply.reset_mock()
        msg = f.command(run)
        self.assertEqual(f.b.queue.qsize(), 1)
        self.assertEqual(self.command(f.replies(), '/result '), '/result ' + msg._journal_id)
        cancel = self.command(f.replies(), '/queue cancel ')
        self.assertTrue(AdminMixin._try_admin(f.b, 'owner', cancel).startswith('✅'))
        self.assertTrue(f.b.queue.empty())

    def test_artifact_command_keeps_original_index_after_invalid_file(self):
        f = self.fixture(artifact_fixture.ArtifactIntegrationTests)
        records = [{'path': str(f.root / 'missing.txt'), 'label': '未生成'},
                   {'path': str(f.path), 'label': '可取回'}]
        f.generate('\n'.join('[[产物]]' + json.dumps(r, ensure_ascii=False) for r in records))
        command = self.command(f.replies(), '/getfile ')
        self.assertEqual(command, '/getfile ' + f.ident + ' 2')
        f.b.runner.reset_mock()
        f.b._artifact_command(f.message(), command)
        self.assertEqual(f.b.queue.qsize(), 1)
        self.assertEqual(f.b.queue.queue[0][2]['sendfile'], [str(f.path)])
        f.b.runner.run.assert_not_called()
        f.b.client.send_file.assert_not_called()

    def test_unsaved_multiple_artifacts_never_offer_retrieval_commands(self):
        f = self.fixture(artifact_fixture.ArtifactIntegrationTests)
        second = f.root / 'second.txt'
        second.write_text('second file', encoding='utf-8')
        text = '\n'.join('[[产物]]' + json.dumps({'path': str(path)}) for path in (f.path, second))
        with patch.object(f.b.journal, 'save_artifacts', side_effect=OSError('locked')):
            f.generate(text)
        self.assertIn('产物清单未保存', f.replies())
        self.assertNotIn('/getfile', f.replies())
        self.assertEqual(f.b.queue.qsize(), 0)
        f.b.client.send_file.assert_not_called()

    def completed_fixture(self, kind='ordinary'):
        case = template_fixture.TemplateIntegrationTests if kind == 'template' else batch_fixture.BatchIntegrationTests
        f = self.fixture(case)
        if kind == 'template':
            f.saved()
            msg, body, job = f.queued()
            execute = lambda: f.b._run_template(msg, body, job)
        elif kind == 'batch':
            msg, body, manifest = f.queued()
            execute = lambda: f.b._run_batch(msg, body, manifest)
        else:
            msg = f.message()
            msg._journal_id, _ = f.b.journal.add('owner', msg.message_id, 'task', 'plain task')
            msg._receipt_outcome = 'completed'
            execute = lambda: f.b._run_task(msg, 'plain task')
        f.b.journal.update(msg._journal_id, 'running')
        f.b.reply = Bridge.reply.__get__(f.b)
        f.b._worker = threading.current_thread()
        f.b.runner.run.return_value = RunnerResult('已完成汇总，无需生成文件。')
        return f, msg, execute

    @staticmethod
    def finish_receipt(f, msg):
        f.b.journal.update(msg._journal_id, msg._receipt_outcome,
                           execution_state=msg._execution_state)
        f.b._worker = None

    def test_plain_batch_and_template_completion_result_commands_are_owner_bound(self):
        for kind in ('ordinary', 'batch', 'template'):
            with self.subTest(kind=kind):
                f, msg, execute = self.completed_fixture(kind)
                execute()
                sent = f.b.client.send_text.call_args.args[1]
                command = self.command(sent, '/result ')
                self.assertEqual(command, '/result ' + msg._journal_id)
                self.finish_receipt(f, msg)
                cached = f.b.journal.replies('owner', msg._journal_id)
                self.assertEqual([r['body'] for r in cached], [sent])
                f.b.runner.run.reset_mock()
                f.b._history_command(f.message(), command)
                self.assertIn(sent, f.b.client.send_text.call_args.args[1])
                f.b._history_command(f.message(uid='other'), command)
                self.assertNotIn('已完成汇总', f.b.client.send_text.call_args.args[1])
                self.assertNotIn(msg._journal_id, f.b.client.send_text.call_args.args[1])
                f.b.runner.run.assert_not_called()
                f.b.client.send_file.assert_not_called()
                self.assertTrue(f.b.queue.empty())

    def test_failed_or_declined_result_cache_never_advertises_result_command(self):
        for mode in ('error', 'declined'):
            with self.subTest(mode=mode):
                f, msg, execute = self.completed_fixture()
                options = {'side_effect': OSError('cache unavailable')} if mode == 'error' else {'return_value': None}
                with patch.object(f.b.journal, 'prepare_reply', **options):
                    execute()
                sent = f.b.client.send_text.call_args.args[1]
                self.assertIn('已完成汇总', sent)
                self.assertNotIn('/result ', sent)
                if mode == 'error':
                    self.assertIn('缓存未能保存', sent)
                self.assertEqual(msg._execution_state, 'completed')
                self.finish_receipt(f, msg)
                self.assertEqual(f.b.journal.replies('owner', msg._journal_id), [])

    def test_completion_without_journal_does_not_invent_result_command(self):
        f, msg, execute = self.completed_fixture()
        f.b.journal = None
        execute()
        self.assertEqual(f.b.client.send_text.call_args.args[1], '已完成汇总，无需生成文件。')
        self.assertEqual(msg._execution_state, 'completed')

    def test_result_command_survives_partial_delivery_and_resend_without_reexecution(self):
        f, msg, execute = self.completed_fixture()
        client = ILinkClient('https://example.invalid', '', '', state_dir=str(f.root), max_reply_chars=120)
        f.b.client = client
        f.b.runner.run.return_value = RunnerResult('分片结果正文 ' * 100)
        sent = []

        def first_send(uid, text, context):
            sent.append(text)
            if len(sent) == 2:
                raise ILinkError('second chunk receipt lost')

        client.send_chunk = first_send
        with patch('ilink_client.time.sleep'), self.assertLogs('bridge', level='ERROR'):
            execute()
        self.finish_receipt(f, msg)
        cached = f.b.journal.replies('owner', msg._journal_id, only_pending=True)[0]
        self.assertEqual(cached['accepted_chunks'], 1)
        self.assertEqual(self.command(cached['body'], '/result '), '/result ' + msg._journal_id)
        client.max_reply_chars = 500
        resent = []
        client.send_chunk = lambda uid, text, context: resent.append(text)
        f.b.runner.run.reset_mock()
        with patch('ilink_client.time.sleep'):
            f.b._history_command(f.message(), '/resend ' + msg._journal_id)
        data = [text for text in resent if text.startswith('（')]
        self.assertEqual(data[0], sent[1])
        self.assertEqual([text.split('\n', 1)[1] for text in data], split_text(cached['body'], 120)[1:])
        self.assertIn('/result ' + msg._journal_id, '\n'.join(data))
        self.assertEqual(f.b.journal.replies('owner', msg._journal_id, only_pending=True), [])
        f.b.runner.run.assert_not_called()
        self.assertTrue(f.b.queue.empty())

    def test_deferred_completion_keeps_result_command_option_and_cached_text(self):
        f, msg, execute = self.completed_fixture()
        with f.b._reply_scope():
            execute()
            f.b.client.send_text.assert_not_called()
        sent = f.b.client.send_text.call_args.args[1]
        self.assertEqual(self.command(sent, '/result '), '/result ' + msg._journal_id)
        self.finish_receipt(f, msg)
        self.assertEqual(f.b.journal.replies('owner', msg._journal_id)[0]['body'], sent)


if __name__ == '__main__':
    unittest.main()
