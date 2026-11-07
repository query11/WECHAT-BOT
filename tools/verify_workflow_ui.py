"""Exercise real history controls against isolated stores and a fake transport."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bridge_runtime import Runtime
from tests.selftest_workflow_actions import WorkflowTests
from task_templates import TaskTemplateStore
from remind import ReminderStore, _next_daily
from attachment_batches import collect_input_records
from task_artifacts import validate_artifact


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = WorkflowTests()
    fixture.setUp()
    stop, errors, status_retries = threading.Event(), [], []
    thread = None
    try:
        bridge = fixture.b
        bridge.template_store = TaskTemplateStore(bridge.journal.path)
        bridge._worker = Mock()
        bridge._worker.is_alive.return_value = True
        bridge._started_at = time.time()
        bridge._last_ctx['owner'] = ('owner', 'isolated-fake-context')
        bridge.allowed.update(('owner', 'other'))
        (fixture.root / 'desktop-input.csv').write_text('项目,金额\n甲,10\n乙,30\n', encoding='utf-8')
        for state, label in (('recoverable', '待恢复：汇总上周材料'),
                             ('uncertain', '待核对：生成周报'), ('completed', '已完成：附件整理'),
                             ('failed', '失败：缺少输入文件')):
            ident, _ = bridge.journal.add('owner', state, 'task', label, payload='隔离验证任务')
            bridge.journal.update(ident, state, summary='仅用于窗口验收的合成记录')
        sample = bridge.template_store.save('owner', '周报', dict(inputs='本次确认的报表', steps='核对数据并汇总', output_dir='工作目录', output_format='CSV', checks='列合计 | 汇总.csv | - | 金额 | 40'))
        entries = [fixture.entry('输入.csv', '项目,金额\n甲,10\n乙,30\n'.encode('utf-8'))]
        batch = bridge.batch_store.create_recent('owner', '窗口验收材料', collect_input_records(entries, fixture.inbox, [fixture.root], 'owner'))
        bridge.batch_store.seal('owner', batch['id'])
        ident, _, _ = bridge.journal.add_batch('owner', 'rich-history', '整理本周材料', batch['id'], template=sample)
        product = fixture.workdir / '汇总.csv';product.write_text('项目,金额\n合计,40\n', encoding='utf-8')
        bridge.journal.update(ident, 'completed', summary='已生成汇总表；金额合计 40，检查通过。', outputs=[str(product)])
        checks = [dict(file='汇总.csv', kind='column_sum', status='passed', condition='金额合计应为 40', detail='实际合计 40，与预期一致。')]
        with bridge.journal.connect() as db:
            db.execute('UPDATE tasks SET artifacts=?,result_checks=? WHERE id=?',
                       (json.dumps([validate_artifact(str(product), **bridge._artifact_options())]),json.dumps(checks),ident))
        bridge.journal.prepare_reply(ident, '已汇总本周材料，金额合计为 40。\n文件：汇总.csv', 'completed')
        seed_task = ident
        bridge.reminders = ReminderStore(str(fixture.root / 'reminders.json'))
        bridge.reminders.add('owner', time.time()-60, '过去的合成触发', '整理本周材料', kind='task')
        fired = bridge.reminders.due()[0]
        bridge.reminders.record_run(fired, 'queued', '已入队', ident)
        bridge.reminders.add('owner', _next_daily(9, 0, time.time(), slack=0), '每天 09:00', '核对今日工作安排', daily=dict(hour=9, minute=0))
        paused, _ = bridge.reminders.add('owner', time.time()+86400, '明天', '汇总本周报表', kind='task')
        bridge.reminders.manage('owner', paused['id'], bridge.reminders.version(paused), 'pause')
        review_entries = [fixture.entry('选错的文件.txt', b'wrong file'), fixture.entry('本次材料.txt', b'correct input')]
        review_batch = bridge.batch_store.create_recent('owner', '待核对材料', collect_input_records(review_entries, fixture.inbox, [fixture.root], 'owner'))
        bridge.batch_store.begin_intake('owner', review_batch['id'], 'failed-fixture-download')
        bridge.batch_store.finish_intake('owner', review_batch['id'], 'failed-fixture-download', [], failed=1)
        runtime = Runtime(bridge, fixture.root / '.runtime')
        runtime.connected()
        runtime.tick()

        def serve():
            while not stop.wait(.1):
                try:
                    runtime.tick()
                except Exception as exc:
                    # Production Runtime.loop retries a snapshot if a Windows
                    # reader briefly holds status.json without delete sharing.
                    target = getattr(exc, 'filename2', None) or getattr(exc, 'filename', None)
                    if (isinstance(exc, PermissionError) and target
                            and Path(target) == runtime.root / 'status.json' and len(status_retries) < 10):
                        status_retries.append(repr(exc))
                    else:
                        errors.append(repr(exc))
                        stop.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        shell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        command = [str(shell), '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                   '-File', str(ROOT / 'task_history.ps1'), '-Verify', '-VerifyActions',
                   '-SkipClipboardVerification', '-Root', str(fixture.root), '-FocusId', seed_task]
        template_command = [str(shell), '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                            '-File', str(ROOT / 'template_editor.ps1'), '-Verify', '-Root', str(fixture.root), '-TaskId', seed_task]
        template_result = subprocess.run(template_command, capture_output=True, encoding='utf-8', errors='replace',
                                         timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        (args.output / 'template-verification.log').write_text(template_result.stdout + template_result.stderr, encoding='utf-8')
        template_screenshot = fixture.root / '.runtime/template-editor-verified.png'
        if template_screenshot.exists():
            shutil.copy2(template_screenshot, args.output / template_screenshot.name)
        result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        (args.output / 'window-verification.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        screenshot = fixture.root / '.runtime/task-history-verified.png'
        if screenshot.exists():
            shutil.copy2(screenshot, args.output / screenshot.name)
        workflow_command = [str(shell), '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                            '-File', str(ROOT / 'workflow_window.ps1'), '-Verify', '-Root', str(fixture.root),
                            '-Page', 'batches', '-ItemId', review_batch['id']]
        workflow_result = subprocess.run(workflow_command, capture_output=True, encoding='utf-8', errors='replace',
                                         timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        (args.output / 'workflow-verification.log').write_text(workflow_result.stdout + workflow_result.stderr, encoding='utf-8')
        direct_command = workflow_command[:-4] + ['-Page', 'revise', '-ItemId', seed_task]
        direct_result = subprocess.run(direct_command, capture_output=True, encoding='utf-8', errors='replace',
                                       timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        (args.output / 'revision-entry.log').write_text(direct_result.stdout + direct_result.stderr, encoding='utf-8')
        file_entry = subprocess.run(workflow_command[:-4] + ['-Page', 'localfiles'], capture_output=True,
                                    encoding='utf-8', errors='replace', timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        (args.output / 'file-entry.log').write_text(file_entry.stdout + file_entry.stderr, encoding='utf-8')
        schedule_entry = subprocess.run(workflow_command[:-4] + ['-Page', 'schedules'], capture_output=True,
                                        encoding='utf-8', errors='replace', timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        (args.output / 'schedule-entry.log').write_text(schedule_entry.stdout + schedule_entry.stderr, encoding='utf-8')
        with bridge.journal.connect(read_only=True) as db:
            revisions = db.execute("SELECT revision FROM tasks WHERE kind='revision_task'").fetchall()
            file_tasks = db.execute("SELECT local_files FROM tasks WHERE kind='local_file_task'").fetchall()
        preserved = (len(revisions) == 1 and
                     Path(json.loads(revisions[0][0])['files'][0]['path']).read_bytes() == product.read_bytes())
        files_preserved = (len(file_tasks) == 2 and all(
            Path(json.loads(r[0])['files'][0]['path']).read_bytes() == (fixture.root/'desktop-input.csv').read_bytes()
            for r in file_tasks))
        history_revision_command = [str(shell), '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                                    '-File', str(ROOT / 'task_history.ps1'), '-Verify',
                                    '-SkipClipboardVerification', '-Root', str(fixture.root)]
        history_revision_result = subprocess.run(history_revision_command, capture_output=True, encoding='utf-8',
                                                errors='replace', timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        (args.output / 'revision-history.log').write_text(history_revision_result.stdout + history_revision_result.stderr, encoding='utf-8')
        for name in ('workflow-preview.png', 'workflow-results.png', 'workflow-batches.png', 'workflow-revision.png', 'workflow-files.png', 'workflow-schedules.png'):
            path = fixture.root / '.runtime' / name
            if path.exists():
                shutil.copy2(path, args.output / name)
        preference_runs = []
        for script in ('task_history.ps1', 'template_editor.ps1', 'workflow_window.ps1', 'tray_app.ps1'):
            for mode in ('save', 'read'):
                command = [str(shell), '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                           '-File', str(ROOT / script), '-PreferenceProbe', mode,
                           '-PreferenceRoot' if script == 'tray_app.ps1' else '-Root', str(fixture.root)]
                probe = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                       timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
                preference_runs.append(dict(script=script, mode=mode, exit_code=probe.returncode))
                (args.output / (script + '-' + mode + '.log')).write_text(probe.stdout + probe.stderr, encoding='utf-8')
        proof = dict(exit_code=result.returncode, template_exit_code=template_result.returncode,
                     workflow_exit_code=workflow_result.returncode,
                     revision_entry_exit_code=direct_result.returncode, revision_count=len(revisions),
                     revision_history_exit_code=history_revision_result.returncode,
                     old_version_preserved=preserved,
                     file_entry_exit_code=file_entry.returncode, schedule_entry_exit_code=schedule_entry.returncode, local_file_task_count=len(file_tasks),
                     local_input_copies_preserved=files_preserved,
                     preference_runs=preference_runs,
                     template_screenshot=template_screenshot.exists(), runtime_errors=errors, transient_status_retries=status_retries,
                     model_called=bridge.runner.run.called, transport_called=bridge.client.send_text.called,
                     screenshot=screenshot.exists(), isolated=True)
        (args.output / 'proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
        print(result.stdout + result.stderr)
        print(template_result.stdout + template_result.stderr)
        print(workflow_result.stdout + workflow_result.stderr)
        print(direct_result.stdout + direct_result.stderr)
        print(file_entry.stdout + file_entry.stderr)
        print(schedule_entry.stdout + schedule_entry.stderr)
        print(history_revision_result.stdout + history_revision_result.stderr)
        print(json.dumps(proof, ensure_ascii=False))
        return int(bool(result.returncode or template_result.returncode or workflow_result.returncode or direct_result.returncode or file_entry.returncode or schedule_entry.returncode or history_revision_result.returncode or not preserved or not files_preserved or any(r['exit_code'] for r in preference_runs) or errors or proof['model_called'] or proof['transport_called'] or not proof['screenshot'] or not proof['template_screenshot']))
    finally:
        stop.set()
        if thread:
            thread.join(3)
        fixture.doCleanups()


if __name__ == '__main__':
    raise SystemExit(main())
