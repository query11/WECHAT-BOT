"""统一验证入口 tools/verify.py：套件表、计数来源、失败与降级路径。

覆盖：
- 快速子集清单与 tools/pre-commit.ps1 逐字一致（防两处清单各改各的）；
- 计数来自真实结果对象：unittest 走 TestResult，旧入口走 selftest.py 的 JSON 摘要；
- 失败 / 错误 / 超时 / 起不来 / 全跳过 / 没加载到用例，一律非零且写明原因；
- 没请求的套件不会被偷偷执行，报告里记成 not_requested；
- 报告和日志只落在报告目录（默认在 .runtime 下），不碰纳入版本控制的文档。
"""
import json
import os
from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_verify():
    spec = importlib.util.spec_from_file_location('verify_entry', ROOT / 'tools' / 'verify.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = load_verify()


PASS_MODULE = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
FAIL_MODULE = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
               "    def test_bad(self):\n        self.fail('boom')\n")
ERROR_MODULE = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                "    def test_err(self):\n        raise RuntimeError('boom')\n")
SKIP_MODULE = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
               "    @unittest.skip('not applicable here')\n    def test_skipped(self):\n        pass\n")
SLOW_MODULE = ("import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n"
               "    def test_slow(self):\n        time.sleep(60)\n")
EMPTY_MODULE = "# 一个用例都没有\n"
PASS_CHECKS = (
    "import json, os\nfrom pathlib import Path\n"
    "Path(os.environ['SELFTEST_REPORT_JSON']).write_text(\n"
    "    json.dumps({'kind':'checks','checks':1,'passed':1,'failed':0}), encoding='utf-8')\n"
)
MIXED_MODULE = ("import unittest\nclass T(unittest.TestCase):\n"
                "    def test_ok(self): pass\n"
                "    @unittest.skip('fixture skip')\n"
                "    def test_skip(self): pass\n"
                "    @unittest.expectedFailure\n"
                "    def test_expected_failure(self): self.fail('expected')\n")


class RegistryTests(unittest.TestCase):
    def test_fast_subset_matches_the_commit_hook(self):
        declared = verify.fast_modules_from_pre_commit(ROOT)
        self.assertIsNotNone(declared, 'tools/pre-commit.ps1 里的 $modules 没解析出来')
        self.assertEqual(tuple(declared), verify.FAST_MODULES)
        self.assertIsNone(verify.fast_modules_drift(ROOT))

    def test_registry_describes_every_suite_and_its_unit(self):
        for name in ('quick', 'offline', 'integration'):
            self.assertIn(name, verify.SUITES)
            suite = verify.SUITES[name]
            self.assertTrue(suite['description'])
            self.assertTrue(suite['requirements'])
            self.assertTrue(suite['runs'])
            for run in suite['runs']:
                self.assertIn(run['kind'], ('cases', 'checks'))
        units = {run['kind'] for suite in verify.SUITES.values() for run in suite['runs']}
        self.assertEqual(units, {'cases', 'checks'})

    def test_integration_is_never_part_of_the_quick_or_offline_suites(self):
        for name in ('quick', 'offline'):
            for run in verify.SUITES[name]['runs']:
                self.assertNotIn('tests.selftest_process_isolation', run.get('modules', ()))

    def test_new_regressions_are_registered_offline_without_quick_recursion(self):
        offline = {module for run in verify.SUITES['offline']['runs']
                   for module in run.get('modules', ())}
        self.assertTrue({'tests.selftest_identity', 'tests.selftest_verify', 'tests.selftest_intake_delivery'} <= offline)
        quick = {module for run in verify.SUITES['quick']['runs']
                 for module in run.get('modules', ())}
        # selftest_verify 会真跑一次 quick，登记到 quick 自身会递归启动子进程。
        self.assertNotIn('tests.selftest_verify', quick)

    def test_every_test_module_belongs_to_a_suite(self):
        # 未登记的测试文件不会被任何入口执行，CI 也不会发现它已经过期。
        self.assertEqual(verify.unregistered_test_modules(ROOT), [])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'tests').mkdir()
            (Path(tmp) / 'tests' / 'selftest_orphan.py').write_text('', encoding='utf-8')
            self.assertEqual(verify.unregistered_test_modules(tmp), ['tests.selftest_orphan'])

    def test_default_entry_only_runs_quick(self):
        # 不带 --suite 时只请求 quick，绝不顺手带上集成测试。
        with patch.object(sys, 'argv', ['verify.py']), \
             patch.object(verify, 'run_one') as fake, \
             patch.object(verify, 'write_json'):
            fake.return_value = {'suite': 'quick', 'id': 'fast', 'status': 'passed',
                                 'counts': {}, 'reason': ''}
            with patch.object(verify, 'git_info', return_value={'short': None, 'dirty': None}):
                code = verify.main([])
        self.assertEqual(code, 0)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(fake.call_args[0][0], 'quick')

    def test_reports_stay_out_of_the_tracked_tree(self):
        self.assertIn('.runtime', str(verify.DEFAULT_REPORT_DIR))
        ignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('.runtime/', ignore)


class ClassifyTests(unittest.TestCase):
    def test_cases_pass_fail_and_missing_result(self):
        self.assertEqual(verify.classify('cases', {'run': 3, 'failures': 0, 'errors': 0,
                                                   'skipped': 0}, 0)[0], 'passed')
        status, reason = verify.classify('cases', {'run': 3, 'failures': 1, 'errors': 0,
                                                   'skipped': 0}, 1)
        self.assertEqual(status, 'failed')
        self.assertIn('失败 1', reason)
        status, reason = verify.classify('cases', None, 1)
        self.assertEqual(status, 'failed')
        self.assertIn('没有拿到机器可读结果', reason)

    def test_empty_or_fully_skipped_never_counts_as_passed(self):
        self.assertEqual(verify.classify('cases', {'run': 0}, 0)[0], 'no_tests')
        status, reason = verify.classify('cases', {'run': 2, 'skipped': 2}, 0)
        self.assertEqual(status, 'no_tests')
        self.assertIn('跳过', reason)

    def test_load_failure_timeout_and_unexpected_success(self):
        self.assertEqual(verify.classify('cases', {'load_error': 'boom'}, 1)[0], 'failed')
        self.assertEqual(verify.classify('cases', None, None),
                         ('timeout', '超过时间上限仍未结束，已写出的结果也不能视为完成'))
        self.assertEqual(verify.classify('cases', {'run': 1, 'unexpected_successes': 1}, 0)[0],
                         'failed')

    def test_success_payload_cannot_override_abnormal_exit_or_timeout(self):
        for kind, payload in (('cases', {'run': 1, 'passed': 1}),
                              ('checks', {'checks': 1, 'passed': 1})):
            for exit_code, expected in ((7, 'failed'), (-1, 'failed'), (None, 'timeout')):
                with self.subTest(kind=kind, exit_code=exit_code):
                    self.assertEqual(verify.classify(kind, payload, exit_code)[0], expected)

    def test_checks_unit_uses_the_old_entry_count(self):
        self.assertEqual(verify.classify('checks', {'checks': 2066, 'failed': 0}, 0)[0], 'passed')
        self.assertEqual(verify.classify('checks', {'checks': 0}, 0)[0], 'no_tests')
        status, reason = verify.classify('checks', {'checks': 5, 'failed': 2}, 1)
        self.assertEqual(status, 'failed')
        self.assertIn('失败 2', reason)


class SyntheticRunTests(unittest.TestCase):
    """用假套件真的跑一遍入口，检查退出码、报告和日志。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='verify-entry-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '假 项目'
        (self.root / 'tools').mkdir(parents=True)
        for name, text in (('vpass.py', PASS_MODULE), ('vfail.py', FAIL_MODULE),
                           ('verror.py', ERROR_MODULE), ('vskip.py', SKIP_MODULE),
                           ('vslow.py', SLOW_MODULE), ('vempty.py', EMPTY_MODULE),
                           ('vmixed.py', MIXED_MODULE)):
            (self.root / name).write_text(text, encoding='utf-8')
        self.reports = Path(self.temp.name) / '报告 目录'

    def suite(self, name, modules, kind='cases'):
        run = {'id': name, 'kind': kind, 'modules': tuple(modules)}
        return {'description': '合成套件', 'requirements': '仅测试用', 'runs': (run,)}

    def run_entry(self, suites, requested, extra=()):
        added = {name: self.suite(name.replace('fake-', ''), modules)
                 for name, modules in suites.items()}
        with patch.dict(verify.SUITES, added):
            return verify.main(['--suite', requested, '--root', str(self.root),
                                '--report-dir', str(self.reports), '--label', 'test',
                                *extra])

    def report(self):
        return json.loads((self.reports / 'test' / 'report.json').read_text(encoding='utf-8'))

    def run_checks(self, source, timeout=10):
        (self.root / 'fake_checks.py').write_text(source, encoding='utf-8')
        suite = {'description': '合成旧入口', 'requirements': '仅测试用',
                 'runs': ({'id': 'checks', 'kind': 'checks', 'script': 'fake_checks.py'},)}
        with patch.dict(verify.SUITES, {'fake-checks': suite}):
            return verify.main(['--suite', 'fake-checks', '--root', str(self.root),
                                '--report-dir', str(self.reports), '--label', 'test',
                                '--timeout', str(timeout)])

    def test_passing_suite_reports_machine_readable_counts(self):
        self.assertEqual(self.run_entry({'fake-pass': ['vpass']}, 'fake-pass'), 0)
        result = self.report()['results'][0]
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['counts']['run'], 1)
        self.assertEqual(result['counts']['passed'], 1)
        self.assertEqual(result['unit'], '测试用例数')
        self.assertTrue(Path(result['log']).is_file())
        self.assertTrue(Path(result['log']).read_text(encoding='utf-8').strip())

    def test_failing_suite_returns_non_zero_and_names_the_case(self):
        self.assertEqual(self.run_entry({'fake-fail': ['vfail']}, 'fake-fail'), 1)
        report = self.report()
        self.assertFalse(report['ok'])
        self.assertEqual(report['exit_code'], 1)
        result = report['results'][0]
        self.assertEqual(result['status'], 'failed')
        self.assertIn('vFail'.lower(), result['failures'][0].lower())

    def test_erroring_suite_is_not_a_pass(self):
        self.assertEqual(self.run_entry({'fake-error': ['verror']}, 'fake-error'), 1)
        self.assertEqual(self.report()['results'][0]['status'], 'failed')

    def test_all_skipped_suite_is_no_tests_not_passed(self):
        self.assertEqual(self.run_entry({'fake-skip': ['vskip']}, 'fake-skip'), 1)
        result = self.report()['results'][0]
        self.assertEqual(result['status'], 'no_tests')
        self.assertEqual(result['counts']['skipped'], 1)
        self.assertEqual(result['counts']['passed'], 0)
        self.assertIn('跳过', result['reason'])

    def test_passed_counts_only_success_not_skip_or_expected_failure(self):
        self.assertEqual(self.run_entry({'fake-mixed': ['vmixed']}, 'fake-mixed'), 0)
        counts = self.report()['results'][0]['counts']
        self.assertEqual(counts['run'], 3)
        self.assertEqual(counts['passed'], 1)
        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(counts['expected_failures'], 1)

    def test_multiple_failed_subtests_do_not_make_success_count_negative(self):
        (self.root / 'vsubtests.py').write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_subtests(self):\n"
            "        for value in (1, 2):\n"
            "            with self.subTest(value=value): self.fail('failure')\n",
            encoding='utf-8')
        self.assertEqual(self.run_entry({'fake-subtests': ['vsubtests']}, 'fake-subtests'), 1)
        counts = self.report()['results'][0]['counts']
        self.assertEqual(counts['run'], 1)
        self.assertEqual(counts['failed'], 2)
        self.assertEqual(counts['passed'], 0)

    def test_successful_checks_json_does_not_hide_failed_process(self):
        self.assertEqual(self.run_checks(PASS_CHECKS + 'raise SystemExit(7)\n'), 1)
        result = self.report()['results'][0]
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['counts']['passed'], 1)
        self.assertIn('退出码 7', result['reason'])

    def test_successful_checks_json_does_not_hide_timeout(self):
        self.assertEqual(self.run_checks(PASS_CHECKS + 'import time\ntime.sleep(30)\n', 1), 1)
        result = self.report()['results'][0]
        self.assertEqual(result['status'], 'timeout')
        self.assertIsNone(result['exit_code'])
        self.assertEqual(result['counts']['passed'], 1)

    def test_reusing_label_never_consumes_previous_checks_result(self):
        self.assertEqual(self.run_checks(PASS_CHECKS), 0)
        previous = Path(self.report()['results'][0]['result'])
        self.assertTrue(previous.is_file())
        self.assertEqual(self.run_checks('raise SystemExit(0)\n'), 1)
        result = self.report()['results'][0]
        self.assertNotEqual(Path(result['result']), previous)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['counts'], {})
        self.assertIn('没有拿到机器可读结果', result['reason'])

    def test_reusing_label_never_consumes_previous_unittest_result(self):
        self.assertEqual(self.run_entry({'fake-cases': ['vpass']}, 'fake-cases'), 0)
        previous = Path(self.report()['results'][0]['result'])
        (self.root / 'vexit.py').write_text('raise SystemExit(0)\n', encoding='utf-8')
        self.assertEqual(self.run_entry({'fake-cases': ['vexit']}, 'fake-cases'), 1)
        result = self.report()['results'][0]
        self.assertNotEqual(Path(result['result']), previous)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['counts'], {})

    def test_module_without_tests_is_reported_as_a_load_failure(self):
        self.assertEqual(self.run_entry({'fake-empty': ['vempty']}, 'fake-empty'), 1)
        result = self.report()['results'][0]
        self.assertEqual(result['status'], 'failed')
        self.assertIn('没加载起来', result['reason'])

    def test_requested_suite_that_times_out_returns_non_zero(self):
        self.assertEqual(
            self.run_entry({'fake-slow': ['vslow']}, 'fake-slow', ('--timeout', '3')), 1)
        result = self.report()['results'][0]
        self.assertEqual(result['status'], 'timeout')
        self.assertIsNone(result['exit_code'])
        self.assertIn('超时', Path(result['log']).read_text(encoding='utf-8'))

    def test_unstartable_suite_is_reported_as_launch_failure(self):
        broken = [os.path.join(str(self.root), 'does-not-exist.exe')]
        with patch.object(verify, 'build_command',
                          return_value=(broken, dict(os.environ), self.root / 'missing.json')):
            self.assertEqual(self.run_entry({'fake-pass': ['vpass']}, 'fake-pass'), 1)
        result = self.report()['results'][0]
        self.assertEqual(result['status'], 'launch_failed')
        self.assertIn('起不来', result['reason'])

    def test_unrequested_suites_are_not_run_and_are_listed(self):
        code = self.run_entry({'fake-pass': ['vpass'], 'fake-fail': ['vfail']}, 'fake-pass')
        self.assertEqual(code, 0)
        report = self.report()
        self.assertEqual([r['suite'] for r in report['results']], ['fake-pass'])
        self.assertIn('fake-fail', report['not_requested'])

    def test_report_separates_check_entries_from_unittest_cases(self):
        script = self.root / 'fake_checks.py'
        script.write_text(
            "import json, os\n"
            "json.dump({'kind': 'checks', 'checks': 7, 'passed': 7, 'failed': 0},\n"
            "          open(os.environ['SELFTEST_REPORT_JSON'], 'w', encoding='utf-8'))\n"
            "print('通过 7 项，失败 0 项')\n", encoding='utf-8')
        added = {'fake-checks': {'description': '合成旧入口', 'requirements': '仅测试用',
                                 'runs': ({'id': 'old-entry', 'kind': 'checks',
                                           'script': 'fake_checks.py'},)}}
        with patch.dict(verify.SUITES, added):
            code = verify.main(['--suite', 'fake-checks', '--root', str(self.root),
                                '--report-dir', str(self.reports), '--label', 'mixed'])
        self.assertEqual(code, 0)
        report = json.loads((self.reports / 'mixed' / 'report.json').read_text(encoding='utf-8'))
        result = report['results'][0]
        self.assertEqual(result['kind'], 'checks')
        self.assertEqual(result['unit'], '检查条目数')
        self.assertEqual(result['counts'], {'checks': 7, 'passed': 7, 'failed': 0})
        self.assertIn('不能相加', report['note'])

    def test_the_real_quick_suite_really_runs_end_to_end(self):
        """真跑一次 quick：证明报告里的计数与 unittest 自己的结果对得上。"""
        code = verify.main(['--suite', 'quick', '--label', 'selftest-verify',
                            '--report-dir', str(self.reports)])
        self.assertEqual(code, 0)
        result = json.loads(
            (self.reports / 'selftest-verify' / 'report.json').read_text(encoding='utf-8'))
        counts = result['results'][0]['counts']
        self.assertGreater(counts['run'], 50, counts)
        self.assertEqual(counts['failed'], 0)
        self.assertEqual(counts['errors'], 0)
        self.assertEqual(sorted(result['not_requested']), ['integration', 'offline'])
        # 报告里的提交号确实来自仓库，而不是编的
        if result['git']['commit']:
            head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                  capture_output=True).stdout.decode().strip()
            self.assertEqual(result['git']['commit'], head)


if __name__ == '__main__':
    unittest.main()
