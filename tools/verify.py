#!/usr/bin/env python3
"""统一的验证入口：把项目里那几个"入口"明确登记成套件，并留下可复核的报告。

    python tools/verify.py --list                    # 每个套件的性质与前置条件
    python tools/verify.py --suite quick             # 提交钩子用的快速子集
    python tools/verify.py --suite offline           # 旧入口 + 快速子集 + 身份/入口专项
    python tools/verify.py --suite integration       # Windows 进程隔离集成测试（必须点名）
    python tools/verify.py --suite quick --suite offline --suite integration

几条硬规矩（都是上一轮踩过或差点踩到的）：

1. 只跑**明确请求**的套件。Windows 集成测试必须显式点名，绝不默认拉起真实业务、
   浏览器或用户程序；没点名的套件在报告里记成 not_requested。
2. 套件表是显式登记，不做"扫描 selftest*.py 然后全跑"。清单本身也有防漂移检查：
   快速子集的模块清单必须与 ``tools/pre-commit.ps1`` 里那份逐字一致。
3. 计数取**真实结果对象或机器可读输出**：unittest 走子进程里的 ``TestResult``，
   旧入口 ``selftest.py`` 走它自己写出的 JSON 摘要（环境变量 SELFTEST_REPORT_JSON）。
   不去正则匹配中文终端文本，也不因为出现"OK"就判定整体通过。
4. 旧入口记的是**检查条目数**，unittest 记的是**测试用例数**，两者单位不同，
   报告里分开写、不相加、不冒充一个"总测试数"。
5. 被请求的套件只要出现失败、错误、超时、起不来、没加载到用例，或有条件跳过导致
   **一条有效用例都没跑**，整体返回非零并写明原因；全跳过不算"已验证通过"。
6. 报告和日志只写 ``<报告目录>/<本轮标识>/``，默认在 ``.runtime/`` 下（已被 .gitignore
   忽略）。不写任何纳入版本控制的验收文档，也不复制真实配置、聊天内容或凭据。
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_DIR = ROOT / '.runtime' / 'verification'
DEFAULT_TIMEOUT_SECONDS = 900

# 提交钩子那份快速子集。必须与 tools/pre-commit.ps1 的 $modules 逐字一致，
# 不一致时下面 fast_modules_drift() 会拦下来，免得两处清单各改各的。
FAST_MODULES = (
    'tests.selftest_network_route',    # 连接路由与加密 DNS 兜底
    'tests.selftest_runtime',          # 托盘状态与重连提示窗口
    'tests.selftest_experience',       # 故障说人话与恢复
    'tests.selftest_standby',          # 唤醒 / 待机门控
    'tests.selftest_reliability',      # 配置、队列与收尾
    'tests.selftest_bot_token',        # bot_token 的 DPAPI 存放
    'tests.selftest_key_vault',        # DPAPI 原语
    'tests.selftest_backend_switch',   # 保存的后端选择
    'tests.selftest_backend_profile',  # 后端配置窗口
    'tests.selftest_hooks',            # 暂存路径分类与失败传播
    'tests.selftest_process_isolation.BootstrapRequestTests',   # 启动请求守卫，不需要 GUI
)

# 验证入口的测试会真跑 quick，因此只登记到 offline，不能放进 quick 造成递归。
OFFLINE_ONLY_MODULES = ('tests.selftest_identity', 'tests.selftest_verify', 'tests.selftest_result_revisions', 'tests.selftest_desktop_files', 'tests.selftest_desktop_schedules',
                        'tests.selftest_task_handoff', 'tests.selftest_attachment_limit',
                        'tests.selftest_structured_results', 'tests.selftest_history_resilience',
                        'tests.selftest_history_paths', 'tests.selftest_intake_delivery',
                        'tests.selftest_task_artifacts', 'tests.selftest_artifact_integration',
                        'tests.selftest_queue_management', 'tests.selftest_attachment_batches',
                        'tests.selftest_batch_integration', 'tests.selftest_task_templates',
                        'tests.selftest_template_journal', 'tests.selftest_template_integration',
                        'tests.selftest_journal_retention', 'tests.selftest_snapshot_lifecycle',
                        'tests.selftest_command_guidance', 'tests.selftest_template_result_checks', 'tests.selftest_desktop_workflow', 'tests.selftest_task_progress',
                        'tests.selftest_result_checks_integration', 'tests.selftest_workflow_actions', 'tests.selftest_template_desktop')

SUITES = {
    'quick': {
        'description': '提交钩子用的快速子集：10 个模块 + 一个不需要 GUI 的入口守卫类。',
        'requirements': '离线、不需要桌面、不联网、不启动真实后台；约 10 秒。'
                        '单位是 unittest 测试用例数。',
        'runs': ({'id': 'fast', 'kind': 'cases', 'modules': FAST_MODULES},),
    },
    'offline': {
        'description': '离线回归：旧入口 selftest.py 的全部检查、快速子集，'
                       '以及代码身份、验证入口、取消、附件反馈、接收确认、文字补发和历史读取专项；各项分别记录。'
                       '单位不同（检查条目数 vs 测试用例数）。',
        'requirements': '离线、不需要真实 config.json 或 .runtime（测试自建模拟服务、'
                        '合成数据和临时目录）；约 4 分钟。**不覆盖手机实机行为**——'
                        '收发、附件、取消、提醒这些仍要按 实机验收.md 在手机上验收。',
        'runs': (
            {'id': 'selftest-checks', 'kind': 'checks', 'script': 'selftest.py'},
            {'id': 'fast', 'kind': 'cases', 'modules': FAST_MODULES},
            {'id': 'offline-only', 'kind': 'cases', 'modules': OFFLINE_ONLY_MODULES},
        ),
    },
    'integration': {
        'description': 'Windows 进程隔离集成测试：真实 Job Object、真实子进程。',
        'requirements': '仅 Windows；必须显式 --suite integration 才会运行。'
                        '只使用临时宿主与心跳子进程，不启动真实后台、浏览器或用户程序。'
                        '其中 4 条用例要创建并嵌套 Job Object：在受限的父 Job（例如某些'
                        'IDE/沙箱的 kill-on-close Job）里可能以 WinError 5 报错。'
                        '需核对失败位置与宿主限制，不能仅凭错误码排除回归；'
                        '可在普通 shell 里重跑验证。'
                        '非 Windows 上整组会被跳过，此时判为"没有有效用例"而非通过。',
        'runs': ({'id': 'process-isolation', 'kind': 'cases',
                  'modules': ('tests.selftest_process_isolation',)},),
    },
}


def fast_modules_from_pre_commit(root=ROOT):
    """从 tools/pre-commit.ps1 里读出那份 $modules 清单（读不到就返回 None）。"""
    try:
        text = (Path(root) / 'tools' / 'pre-commit.ps1').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    match = re.search(r'\$modules\s*=\s*@\((.*?)\n\)', text, re.S)
    if not match:
        return None
    names = re.findall(r"'([^']+)'", match.group(1))
    return tuple(names) or None


def fast_modules_drift(root=ROOT):
    """返回不一致说明，或 None 表示一致/读不到（读不到只算警告，不拦运行）。"""
    declared = fast_modules_from_pre_commit(root)
    if declared is None:
        return None
    if declared != FAST_MODULES:
        return '快速子集清单与 tools/pre-commit.ps1 不一致：verify.py=%s / pre-commit.ps1=%s' % (
            list(FAST_MODULES), list(declared))
    return None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def git_info(root=ROOT):
    """代码提交号与是否有未提交改动。取不到就如实说未知，不假装干净。"""
    def run(*args):
        try:
            done = subprocess.run(['git', '-C', str(root)] + list(args),
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        return done.stdout.decode('utf-8', 'replace')

    info = {'commit': None, 'short': None, 'branch': None, 'dirty': None, 'dirty_paths': []}
    commit = run('rev-parse', 'HEAD')
    if commit:
        info['commit'] = commit.strip()
        info['short'] = info['commit'][:7]
    branch = run('rev-parse', '--abbrev-ref', 'HEAD')
    if branch:
        info['branch'] = branch.strip()
    status = run('status', '--porcelain')
    if status is not None:
        paths = [line for line in status.splitlines() if line.strip()]
        info['dirty'] = bool(paths)
        info['dirty_paths'] = paths[:50]
    return info


# ---------- 子进程侧：把真实结果对象写成 JSON ----------

def internal_cases(argv):
    """--internal-cases <json 路径> <项目根> <模块...>：跑 unittest 并落一份 JSON。"""
    report_path, root, modules = argv[0], argv[1], list(argv[2:])
    if root not in sys.path:
        sys.path.insert(0, root)
    payload = {'kind': 'cases', 'modules': modules, 'run': 0, 'passed': 0, 'failures': 0, 'errors': 0,
               'skipped': 0, 'expected_failures': 0, 'unexpected_successes': 0,
               'failed_ids': [], 'skipped_ids': [], 'load_error': None}
    try:
        import unittest
        suite = unittest.TestLoader().loadTestsFromNames(modules)
    except Exception as exc:                                    # noqa: BLE001 - 如实上报
        payload['load_error'] = '%s: %s' % (type(exc).__name__, exc)
        try:
            write_json(report_path, payload)
        finally:
            print('[verify] 加载测试失败：' + payload['load_error'], file=sys.stderr)
        return 1
    if suite.countTestCases() == 0:
        payload['load_error'] = '没有加载到任何测试用例：' + ' '.join(modules)
        write_json(report_path, payload)
        print('[verify] ' + payload['load_error'], file=sys.stderr)
        return 1
    class CountedResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passed = 0

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed += 1

    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout,
                                     resultclass=CountedResult).run(suite)
    payload.update(
        run=result.testsRun,
        passed=result.passed,
        failures=len(result.failures),
        errors=len(result.errors),
        skipped=len(result.skipped),
        expected_failures=len(result.expectedFailures or []),
        unexpected_successes=len(result.unexpectedSuccesses or []),
        failed_ids=[str(case) for case, _ in list(result.failures) + list(result.errors)],
        skipped_ids=[str(case) for case, _ in result.skipped])
    write_json(report_path, payload)
    return 0 if result.wasSuccessful() else 1


# ---------- 父进程侧 ----------

def build_command(run, root, report_path):
    """(命令, 环境变量, 结果 JSON 路径)。两种 kind 都通过文件拿机器可读结果。"""
    env = dict(os.environ)
    # 日志按 UTF-8 落盘：中文输出不该因为控制台代码页变成乱码。
    env['PYTHONIOENCODING'] = 'utf-8'
    if run['kind'] == 'cases':
        command = [sys.executable, str(Path(__file__).resolve()),
                   '--internal-cases', str(report_path), str(root)] + list(run['modules'])
        return command, env, report_path
    result_path = report_path.with_name(report_path.stem + '.json')
    env['SELFTEST_REPORT_JSON'] = str(result_path)
    command = [sys.executable, '-u', run['script']]
    return command, env, result_path


def classify(kind, payload, exit_code):
    """(状态, 原因)。状态只有 passed / failed / no_tests / timeout / launch_failed。"""
    if exit_code is None:
        return 'timeout', '超过时间上限仍未结束，已写出的结果也不能视为完成'
    if payload is None:
        return 'failed', '没有拿到机器可读结果（子进程退出码 %s），不按通过处理' % exit_code
    if kind == 'cases':
        if payload.get('load_error'):
            return 'failed', '测试没加载起来：' + str(payload['load_error'])
        run = int(payload.get('run') or 0)
        bad = int(payload.get('failures') or 0) + int(payload.get('errors') or 0)
        unexpected = int(payload.get('unexpected_successes') or 0)
        if bad or unexpected:
            return 'failed', '失败 %d、错误 %d、意外通过 %d' % (
                payload.get('failures') or 0, payload.get('errors') or 0, unexpected)
        if run == 0:
            return 'no_tests', '一条用例都没跑'
        if int(payload.get('skipped') or 0) == run:
            return 'no_tests', '全部 %d 条用例都被跳过（本机不满足条件），不算通过' % run
        if exit_code != 0:
            return 'failed', '子进程退出码 %s，结果文件不能覆盖异常退出' % exit_code
        return 'passed', ''
    checks = int(payload.get('checks') or 0)
    failed = int(payload.get('failed') or 0)
    if failed:
        return 'failed', '检查条目失败 %d 项' % failed
    if checks == 0:
        return 'no_tests', '一条检查都没跑'
    if exit_code != 0:
        return 'failed', '子进程退出码 %s，结果文件不能覆盖异常退出' % exit_code
    return 'passed', ''


def run_one(suite_name, run, root, log_dir, timeout, drift):
    log_path = log_dir / ('%s.%s.log' % (suite_name, run['id']))
    # label 可重复使用，但结果文件必须属于当前子进程，不能读到上轮残留 JSON。
    # 使用独立路径也避免并发运行相同 label 时彼此消费结果。
    result_path = log_dir / ('%s.%s.%s.json' % (suite_name, run['id'], uuid.uuid4().hex))
    command, env, result_path = build_command(run, root, result_path)
    record = {'suite': suite_name, 'id': run['id'], 'kind': run['kind'],
              'unit': '检查条目数' if run['kind'] == 'checks' else '测试用例数',
              'command': command, 'exit_code': None, 'status': 'failed', 'reason': '',
              'started_at': datetime.datetime.now().isoformat(timespec='seconds'),
              'duration_sec': None, 'log': str(log_path), 'result': str(result_path),
              'counts': {}, 'failures': []}
    started = time.monotonic()
    exit_code = None
    try:
        with open(log_path, 'w', encoding='utf-8', errors='replace') as log:
            try:
                done = subprocess.run(command, cwd=str(root), env=env, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=timeout)
                exit_code = done.returncode
            except subprocess.TimeoutExpired:
                exit_code = None
                print('[verify] %s/%s 超时（%ss）' % (suite_name, run['id'], timeout), file=log)
    except OSError as exc:
        record['status'] = 'launch_failed'
        record['reason'] = '子进程起不来：%s' % exc
        record['duration_sec'] = round(time.monotonic() - started, 2)
        return record

    payload = None
    try:
        payload = json.loads(Path(result_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        payload = None

    status, reason = classify(run['kind'], payload, exit_code)
    if drift and run['id'] == 'fast':
        status, reason = 'failed', drift
    record['status'] = status
    record['reason'] = reason
    record['exit_code'] = exit_code
    record['duration_sec'] = round(time.monotonic() - started, 2)
    if payload is not None:
        if run['kind'] == 'checks':
            record['counts'] = {'checks': payload.get('checks'),
                                'passed': payload.get('passed'),
                                'failed': payload.get('failed')}
            record['failures'] = list(payload.get('failures') or [])[:50]
        else:
            record['counts'] = {'run': payload.get('run'), 'passed': payload.get('passed'),
                                'failed': payload.get('failures'), 'errors': payload.get('errors'),
                                'skipped': payload.get('skipped'),
                                'expected_failures': payload.get('expected_failures'),
                                'unexpected_successes': payload.get('unexpected_successes')}
            record['failures'] = list(payload.get('failed_ids') or [])[:50]
            record['skipped'] = list(payload.get('skipped_ids') or [])[:50]
    return record


def print_registry():
    print('已登记的套件（单位不同，不能相加）：')
    for name, suite in SUITES.items():
        print('\n  %s  -- %s' % (name, suite['description']))
        print('      前置条件：%s' % suite['requirements'])
        for run in suite['runs']:
            unit = '检查条目数' if run['kind'] == 'checks' else '测试用例数'
            print('      - %s（%s）' % (run['id'], unit))
    print('\n默认只跑 quick。Windows 集成测试要显式 --suite integration。')


def cli(argv):
    parser = argparse.ArgumentParser(
        prog='tools/verify.py', description='统一的验证入口（只跑明确请求的套件）')
    parser.add_argument('--suite', action='append', choices=sorted(SUITES), default=None,
                        help='要运行的套件，可重复；默认 quick')
    parser.add_argument('--list', action='store_true', help='列出套件表与前置条件后退出')
    parser.add_argument('--label', default=None, help='本轮标识（默认时间戳加提交号）')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help='单个套件的时间上限，秒（默认 %d）' % DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument('--report-dir', default=None,
                        help='报告根目录（默认 <项目>/.runtime/verification）')
    parser.add_argument('--root', default=None, help='项目根目录（默认本文件的上两级）')
    args = parser.parse_args(argv)

    if args.list:
        print_registry()
        return 0

    root = Path(args.root).resolve() if args.root else ROOT
    requested = args.suite or ['quick']
    if len(set(requested)) != len(requested):
        print('[verify] 同一个套件被请求了多次', file=sys.stderr)
        return 2

    started_at = datetime.datetime.now()
    info = git_info(root)
    label = args.label or '%s-%s%s' % (started_at.strftime('%Y%m%d-%H%M%S'),
                                       info['short'] or 'nogit',
                                       '-dirty' if info['dirty'] else '')
    report_dir = Path(args.report_dir).resolve() if args.report_dir else DEFAULT_REPORT_DIR
    out_dir = report_dir / label
    log_dir = out_dir / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)

    drift = fast_modules_drift(root)
    if drift:
        print('[verify] 警告：' + drift, file=sys.stderr)

    records = []
    for name in requested:
        for run in SUITES[name]['runs']:
            record = run_one(name, run, root, log_dir, args.timeout, drift)
            records.append(record)
            print('[verify] %-8s %-18s %-10s %s' % (
                record['suite'], record['id'], record['status'],
                (record['counts'] or {})))
            if record['status'] != 'passed':
                print('[verify]   原因：' + record['reason'], file=sys.stderr)

    ok = all(record['status'] == 'passed' for record in records)
    report = {
        'label': label,
        'started_at': started_at.isoformat(timespec='seconds'),
        'finished_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'duration_sec': round((datetime.datetime.now() - started_at).total_seconds(), 2),
        'python': sys.version.split()[0],
        'platform': platform.platform(),
        'executable': sys.executable,
        'root': str(root),
        'git': info,
        'requested_suites': requested,
        'suite_requirements': {name: {'description': SUITES[name]['description'],
                                      'requirements': SUITES[name]['requirements']}
                               for name in requested},
        'not_requested': [name for name in SUITES if name not in requested],
        'fast_list_drift': drift,
        'results': records,
        'ok': ok,
        'exit_code': 0 if ok else 1,
        'note': '检查条目数与测试用例数是两种单位，不能相加成一个"总测试数"。'
                '离线回归不代表手机实机行为已经验收。',
    }
    write_json(out_dir / 'report.json', report)
    print('\n[verify] 本轮标识 %s' % label)
    print('[verify] 报告 %s' % (out_dir / 'report.json'))
    print('[verify] 日志 %s' % log_dir)
    print('[verify] 结果：%s' % ('全部通过' if ok else '存在未通过或没跑成的套件'))
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--internal-cases':
        return internal_cases(argv[1:])
    return cli(argv)


if __name__ == '__main__':
    raise SystemExit(main())
