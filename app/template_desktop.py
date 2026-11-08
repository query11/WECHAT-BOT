"""JSON stdin client for the local runtime control channel; no database writes."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from bridge_runtime import atomic_json
from task_history import _source


def request_templates(root, request, *, workflow=False):
    source, error, _ = _source(root)
    capability = 'desktop_workflow' if workflow else 'template_editor'
    if error or not source or source.get('kind') != 'runtime' or not source.get(capability):
        return dict(ok=False, message='后台未运行或尚未加载模板管理，请启动或重启后台后重新读取。')
    if not source.get('enabled'):
        return dict(ok=False, message='任务记录未启用，无法管理模板。')
    operation = request.get('operation')
    allowed = ('view', 'pin', 'preview', 'run', 'seal', 'remove', 'dismiss', 'revision_source', 'revision_preview', 'revision_run', 'files_preview', 'files_run', 'schedule_preview', 'schedule_save', 'schedule_pause', 'schedule_resume', 'schedule_delete') if workflow else ('view', 'save', 'delete', 'seed')
    if operation not in allowed:
        return dict(ok=False, message='不支持的模板操作。')
    # The source is pinned by the initial read; mutations never silently target
    # another runtime or database after a restart/reconfiguration.
    if (operation != 'view' if workflow else operation in ('save', 'delete')) and request.get('source') != {'pid': source['pid'], 'path': source['path']}:
        return dict(ok=False, message='后台或任务库已经变化，请重新读取后再保存。')
    runtime = root / '.runtime'
    ident = uuid.uuid4().hex
    command = runtime / ('command-' + ident + '.json')
    reply = runtime / ('reply-' + ident + '.json')
    fields = ('operation', 'uid', 'name', 'definition', 'expected_revision', 'task_id')
    if workflow:
        fields += ('template_id', 'enabled', 'expected_version', 'batch_id', 'index', 'body', 'token', 'item_id', 'mode', 'indexes', 'paths', 'delivery', 'schedule_id', 'plan')
    action = {key: request[key] for key in fields if key in request}
    action.update(kind='workflow' if workflow else 'templates', journal_path=source['path'])
    atomic_json(command, dict(pid=source['pid'], at=time.time(), action=action))
    deadline = time.monotonic() + 23
    while time.monotonic() < deadline:
        try:
            response = json.loads(reply.read_text(encoding='utf-8-sig'))
        except (FileNotFoundError, PermissionError):
            time.sleep(.1)
            continue
        try:
            reply.unlink(missing_ok=True)
        except OSError:
            pass
        data = response.get('data') or dict(ok=False, message=response.get('message', '操作未获确认。'))
        data['source'] = {'pid': source['pid'], 'path': source['path']}
        return data
    return dict(ok=False, message='未获得后台确认，请重新读取并核对；本次操作不会自动重试。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        raw = sys.stdin.buffer.read(80 * 1024 + 1)
        if len(raw) > 80 * 1024:
            raise ValueError('too large')
        request = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(request, dict):
            raise ValueError('not an object')
        result = request_templates(args.root.resolve(), request)
    except Exception:
        result = dict(ok=False, message='读取或保存未能确认，请重新读取后核对。')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
