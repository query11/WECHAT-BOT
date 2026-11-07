"""Template integration with real Message/SQLite/queue/files, no live transport."""
import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import unittest
from unittest.mock import patch

from tests import selftest_batch_integration as batch_fixture
from bridge_templates import parse_template_definition, template_output_dir
from runner_result import RunnerResult
from task_control import TaskQueue
from task_journal import TaskJournal
from task_templates import TaskTemplateStore, TemplateError


class TemplateIntegrationTests(unittest.TestCase):
    # Reuse only fixture helpers: inheriting the TestCase would rerun all batch tests.
    message = batch_fixture.BatchIntegrationTests.message
    entry = batch_fixture.BatchIntegrationTests.entry
    ready = batch_fixture.BatchIntegrationTests.ready
    replies = batch_fixture.BatchIntegrationTests.replies

    def setUp(self):
        batch_fixture.BatchIntegrationTests.setUp(self)
        self.b.template_store = TaskTemplateStore(self.b.journal.path)
        self.definition = dict(inputs='需要已确认的报表及用户提供的统计范围',
                               steps='核对数据\n逐项汇总，并标出缺失数据',
                               output_dir='工作目录/结果', output_format='TXT 汇总与差异说明')

    def saved(self, name='周报', uid='owner', **changes):
        return self.b.template_store.save(uid, name, dict(self.definition, **changes))

    def expected_execution_snapshot(self, template):
        # The saved definition stays reusable; accepted work pins its directory.
        self.assertEqual(template['definition']['output_dir'], '工作目录/结果')
        expected = copy.deepcopy(template)
        expected['definition']['output_dir'] = str(self.workdir / '结果')
        return expected

    def command(self, text, uid='owner', attachments=False, through_handle=False, mid=None):
        items = ([dict(text_item=dict(text=text)), dict(file_item=dict(file_name='new.txt'))]
                 if attachments else None)
        msg = self.message(text, uid, items=items, mid=mid)
        if through_handle:
            self.b.handle(msg)
        else:
            self.assertTrue(self.b._template_command(msg, msg.text))
        return msg

    def queued(self, command='/template run 周报 本次按日期核对'):
        msg = self.command(command)
        self.assertEqual(self.b.queue.qsize(), 1, self.replies())
        item = self.b.queue.get_nowait()
        self.b.queue.task_done()
        msg._receipt_outcome = 'completed'
        return msg, item[1], item[2]

    def receipt(self, msg):
        with self.b.journal.connect(read_only=True) as db:
            row = db.execute('SELECT kind,state,payload,inputs,template FROM tasks WHERE id=?',
                             (msg._journal_id,)).fetchone()
        return dict(zip(('kind', 'state', 'payload', 'inputs', 'template'), row))

    def worker_once(self):
        calls, failures = [], []
        original = self.b._take_task

        class Finished(Exception):
            pass

        def take_one():
            if calls:
                raise Finished()
            calls.append(True)
            return original()

        def target():
            try:
                self.b._loop_worker()
            except Finished:
                pass
            except BaseException as exc:
                failures.append(exc)

        with patch.object(self.b, '_take_task', side_effect=take_one):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            thread.join(3)
        self.assertFalse(thread.is_alive(), 'template worker did not finish')
        self.assertEqual(failures, [])
        self.assertEqual(self.b.queue.unfinished_tasks, 0)

    def test_save_four_fields_multiline_is_text_only_and_no_directory_created(self):
        msg = self.command('/template save 周报\n'
                           '输入：已确认报表\n步骤：先核对金额\n再汇总日期\n'
                           '输出目录：工作目录/结果\n输出格式：TXT 和差异清单', through_handle=True)
        template = self.b.template_store.get('owner', '周报')
        self.assertEqual(template['definition'], dict(inputs='已确认报表', steps='先核对金额\n再汇总日期',
                                                     output_dir='工作目录/结果', output_format='TXT 和差异清单'))
        self.assertFalse(hasattr(msg, '_journal_id'))
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertFalse((self.workdir / '结果').exists())
        self.b.runner.run.assert_not_called()
        self.b.client.send_file.assert_not_called()

    def test_duplicate_missing_and_unlabeled_template_fields_cannot_update(self):
        original = self.saved()
        definitions = ('输入：A\n输入要求：B\n步骤：S\n输出目录：.\n输出格式：TXT',
                       '输入：A\n步骤：S\n输出格式：TXT',
                       '这是未标记的要求\n输入：A\n步骤：S\n输出目录：.\n输出格式：TXT',
                       '输入：A\n步骤：\n输出目录：.\n输出格式：TXT')
        for body in definitions:
            with self.subTest(body=body):
                self.command('/template save 周报\n' + body)
                self.assertEqual(self.b.template_store.get('owner', '周报'), original)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()

    def test_parser_supports_chinese_and_ascii_colons_without_guessing_missing_fields(self):
        definition = parse_template_definition('输入要求: A\n处理步骤：第一步\n 第二步\n'
                                                '输出目录: .\n输出格式：Excel')
        self.assertEqual(definition['steps'], '第一步\n 第二步')
        self.assertEqual(definition['inputs'], 'A')
        with self.assertRaises(TemplateError):
            parse_template_definition('输入: A\n处理步骤: s\n输出目录: .')

    def test_standby_allows_queries_but_blocks_save_delete_run_and_natural_execution(self):
        original = self.saved()
        self.b._awake.discard('owner')
        for text in ('/template help', '/template list', '/template show 周报'):
            self.command(text, through_handle=True)
        self.assertIn('周报', self.replies())
        for text in ('/template delete 周报', '/template run 周报', '按周报模板运行',
                     '/template save 新模板\n输入：a\n步骤：s\n输出目录：.\n输出格式：TXT'):
            self.command(text, through_handle=True)
        self.assertEqual(self.b.template_store.get('owner', '周报'), original)
        self.assertIsNone(self.b.template_store.get('owner', '新模板'))
        self.assertEqual(self.b._awake, {'other'})
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertIn('开始对话', self.replies())
        self.b.runner.run.assert_not_called()

    def test_draining_blocks_template_execution(self):
        self.saved()
        self.b._draining.add('owner')
        self.command('/template run 周报', through_handle=True)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertIn('收尾', self.replies())

    def test_attachment_bearing_template_commands_are_not_downloaded_or_queued(self):
        original = self.saved()
        for text in ('/template run 周报', '/template delete 周报', '/template list', '按周报模板处理这份表'):
            self.command(text, attachments=True, through_handle=True)
        self.assertEqual(self.b._inbox_q.qsize(), 0)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(self.b.template_store.get('owner', '周报'), original)
        self.assertIn('附件未接收', self.replies())
        self.b.runner.run.assert_not_called()

    def test_plain_template_run_omits_recent_inbox_context(self):
        snapshot = self.saved()
        self.entry('recent-should-not-enter.txt', b'unrelated')
        msg, body, job = self.queued()
        self.assertEqual(job['template'], self.expected_execution_snapshot(snapshot))
        self.assertEqual(self.b.template_store.get('owner', '周报'), snapshot)
        self.assertNotIn('batch', job)
        self.b.runner.run.return_value = RunnerResult('处理结束')
        self.b._run_template(msg, body, job)
        prompt = self.b.runner.run.call_args.args[1]
        self.assertIn('本次没有附件清单', prompt)
        self.assertIn('接收时固定的版本', prompt)
        self.assertIn('本次按日期核对', prompt)
        definition = json.loads(next(line for line in prompt.splitlines() if line.startswith('{"名称":')))
        self.assertEqual(definition['输出目录'], str(self.workdir / '结果'))
        self.assertNotIn('recent-should-not-enter.txt', prompt)
        self.assertNotIn('OLD RECENT', prompt)
        self.b._recent_inbox_context.assert_not_called()

    def test_template_batch_freezes_declared_files_and_isolates_later_arrivals(self):
        self.saved()
        batch = self.ready([self.entry('one.txt'), self.entry('two.txt', b'second input')])
        msg, body, job = self.queued('/template batch 周报 %s 按本周日期核对' % batch['id'])
        self.entry('later.txt', b'later unrelated upload')
        copied = []

        def run(uid, prompt):
            self.assertEqual(uid, 'owner')
            self.assertIn('固定输入清单', prompt)
            self.assertIn('按本周日期核对', prompt)
            self.assertNotIn('later.txt', prompt)
            copied.extend(Path(record['path']) for record in msg._batch_frozen)
            self.assertEqual([path.read_bytes() for path in copied], [b'first input', b'second input'])
            self.assertTrue(all(path.exists() for path in copied))
            return RunnerResult('模板批次处理结束')

        self.b.runner.run.side_effect = run
        self.b._run_template(msg, body, job)
        self.b._recent_inbox_context.assert_not_called()
        self.assertEqual([row['name'] for row in job['batch']['files']], ['one.txt', 'two.txt'])
        self.assertTrue(copied)
        self.assertTrue(all(not path.exists() for path in copied))
        self.assertFalse(list(self.workdir.glob('.wechat-batch-inputs-*')))

    def test_changed_batch_input_prevents_template_model_execution(self):
        self.saved()
        batch = self.ready()
        msg, body, job = self.queued('/template batch 周报 ' + batch['id'])
        (self.inbox / 'one.txt').write_bytes(b'changed after acceptance')
        self.b._run_template(msg, body, job)
        self.b.runner.run.assert_not_called()
        self.assertEqual(msg._receipt_outcome, 'failed')
        self.assertIn('批次未执行', self.replies())

    def test_natural_attachment_request_uses_only_single_owner_ready_batch(self):
        self.saved()
        own = self.ready()
        self.ready([self.entry('other.txt', uid='other')], uid='other')
        msg = self.command('按周报模板处理这份表 本次检查重复金额', through_handle=True)
        self.assertEqual(self.b.queue.qsize(), 1, self.replies())
        item = self.b.queue.queue[0]
        self.assertEqual(item[2]['batch']['id'], own['id'])
        self.assertEqual(item[1], '本次检查重复金额')
        self.assertEqual(self.b.batch_store.get('owner', own['id'])['task_id'], msg._journal_id)
        self.b.runner.run.assert_not_called()

    def test_natural_attachment_request_without_ready_batch_does_not_guess_recent_files(self):
        self.saved()
        self.entry('recent.txt', b'unconfirmed attachment')
        self.command('按周报模板处理这份表', through_handle=True)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertIn('先核对', self.replies())
        self.b._recent_inbox_context.assert_not_called()
        self.b.runner.run.assert_not_called()

    def test_multiple_ready_batches_require_explicit_id(self):
        self.saved()
        first = self.ready([self.entry('first.txt')])
        second = self.ready([self.entry('second.txt')])
        self.command('按周报模板处理这批附件', through_handle=True)
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(self.b.batch_store.get('owner', first['id'])['state'], 'ready')
        self.assertEqual(self.b.batch_store.get('owner', second['id'])['state'], 'ready')
        self.assertIn('明确指定', self.replies())
        self.command('/template batch 周报 ' + second['id'])
        self.assertEqual(self.b.queue.queue[0][2]['batch']['id'], second['id'])

    def test_natural_plain_run_does_not_select_ready_batch(self):
        self.saved()
        batch = self.ready()
        self.command('按周报模板运行 给出核对步骤', through_handle=True)
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertNotIn('batch', self.b.queue.queue[0][2])
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['state'], 'ready')

    def test_updated_deleted_template_cannot_change_accepted_snapshot(self):
        original = self.saved()
        msg = self.command('/template run 周报 本次说明')
        self.saved(steps='后来完全不同的步骤')
        self.b.template_store.delete('owner', '周报')
        self.assertEqual(self.b.queue.queue[0][2]['template'], self.expected_execution_snapshot(original))
        self.assertEqual(json.loads(self.receipt(msg)['template']), self.expected_execution_snapshot(original))
        self.assertEqual(self.receipt(msg)['kind'], 'template_task')

    def test_resume_retains_deleted_template_snapshot_without_recent_files(self):
        original = self.saved()
        msg, body, job = self.queued('/template run 周报 原始补充要求')
        self.b.template_store.delete('owner', '周报')
        self.b.journal = TaskJournal(self.b.journal.path)
        self.entry('newer.txt', b'later upload')
        recovery = self.message('/resume ' + msg._journal_id)
        self.assertIsNone(self.b._enqueue((recovery, '', None), recovery, recovery_id=msg._journal_id))
        queued = self.b.queue.queue[0]
        self.assertEqual(queued[1], '原始补充要求')
        self.assertEqual(queued[2], {'template': self.expected_execution_snapshot(original)})
        self.assertEqual(recovery._journal_id, msg._journal_id)
        self.b.runner.run.assert_not_called()

    def test_batch_template_resume_retains_both_version_and_input_manifest(self):
        original = self.saved()
        batch = self.ready()
        msg, body, job = self.queued('/template batch 周报 %s 原始补充' % batch['id'])
        frozen = copy.deepcopy(job)
        self.saved(steps='后来修改')
        self.b.template_store.delete('owner', '周报')
        self.entry('later.txt')
        self.b.journal = TaskJournal(self.b.journal.path)
        recovery = self.message('/resume ' + msg._journal_id)
        self.assertIsNone(self.b._enqueue((recovery, '', None), recovery, recovery_id=msg._journal_id))
        item = self.b.queue.queue[0]
        self.assertEqual(item[1], '原始补充')
        self.assertEqual(item[2], frozen)
        self.assertEqual(item[2]['template'], self.expected_execution_snapshot(original))

    def test_corrupt_recovery_snapshot_cannot_downgrade_to_plain_task(self):
        self.saved()
        for raw in ('[]', '{}', '{bad-json', json.dumps(dict(uid='other'))):
            with self.subTest(raw=raw):
                msg, _, _ = self.queued()
                self.b.journal = TaskJournal(self.b.journal.path)
                with self.b.journal.connect() as db:
                    db.execute('UPDATE tasks SET template=? WHERE id=?', (raw, msg._journal_id))
                recovery = self.message('/resume ' + msg._journal_id)
                self.b.handle(recovery)
                self.assertEqual(self.b.queue.qsize(), 0)
                self.assertEqual(self.receipt(msg)['state'], 'recoverable')
        self.b.runner.run.assert_not_called()

    def test_real_worker_routes_plain_and_batch_templates_and_persists_completion(self):
        self.saved()
        for with_batch in (False, True):
            with self.subTest(with_batch=with_batch):
                batch = self.ready() if with_batch else None
                msg = self.command('/template batch 周报 ' + batch['id'] if with_batch else '/template run 周报')
                self.b.runner.run.return_value = RunnerResult('已按指定模板处理')
                self.worker_once()
                self.assertTrue(msg._task_control.finished.is_set())
                self.assertEqual(self.receipt(msg)['state'], 'completed')
                prompt = self.b.runner.run.call_args.args[1]
                self.assertIn('接收时固定的版本', prompt)
                if with_batch:
                    self.assertIn('固定输入清单', prompt)
                self.assertNotIn('OLD RECENT', prompt)
        self.assertEqual(self.b.runner.run.call_count, 2)
        self.b.client.send_file.assert_not_called()

    def test_queue_full_before_acceptance_creates_no_template_receipt(self):
        self.saved()
        self.b.queue = TaskQueue(maxsize=1)
        self.b.queue_max = 1
        self.b.queue.put_nowait((self.message(), 'already queued', None))
        msg = self.command('/template run 周报')
        self.assertFalse(hasattr(msg, '_journal_id'))
        with self.b.journal.connect(read_only=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE kind='template_task'").fetchone()[0], 0)
        self.assertEqual(self.b.queue.qsize(), 1)
        self.b.runner.run.assert_not_called()

    def test_enqueue_race_cancels_plain_template_receipt(self):
        self.saved()
        with patch.object(self.b.queue, 'put_nowait', side_effect=queue.Full):
            msg = self.command('/template run 周报')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(self.receipt(msg)['state'], 'cancelled')
        self.b.runner.run.assert_not_called()

    def test_enqueue_race_releases_batch_and_cancels_template_receipt(self):
        self.saved()
        batch = self.ready()
        with patch.object(self.b.queue, 'put_nowait', side_effect=queue.Full):
            msg = self.command('/template batch 周报 ' + batch['id'])
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['state'], 'ready')
        self.assertEqual(self.receipt(msg)['state'], 'cancelled')
        self.b.runner.run.assert_not_called()

    def test_template_queue_edit_rejected_move_cancel_allowed(self):
        snapshot = self.saved()
        msg = self.command('/template run 周报 原始补充')
        self.assertIn('只有普通文字', self.b._admin_queue_manage('owner', 'edit', msg._journal_id, '替换要求'))
        self.assertEqual(self.b.queue.queue[0][1], '原始补充')
        self.assertEqual(json.loads(self.receipt(msg)['template']), self.expected_execution_snapshot(snapshot))
        self.assertIn('第 1 条', self.b._admin_queue_manage('owner', 'move', msg._journal_id, 1))
        self.assertIn('已取消', self.b._admin_queue_manage('owner', 'cancel', msg._journal_id, None))
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(json.loads(self.receipt(msg)['template']), self.expected_execution_snapshot(snapshot))

    def test_workdir_change_after_acceptance_cannot_retarget_template_output(self):
        snapshot = self.saved()
        msg, body, job = self.queued()
        accepted = self.expected_execution_snapshot(snapshot)
        moved = self.root / 'new-work'
        moved.mkdir()
        self.b.workdir = str(moved)
        self.b._run_template(msg, body, job)
        self.b.runner.run.assert_not_called()
        self.assertEqual(msg._receipt_outcome, 'failed')
        self.assertIn('模板输出目录超出当前工作目录', self.replies())
        self.assertEqual(job['template'], accepted)
        self.assertEqual(json.loads(self.receipt(msg)['template']), accepted)
        self.assertFalse((moved / '结果').exists())

    def test_invalid_output_locations_rejected_before_receipt_or_model(self):
        file_path = self.workdir / 'already-file.txt'
        file_path.write_text('not a directory', encoding='utf8')
        outside = self.root / 'outside'
        outside.mkdir()
        bad = ('../outside', str(outside), str(file_path), 'C:relative', 'result*',
               '工作目录/.wechat-batch-inputs-private/result')
        for index, output_dir in enumerate(bad):
            with self.subTest(output_dir=output_dir):
                self.saved(name='模板%d' % index, output_dir=output_dir)
                msg = self.command('/template run 模板%d' % index)
                self.assertFalse(hasattr(msg, '_journal_id'))
                self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()

    def test_output_dir_resolver_is_read_only_and_accepts_inside_relative_or_absolute(self):
        for raw in ('工作目录/结果', '工作目录\\结果', './结果', str(self.workdir / '结果')):
            self.assertEqual(template_output_dir(raw, str(self.workdir)), str(self.workdir / '结果'))
        self.assertEqual(template_output_dir('工作目录', str(self.workdir)), str(self.workdir))
        self.assertFalse((self.workdir / '结果').exists())

    def test_output_directory_becoming_file_before_execution_blocks_model(self):
        self.saved(output_dir='result')
        msg, body, job = self.queued()
        (self.workdir / 'result').write_text('became a file', encoding='utf8')
        self.b._run_template(msg, body, job)
        self.b.runner.run.assert_not_called()
        self.assertEqual(msg._receipt_outcome, 'failed')
        self.assertIn('模板任务未执行', self.replies())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_output_directory_replaced_by_external_junction_blocks_model(self):
        self.saved(output_dir='result')
        target = self.workdir / 'result'
        target.mkdir()
        msg, body, job = self.queued()
        target.rmdir()
        outside = self.root / 'outside'
        outside.mkdir()
        created = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(target), str(outside)],
                                 capture_output=True, text=True, errors='replace')
        if created.returncode:
            self.skipTest('junction creation unavailable')
        try:
            self.b._run_template(msg, body, job)
            self.b.runner.run.assert_not_called()
            self.assertEqual(msg._receipt_outcome, 'failed')
            self.assertIn('超出当前工作目录', self.replies())
        finally:
            os.rmdir(target)  # Remove only the junction, never recurse into its target.
        self.assertTrue(outside.is_dir())

    def test_template_and_batch_owner_isolation_at_command_boundary(self):
        self.saved()
        batch = self.ready()
        self.command('/template show 周报', uid='other')
        self.command('/template delete 周报', uid='other')
        self.command('/template run 周报', uid='other')
        self.assertIsNotNone(self.b.template_store.get('owner', '周报'))
        self.saved(uid='other')
        self.command('/template batch 周报 ' + batch['id'], uid='other')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertEqual(self.b.batch_store.get('owner', batch['id'])['state'], 'ready')
        self.b.runner.run.assert_not_called()

    def test_unlisted_sender_cannot_query_or_execute_template(self):
        self.saved()
        self.command('/template list', uid='stranger', through_handle=True)
        self.command('/template run 周报', uid='stranger', through_handle=True)
        self.b.reply.assert_not_called()
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_template_production_result_registers_declared_artifact_and_hides_marker(self):
        self.saved()
        msg = self.command('/template run 周报')
        result = self.workdir / '结果' / '汇总.txt'

        def run(uid, prompt):
            result.parent.mkdir()
            result.write_text('isolated template output', encoding='utf8')
            return RunnerResult('已完成\n[[产物]]' + json.dumps(dict(path=str(result), label='汇总'), ensure_ascii=False))

        self.b.runner.run.side_effect = run
        self.worker_once()
        self.assertEqual(self.receipt(msg)['state'], 'completed')
        catalog = self.b.journal.artifacts('owner', msg._journal_id)
        self.assertEqual(len(catalog['records']), 1)
        self.assertEqual(catalog['records'][0]['status'], 'valid')
        self.assertEqual(catalog['records'][0]['path'], str(result))
        self.assertNotIn('[[产物]]', self.replies())
        self.assertIn('汇总', self.replies())
        self.b.client.send_file.assert_not_called()

    def test_disabled_template_recording_fails_closed(self):
        self.b.template_store = None
        self.command('/template run 周报')
        self.assertIn('未启用', self.replies())
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
