"""Optional result checks through real template/queue/SQLite/files, offline only."""
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from tests import selftest_template_integration as template_fixture
from bridge_templates import parse_template_definition
from claude_runner import TaskCancelled
from ilink_client import ILinkClient, ILinkError, split_text
from runner_result import RunnerResult
from task_control import TaskControl
from task_history import read_history
from task_journal import TaskJournal
from task_templates import TemplateError
from wechat_bridge import Bridge


class ResultChecksIntegrationTests(unittest.TestCase):
    # Share setup helpers, not the TestCase inheritance that reruns old tests.
    setUp = template_fixture.TemplateIntegrationTests.setUp
    message = template_fixture.TemplateIntegrationTests.message
    entry = template_fixture.TemplateIntegrationTests.entry
    ready = template_fixture.TemplateIntegrationTests.ready
    saved = template_fixture.TemplateIntegrationTests.saved
    command = template_fixture.TemplateIntegrationTests.command
    queued = template_fixture.TemplateIntegrationTests.queued
    receipt = template_fixture.TemplateIntegrationTests.receipt
    expected_execution_snapshot = template_fixture.TemplateIntegrationTests.expected_execution_snapshot

    COLUMNS = '必需列 | 汇总.csv | - | 项目,金额'
    TOTAL = '列合计 | 汇总.csv | - | 金额 | 40'
    SECTIONS = '必需章节 | 周报.md | 进展,问题,下周计划'
    CSV = '项目,金额\n交通,10\n餐费,30\n'
    REPORT = '# 周报\n\n## 进展\n已经整理。\n\n## 问题\n暂无。\n\n## 下周计划\n复核结果。\n'

    def replies(self):
        # Before worker execution commands use the original Mock reply; then
        # production reply records and sends through the fake transport.
        if hasattr(self.b.reply, 'call_args_list'):
            return template_fixture.TemplateIntegrationTests.replies(self)
        return '\n'.join(str(call.args[1]) for call in self.b.client.send_text.call_args_list)

    def worker_once(self):
        self.b.reply = Bridge.reply.__get__(self.b)
        try:
            template_fixture.TemplateIntegrationTests.worker_once(self)
        finally:
            self.b._worker = None

    def generated(self, files, *, prose='已完成文件生成。', declare=True, after_write=None):
        paths = []

        def run(uid, prompt):
            self.b._worker = threading.current_thread()
            self.assertEqual(uid, 'owner')
            lines = [prose]
            for name, data in files.items():
                path = self.workdir / '结果' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data if isinstance(data, bytes) else data.encode('utf8'))
                paths.append(path)
                if declare:
                    lines.append('[[产物]]' + json.dumps(dict(path=str(path), label=path.name), ensure_ascii=False))
            if after_write is not None:
                after_write(paths)
            return RunnerResult('\n'.join(lines))

        self.b.runner.run.side_effect = run
        return paths

    def run_checked(self, checks, files, *, batch=False, **kwargs):
        self.saved(checks=checks)
        if batch:
            ready = self.ready()
            msg = self.command('/template batch 周报 ' + ready['id'])
        else:
            msg = self.command('/template run 周报')
        self.generated(files, **kwargs)
        self.worker_once()
        return msg

    def assert_completed(self, msg):
        self.assertEqual(self.receipt(msg)['state'], 'completed', self.replies())
        self.assertEqual(msg._execution_state, 'completed')
        self.assertTrue(msg._task_control.finished.is_set())
        self.assertTrue(self.b.queue.empty())
        self.b.client.send_file.assert_not_called()

    def report(self, msg):
        report = msg._result_checks
        self.assertIsInstance(report, list)
        for row in report:
            self.assertTrue({'file', 'kind', 'status', 'detail'} <= row.keys(), row)
            self.assertIn(row['status'], ('passed', 'failed', 'unverified'))
            self.assertTrue(row['detail'])
        # Unmatched-file checks also survive the reply cache's retention
        # window because the complete report belongs to the task receipt.
        catalog = self.b.journal.artifacts('owner', msg._journal_id)
        self.assertEqual(catalog['result_checks'], report)
        return report

    def cached(self, msg):
        records = self.b.journal.replies('owner', msg._journal_id)
        self.assertIsNotNone(records)
        self.assertTrue(records, self.replies())
        return '\n'.join(row['body'] for row in records)

    def test_save_optional_checks_through_command_and_show_with_no_execution(self):
        checks = '\n'.join((self.SECTIONS, self.COLUMNS, self.TOTAL))
        self.command('/template save 周报\n输入：已确认的报表\n步骤：核对后汇总\n'
                     '输出目录：工作目录/结果\n输出格式：CSV 和 Markdown\n检查条件：\n' + checks,
                     through_handle=True)
        template = self.b.template_store.get('owner', '周报')
        self.assertEqual(template['definition']['checks'], checks)
        self.command('/template show 周报')
        self.assertIn(checks, self.replies())
        self.assertFalse((self.workdir / '结果').exists())
        self.assertTrue(self.b.queue.empty())
        self.b.runner.run.assert_not_called()

    def test_invalid_check_syntax_cannot_overwrite_saved_template(self):
        original = self.saved(checks=self.COLUMNS)
        invalid = ('任意执行 | 汇总.csv | - | 项目',
                   '必需列 | 汇总.csv | -',
                   '列合计 | 汇总.csv | - | 金额 | 不知道',
                   '必需章节 | 周报.md | ',
                   '必需列 | ../汇总.csv | - | 项目')
        for checks in invalid:
            with self.subTest(checks=checks):
                definition = dict(self.definition, checks=checks)
                with self.assertRaises(TemplateError):
                    self.b.template_store.save('owner', '周报', definition)
                self.command('/template save 周报\n输入：输入\n步骤：步骤\n'
                             '输出目录：工作目录/结果\n输出格式：CSV\n检查条件：' + checks)
                self.assertEqual(self.b.template_store.get('owner', '周报'), original)
        self.assertTrue(self.b.queue.empty())
        self.b.runner.run.assert_not_called()

    def test_old_four_fields_and_empty_optional_field_keep_old_behavior(self):
        for suffix in ('', '\n检查条件：'):
            with self.subTest(suffix=suffix):
                definition = parse_template_definition('输入：材料\n步骤：核对\n'
                                                       '输出目录：工作目录/结果\n输出格式：CSV' + suffix)
                self.assertFalse(definition.get('checks'))
        self.saved()
        msg = self.command('/template run 周报')
        self.generated({'汇总.csv': self.CSV})
        self.worker_once()
        self.assert_completed(msg)
        self.assertFalse(getattr(msg, '_result_checks', []))
        self.assertNotIn('结果检查', self.cached(msg))
        self.assertEqual(len(self.b.journal.artifacts('owner', msg._journal_id)['records']), 1)

    def test_whitespace_only_checks_save_as_old_template_without_conditions(self):
        for checks in ('', '   ', '\t\n \r\n', '\u3000\n\u3000'):
            with self.subTest(checks=repr(checks)):
                saved = self.saved(checks=checks)
                self.assertEqual(saved['definition'], self.definition)
                self.assertEqual(self.b.template_store.get('owner', '周报')['definition'], self.definition)
        self.assertTrue(self.b.queue.empty())
        self.b.runner.run.assert_not_called()

    def test_worker_checks_sections_columns_and_total_then_persists_each_file_report(self):
        checks = '\n'.join((self.SECTIONS, self.COLUMNS, self.TOTAL))
        msg = self.run_checked(checks, {'汇总.csv': self.CSV, '周报.md': self.REPORT})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['passed'] * 3)
        self.assertIn(checks, self.b.runner.run.call_args.args[1].replace('\\n', '\n'))
        catalog = self.b.journal.artifacts('owner', msg._journal_id)
        by_name = {row['name']: row for row in catalog['records']}
        self.assertEqual(len(by_name['汇总.csv']['result_checks']), 2)
        self.assertEqual(len(by_name['周报.md']['result_checks']), 1)
        self.assertTrue(all(r['status'] == 'valid' for r in by_name.values()))
        self.assertTrue(all(r['status'] == 'passed' for row in by_name.values() for r in row['result_checks']))
        body = self.cached(msg)
        self.assertIn('结果检查', body)
        self.assertIn('通过', body)
        self.assertNotIn('[[产物]]', body)
        self.b._artifact_command(self.message(), '/artifacts ' + msg._journal_id)
        self.assertIn('结果检查', self.b.client.send_text.call_args.args[1])

    def test_wrong_total_is_completed_with_failed_check_and_file_still_retrievable(self):
        msg = self.run_checked(self.TOTAL, {'汇总.csv': self.CSV.replace('30', '29')})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['failed'])
        self.assertIn('未通过', self.cached(msg))
        catalog = self.b.journal.artifacts('owner', msg._journal_id)
        self.assertEqual(catalog['records'][0]['status'], 'valid')
        self.assertEqual(Path(catalog['records'][0]['path']).read_text(encoding='utf8'), self.CSV.replace('30', '29'))
        self.b._artifact_command(self.message(), '/getfile ' + msg._journal_id + ' 1')
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertEqual(self.b.queue.queue[0][2]['artifacts'][0]['status'], 'valid')
        self.assertEqual(self.b.runner.run.call_count, 1)

    def test_missing_column_is_a_failed_content_check_without_execution_failure(self):
        msg = self.run_checked(self.COLUMNS, {'汇总.csv': '项目,备注\n交通,待核对\n'})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['failed'])
        self.assertIn('未通过', self.cached(msg))
        self.assertEqual(self.b.journal.artifacts('owner', msg._journal_id)['records'][0]['status'], 'valid')

    def test_missing_section_is_a_failed_content_check_without_execution_failure(self):
        msg = self.run_checked(self.SECTIONS, {'周报.md': '# 周报\n\n## 进展\n完成汇总。\n'})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['failed'])
        self.assertIn('未通过', self.cached(msg))

    def test_no_declared_file_does_not_scan_existing_matching_file(self):
        msg = self.run_checked(self.COLUMNS, {'汇总.csv': self.CSV}, declare=False)
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['unverified'])
        self.assertIn('未能检查', self.cached(msg))
        self.assertEqual(self.b.journal.artifacts('owner', msg._journal_id)['records'], [])
        self.assertTrue((self.workdir / '结果' / '汇总.csv').is_file())

    def test_other_declared_file_does_not_satisfy_target_by_prose(self):
        msg = self.run_checked(self.COLUMNS, {'其他.csv': self.CSV}, prose='汇总.csv 的项目和金额检查通过。')
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['unverified'])
        self.assertIn('未能检查', self.cached(msg))
        self.assertEqual(self.b.journal.artifacts('owner', msg._journal_id)['records'][0]['name'], '其他.csv')

    def test_two_declared_files_with_same_basename_are_ambiguous(self):
        msg = self.run_checked(self.COLUMNS, {'甲/汇总.csv': self.CSV, '乙/汇总.csv': self.CSV})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['unverified'])
        self.assertIn('未能检查', self.cached(msg))
        self.assertEqual(len(self.b.journal.artifacts('owner', msg._journal_id)['records']), 2)

    def test_edit_after_queue_cannot_change_check_snapshot_or_model_requirements(self):
        original = self.saved(checks=self.TOTAL)
        msg = self.command('/template run 周报')
        self.saved(checks='列合计 | 汇总.csv | - | 金额 | 999')
        self.assertEqual(json.loads(self.receipt(msg)['template']), self.expected_execution_snapshot(original))
        self.generated({'汇总.csv': self.CSV})
        self.worker_once()
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['passed'])
        self.assertIn(self.TOTAL, self.b.runner.run.call_args.args[1])
        self.assertNotIn('| 999', self.b.runner.run.call_args.args[1])

    def test_recovery_keeps_original_checks_after_template_deletion(self):
        original = self.saved(checks=self.TOTAL)
        msg, body, job = self.queued()
        self.b.template_store.delete('owner', '周报')
        self.b.journal = TaskJournal(self.b.journal.path)
        recovery = self.message('/resume ' + msg._journal_id)
        self.assertIsNone(self.b._enqueue((recovery, '', None), recovery, recovery_id=msg._journal_id))
        self.assertEqual(self.b.queue.queue[0][2]['template'], self.expected_execution_snapshot(original))
        self.generated({'汇总.csv': self.CSV})
        self.worker_once()
        self.assert_completed(recovery)
        self.assertEqual([r['status'] for r in self.report(recovery)], ['passed'])
        self.assertIn('结果检查', self.cached(recovery))

    def test_batch_template_uses_same_result_checks_and_frozen_input_cleanup(self):
        msg = self.run_checked(self.TOTAL, {'汇总.csv': self.CSV}, batch=True)
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['passed'])
        self.assertIn('固定输入清单', self.b.runner.run.call_args.args[1])
        self.assertIn(self.TOTAL, self.b.runner.run.call_args.args[1])
        self.assertEqual(msg._batch_frozen, [])
        self.assertFalse(list(self.workdir.glob('.wechat-batch-inputs-*')))

    def test_file_changed_after_registration_is_unverified_and_original_record_retained(self):
        import bridge_result_checks
        original_read = bridge_result_checks.read_verified_artifact
        self.saved(checks=self.COLUMNS)
        msg = self.command('/template run 周报')
        self.generated({'汇总.csv': self.CSV})

        def replace_before_verified_read(record, *args, **kwargs):
            Path(record['path']).write_text('项目,备注\n替换版本,不能沿用原指纹\n', encoding='utf8')
            return original_read(record, *args, **kwargs)

        with patch('bridge_result_checks.read_verified_artifact', side_effect=replace_before_verified_read):
            self.worker_once()
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['unverified'])
        self.assertIn('未能检查', self.cached(msg))
        catalog = self.b.journal.artifacts('owner', msg._journal_id)
        self.assertEqual(catalog['records'][0]['status'], 'valid')
        self.assertTrue(catalog['records'][0]['sha256'])
        self.assertTrue(Path(catalog['records'][0]['path']).exists())

    def test_result_check_size_limit_keeps_generated_artifact_and_completion(self):
        # Artifact registration allows 100 MiB; the content-check budget is
        # intentionally smaller, so a valid artifact must remain available.
        content = '项目,金额\n' + 'x' * (10 * 1024 * 1024 + 1)
        msg = self.run_checked(self.COLUMNS, {'汇总.csv': content})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['unverified'])
        self.assertIn('未能检查', self.cached(msg))
        record = self.b.journal.artifacts('owner', msg._journal_id)['records'][0]
        self.assertEqual(record['status'], 'valid')
        self.assertGreater(record['size'], 10 * 1024 * 1024)
        self.assertEqual(Path(record['path']).stat().st_size, record['size'])

    def test_unexpected_check_exception_does_not_interrupt_or_discard_artifact(self):
        with patch('bridge_result_checks.evaluate_rule', side_effect=RuntimeError('injected checker failure')):
            msg = self.run_checked(self.COLUMNS, {'汇总.csv': self.CSV})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['unverified'])
        self.assertIn('未能检查', self.cached(msg))
        catalog = self.b.journal.artifacts('owner', msg._journal_id)
        self.assertEqual(catalog['records'][0]['status'], 'valid')
        self.assertEqual(catalog['records'][0]['result_checks'][0]['status'], 'unverified')
        self.assertEqual(Path(catalog['records'][0]['path']).read_text(encoding='utf8'), self.CSV)

    def test_sqlite_artifact_write_failure_still_reports_files_and_checks_without_retrieval(self):
        with self.b.journal.connect() as db:
            db.execute('CREATE TRIGGER reject_checked_artifacts BEFORE UPDATE OF artifacts,result_checks '
                       "ON tasks BEGIN SELECT RAISE(ABORT, 'injected artifact write failure'); END")
        with self.assertLogs('bridge', level='WARNING'):
            msg = self.run_checked(self.COLUMNS, {'汇总.csv': self.CSV})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in msg._result_checks], ['passed'])
        self.assertFalse(msg._artifact_saved)
        self.assertFalse(msg._result_checks_saved)
        self.assertEqual(msg._artifacts[0]['status'], 'valid')
        self.assertTrue(Path(msg._artifacts[0]['path']).is_file())
        text = self.cached(msg)
        self.assertIn('结果检查', text)
        self.assertIn(msg._artifacts[0]['path'], text)
        self.assertIn('产物清单未保存', text)
        self.assertIn('检查结论未保存', text)
        self.assertNotIn('/getfile ', text)
        catalog = self.b.journal.artifacts('owner', msg._journal_id)
        self.assertEqual(catalog['records'], [])
        self.assertEqual(catalog.get('result_checks', []), [])
        self.b._artifact_command(self.message(), '/getfile ' + msg._journal_id + ' 1')
        self.assertTrue(self.b.queue.empty())
        self.assertNotIn('/getfile ', self.b.client.send_text.call_args.args[1])
        self.assertEqual(self.b.runner.run.call_count, 1)

    def test_verified_read_failure_or_nonbytes_return_is_unverified(self):
        variants = (
            dict(side_effect=OSError('injected verified read failure')),
            dict(return_value=(dict(status='invalid'), b'not a verified version')),
            dict(return_value=(dict(status='valid'), 'unsupported decoded string')),
        )
        for number, options in enumerate(variants):
            with self.subTest(variant=number), patch('bridge_result_checks.read_verified_artifact', **options):
                msg = self.run_checked(self.COLUMNS, {'汇总.csv': self.CSV})
                self.assert_completed(msg)
                self.assertEqual([r['status'] for r in self.report(msg)], ['unverified'])
                self.assertIn('未能检查', self.cached(msg))
                record = self.b.journal.artifacts('owner', msg._journal_id)['records'][0]
                self.assertEqual(record['status'], 'valid')
                self.assertEqual(Path(record['path']).read_text(encoding='utf8'), self.CSV)

    def test_two_rules_for_same_file_read_once_and_share_the_verified_bytes(self):
        import bridge_result_checks
        original_evaluate = bridge_result_checks.evaluate_rule
        seen_bytes = []

        def evaluate(rule, raw):
            seen_bytes.append(raw)
            return original_evaluate(rule, raw)

        with patch('bridge_result_checks.read_verified_artifact',
                   wraps=bridge_result_checks.read_verified_artifact) as reader, \
                patch('bridge_result_checks.evaluate_rule', side_effect=evaluate):
            msg = self.run_checked(self.COLUMNS + '\n' + self.TOTAL, {'汇总.csv': self.CSV})
        self.assert_completed(msg)
        self.assertEqual([r['status'] for r in self.report(msg)], ['passed', 'passed'])
        reader.assert_called_once()
        self.assertEqual(len(seen_bytes), 2)
        self.assertIs(seen_bytes[0], seen_bytes[1])
        self.assertEqual(seen_bytes[0], self.CSV.encode('utf8'))

    def test_stop_after_verified_read_propagates_cancel_and_never_uploads(self):
        import bridge_result_checks
        original_read = bridge_result_checks.read_verified_artifact
        self.saved(checks=self.COLUMNS)
        msg, body, job = self.queued()
        msg._task_control = TaskControl()
        output = self.workdir / '结果' / '汇总.csv'
        self.generated({'汇总.csv': self.CSV}, prose='已生成\n[[发文件]]' + str(output))

        def read_then_stop(*args, **kwargs):
            checked = original_read(*args, **kwargs)
            self.assertTrue(msg._task_control.cancel())
            return checked

        try:
            with patch('bridge_result_checks.read_verified_artifact', side_effect=read_then_stop) as reader, \
                    patch('bridge_result_checks.evaluate_rule') as evaluate, \
                    patch.object(self.b, '_send_files') as send_files:
                with self.assertRaises(TaskCancelled):
                    self.b._run_template(msg, body, job)
            reader.assert_called_once()
            evaluate.assert_not_called()
            send_files.assert_not_called()
            self.b.client.send_file.assert_not_called()
            self.assertTrue(msg._task_control.requested.is_set())
            self.assertTrue(output.is_file())
            self.assertEqual(self.b.journal.artifacts('owner', msg._journal_id)['records'], [])
        finally:
            self.b._worker = None

    def test_history_legacy_table_without_checks_returns_empty_without_any_write(self):
        legacy_root = self.root / 'legacy'
        path = legacy_root / '.runtime' / 'tasks.sqlite'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE tasks(id TEXT,label TEXT,kind TEXT,state TEXT,updated REAL)')
            db.execute("INSERT INTO tasks VALUES ('legacy','旧任务','template_task','queued',1)")
        db.close()
        before = path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir())
        rows = read_history(legacy_root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['result_checks'], [])
        self.assertEqual(rows[0]['state'], 'queued')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns,
                          sorted(p.name for p in path.parent.iterdir())), before)

    def test_history_new_table_reads_full_report_and_template_checks_without_any_write(self):
        checks = self.COLUMNS + '\n' + self.TOTAL
        msg = self.run_checked(checks, {'汇总.csv': self.CSV})
        path = Path(self.b.journal.path)
        before = path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir())
        rows = read_history(self.root)
        row = next(row for row in rows if row['id'] == msg._journal_id)
        self.assertEqual(row['result_checks'], msg._result_checks)
        self.assertEqual(row['template']['definition']['checks'], checks)
        self.assertEqual(row['state'], 'completed')
        self.assertIn('结果检查', row['result_text'])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns,
                          sorted(p.name for p in path.parent.iterdir())), before)

    def test_corrupt_optional_check_metadata_does_not_block_valid_artifact_retrieval(self):
        msg = self.run_checked(self.COLUMNS, {'汇总.csv': self.CSV})
        original = self.b.journal.artifacts('owner', msg._journal_id)['records']
        variants = ('{broken-json', '[null]', json.dumps([
            dict(file='汇总.csv', kind='required_columns', status='unknown', detail='invalid outcome')
        ], ensure_ascii=False))
        for raw in variants:
            with self.subTest(raw=raw):
                with self.b.journal.connect() as db:
                    db.execute('UPDATE tasks SET result_checks=? WHERE id=?', (raw, msg._journal_id))
                catalog = self.b.journal.artifacts('owner', msg._journal_id)
                self.assertEqual(catalog['records'], original)
                self.assertEqual(catalog['result_checks'][0]['status'], 'unverified')
                self.b._artifact_command(self.message(), '/artifacts ' + msg._journal_id)
                self.assertIn('不可读', self.b.client.send_text.call_args.args[1])
                self.b._artifact_command(self.message(), '/getfile ' + msg._journal_id + ' 1')
                self.assertEqual(self.b.queue.qsize(), 1)
                item = self.b.queue.get_nowait()
                self.b.queue.task_done()
                self.assertEqual(item[2]['artifacts'], original)
                self.assertEqual(Path(original[0]['path']).read_text(encoding='utf8'), self.CSV)
        self.assertEqual(self.b.runner.run.call_count, 1)
        self.b.client.send_file.assert_not_called()

    def test_other_user_cannot_read_template_result_or_artifact_checks(self):
        msg = self.run_checked(self.COLUMNS, {'汇总.csv': self.CSV})
        self.assert_completed(msg)
        self.b.allowed.add('other')
        for command in ('/template show 周报', '/result ' + msg._journal_id,
                        '/resend ' + msg._journal_id, '/artifacts ' + msg._journal_id):
            with self.subTest(command=command):
                self.b.client.send_text.reset_mock()
                self.b.handle(self.message(command, uid='other'))
                self.b.client.send_text.assert_called()
                sent = '\n'.join(str(call.args[1]) for call in self.b.client.send_text.call_args_list)
                self.assertNotIn('汇总.csv', sent)
                self.assertNotIn('结果检查', sent)
                self.assertNotIn(self.COLUMNS, sent)
        self.assertEqual(self.b.runner.run.call_count, 1)
        self.assertTrue(self.b.queue.empty())

    def test_result_and_resend_preserve_original_report_after_file_changes(self):
        self.saved(checks=self.COLUMNS)
        msg = self.command('/template run 周报')
        client = ILinkClient('https://example.invalid', '', '', state_dir=str(self.root), max_reply_chars=120)
        self.b.client = client
        chunks = []

        def lose_second_receipt(uid, text, context):
            chunks.append(text)
            if len(chunks) == 2:
                raise ILinkError('injected receipt loss')

        client.send_chunk = lose_second_receipt
        self.generated({'汇总.csv': self.CSV}, prose='汇总已经生成，请核对明细。' * 35)
        with patch('ilink_client.time.sleep'), self.assertLogs('bridge', level='ERROR'):
            self.worker_once()
        self.assertEqual(msg._execution_state, 'completed')
        self.assertEqual([r['status'] for r in self.report(msg)], ['passed'])
        retained = self.b.journal.replies('owner', msg._journal_id, only_pending=True)[0]
        self.assertEqual(retained['accepted_chunks'], 1)
        (self.workdir / '结果' / '汇总.csv').write_text('文件已改变，原检查不应重算', encoding='utf8')
        resent = []
        client.send_chunk = lambda uid, text, context: resent.append(text)
        self.b.runner.run.reset_mock()
        client.max_reply_chars = 500
        with patch('ilink_client.time.sleep'), patch.object(self.b, '_capture_artifacts',
                side_effect=AssertionError('cached result must not re-check files')):
            self.b._history_command(self.message(), '/resend ' + msg._journal_id)
        data = [text for text in resent if text.startswith('（')]
        self.assertEqual(data[0], chunks[1])
        self.assertEqual([text.split('\n', 1)[1] for text in data], split_text(retained['body'], 120)[1:])
        self.assertIn('结果检查', '\n'.join(data))
        self.assertEqual(self.b.journal.replies('owner', msg._journal_id, only_pending=True), [])
        resent.clear()
        with patch('ilink_client.time.sleep'), patch.object(self.b, '_capture_artifacts',
                side_effect=AssertionError('cached result must not re-check files')):
            self.b._history_command(self.message(), '/result ' + msg._journal_id)
        self.assertIn('结果检查', '\n'.join(resent))
        self.assertIn(self.report(msg)[0]['detail'], '\n'.join(resent))
        self.assertNotIn('文件已改变', '\n'.join(resent))
        self.b.runner.run.assert_not_called()
        self.assertTrue(self.b.queue.empty())


if __name__ == '__main__':
    unittest.main(verbosity=2)
