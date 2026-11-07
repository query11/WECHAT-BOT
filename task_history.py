"""Read-only, deliberately narrow desktop view of task receipts."""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from task_history_path import DEFAULT_JOURNAL_FILE, resolve_journal_path
from task_journal import RESULT_RETENTION_SECONDS, decode_result_checks


def safe_text(value):
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏密钥]", str(value or ""))


def _process_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 0xFFFFFFFF:
        return False
    if os.name == 'nt':
        # os.kill(pid, 0) is not a portable Windows liveness probe.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not evidence that the writer has stopped.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _source(root):
    """Only inspect path metadata, never load/decrypt the application's config."""
    status_path = root / '.runtime' / 'status.json'
    try:
        status = json.loads(status_path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        status = None
    except (OSError, ValueError, UnicodeError):
        return None, 'source_unknown', '后台状态文件暂不可读，无法确认正在使用的任务库，请稍后刷新。'
    if status is not None and not isinstance(status, dict):
        return None, 'source_unknown', '后台状态格式无效，无法确认正在使用的任务库。'
    if status and status.get('state') != 'stopped' and _process_alive(status.get('pid')):
        heartbeat = status.get('heartbeat')
        if (not isinstance(heartbeat, (int, float)) or not math.isfinite(heartbeat)
                or abs(time.time() - heartbeat) > 8):
            # A stale PID can belong to a newer process. Do not guess either source.
            return None, 'source_unknown', '后台状态暂未刷新，无法确认实际任务库来源；请稍后刷新。'
        raw = status.get('journal_source')
        if (not isinstance(raw, dict) or not isinstance(raw.get('enabled'), bool)
                or (raw['enabled'] and (not isinstance(raw.get('path'), str)
                                       or not raw['path'] or not Path(raw['path']).is_absolute()))):
            return None, 'source_unknown', '后台尚未报告实际任务库路径；请重启后台加载新版后查看。'
        try:
            path = resolve_journal_path(root, raw.get('path')) if raw['enabled'] else None
        except (OSError, ValueError):
            return None, 'source_unknown', '后台报告的任务库路径无效，无法读取历史。'
        actions = status.get('history_actions')
        actions = [a for a in actions if a in ('result','resend','resolve','artifacts','getfile')] if isinstance(actions, list) else []
        direct = status.get('history_direct_actions')
        direct = [a for a in direct if a in ('cancel_queued', 'resume', 'checked', 'abandoned', 'archived')] if isinstance(direct, list) else []
        return dict(kind='runtime', enabled=bool(raw['enabled']), path=str(path) if path else '', actions=actions,
                    direct_actions=direct, pid=status['pid'], runtime_state=status.get('state'),
                    template_editor=status.get('template_editor') is True,
                    desktop_workflow=status.get('desktop_workflow') is True,
                    note='后台实际加载的任务库'), None, ''
    try:
        config = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        # Preserve read_history(root)'s original fixture/offline API, visibly labelled.
        value, kind, note = DEFAULT_JOURNAL_FILE, 'default', '未找到配置；仅查看默认位置的离线历史'
    except (OSError, ValueError, UnicodeError):
        return None, 'config_error', '任务库路径配置无法读取；没有改用其他数据库。'
    else:
        if not isinstance(config, dict):
            return None, 'config_error', '任务库路径配置格式无效；没有改用其他数据库。'
        value, kind, note = config.get('task_journal_file'), 'config', '磁盘配置指定的任务库（后台未运行）'
    try:
        path = resolve_journal_path(root, value)
    except (OSError, ValueError, TypeError):
        return None, 'config_error', '任务库路径配置无效；没有改用其他数据库。'
    return dict(kind=kind, enabled=path is not None, path=str(path) if path else '', actions=[], note=note), None, ''


def _read_rows(path, limit, uid=None, include_replies=True, ident=None):
    # Do not instantiate TaskJournal: startup recovery would mutate live tasks.
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.25)
    try:
        db.execute('PRAGMA query_only=ON')
        columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
        if not {'id', 'label', 'kind', 'state', 'updated'}.issubset(columns):
            raise ValueError('task history schema is incompatible')
        missing = [name for name in ('summary', 'outputs') if name not in columns]
        names = ('summary', 'outputs', 'execution_state', 'delivery_state', 'resolution', 'resolved_at', 'artifacts', 'inputs', 'template', 'result_checks', 'revision', 'local_files')
        extras = ','.join(name if name in columns else ("0" if name == 'resolved_at' else "''") + ' AS ' + name
                          for name in names)
        clauses, params = [], ()
        for column, value in (('uid', uid), ('id', ident)):
            if value is not None:
                clauses.append(column + '=?')
                params += (value,)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = db.execute('SELECT id,label,kind,state,updated,' + extras +
                          ' FROM tasks' + where + ' ORDER BY updated DESC, id DESC LIMIT ?',
                          params + (max(0, min(int(limit), 200)),)).fetchall()
        result = []
        has_replies = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_replies'").fetchone()
        for ident, label, kind, state, updated, summary, raw, execution, delivery, resolution, resolved_at, raw_artifacts, raw_inputs, raw_template, raw_checks, raw_revision, raw_local_files in rows:
            try:
                outputs = json.loads(raw or '[]')
            except (ValueError, TypeError):
                outputs = []
            if not isinstance(outputs, list):
                outputs = []
            try:
                artifacts = json.loads(raw_artifacts or '[]')
            except (ValueError, TypeError):
                artifacts = []
            if not isinstance(artifacts, list):
                artifacts = []
            artifacts = [{key: safe_text(value) if isinstance(value, str) else value
                          for key, value in record.items()
                          if key in ('path', 'name', 'label', 'status', 'reason', 'size', 'format')}
                         for record in artifacts[:21] if isinstance(record, dict)]
            try:
                inputs = json.loads(raw_inputs or '{}')
            except (ValueError, TypeError):
                inputs = {}
            input_rows = inputs.get('files', []) if isinstance(inputs, dict) else []
            input_rows = [{key: safe_text(value) if isinstance(value, str) else value
                           for key, value in record.items() if key in ('path', 'name', 'orig', 'index', 'role')}
                          for record in input_rows[:12] if isinstance(record, dict)] if isinstance(input_rows, list) else []
            try:
                template = json.loads(raw_template or '{}')
            except (ValueError, TypeError):
                template = {}
            definition = template.get('definition', {}) if isinstance(template, dict) else {}
            if not isinstance(definition, dict):
                definition = {}
            template = (dict(name=safe_text(template.get('name'))[:40], revision=template.get('revision'),
                             definition={key: safe_text(definition.get(key))[:limit] for key, limit in
                                         (('inputs', 2000), ('steps', 8000), ('output_dir', 2000), ('output_format', 1000), ('checks', 4000))})
                        if isinstance(template, dict) and isinstance(template.get('name'), str)
                        and isinstance(template.get('revision'), int) and not isinstance(template.get('revision'), bool)
                        and template['revision'] > 0 else {})
            checks = decode_result_checks(raw_checks or '')
            checks = [{key: safe_text(record.get(key))[:limit] for key, limit in
                       (('file', 255), ('kind', 40), ('condition', 2000), ('status', 20), ('detail', 300))}
                      for record in checks[:12] if isinstance(record, dict)] if isinstance(checks, list) else []
            # Paths are displayed locally and are never executed as commands.
            names = [safe_text(v) for v in outputs if isinstance(v,str)][:20]
            replies = (db.execute('SELECT body,state FROM task_replies WHERE task_id=? AND created>=? ORDER BY id',
                                  (ident, time.time() - RESULT_RETENTION_SECONDS)).fetchall() if has_replies and include_replies else [])
            has_result_text = bool(replies) if include_replies else bool(has_replies and db.execute(
                'SELECT 1 FROM task_replies WHERE task_id=? AND created>=? AND body!=? LIMIT 1',
                (ident, time.time()-RESULT_RETENTION_SECONDS, '')).fetchone())
            revision = {}
            if raw_revision:
                try:
                    from task_revisions import validate_revision
                    decoded = json.loads(raw_revision)
                    validated = validate_revision(decoded, decoded.get('uid'))
                    revision = {k: safe_text(validated[k]) for k in ('parent_id','root_id','source_label','instructions','output_dir')}
                    revision['files'] = [dict(name=safe_text(f.get('orig') or f['name']), path=safe_text(f['path'])) for f in validated['files']]
                except (ValueError, TypeError, AttributeError):
                    revision = dict(error='修改任务记录不可读，请核对实际文件。')
            local_files = {}
            if raw_local_files:
                try:
                    from desktop_file_tasks import validate_local_files, DELIVERY
                    decoded = json.loads(raw_local_files)
                    validated = validate_local_files(decoded, decoded.get('uid'))
                    local_files = dict(body=safe_text(validated['body']), output_dir=safe_text(validated['output_dir']),
                                       delivery=DELIVERY[validated['delivery']],
                                       files=[dict(name=safe_text(f.get('orig') or f['name']), path=safe_text(f['path'])) for f in validated['files']])
                except (ValueError, TypeError, AttributeError):
                    local_files = dict(error='本机文件任务记录不可读，请核对实际文件。')
            result.append(dict(id=safe_text(ident), label=safe_text(label),
                               kind=safe_text(kind), state=safe_text(state),
                               updated=updated, summary=safe_text(summary), outputs=names,
                               execution_state=safe_text(execution), delivery_state=safe_text(delivery),
                               resolution=safe_text(resolution), resolved_at=resolved_at, artifacts=artifacts,
                               inputs=input_rows,
                               template=template,
                               revision=revision, local_files=local_files, has_result_text=has_result_text,
                               result_checks=checks,
                               result_text='\n\n'.join(safe_text(r[0]) for r in replies),
                               resend_available=any(r[1] != 'accepted' for r in replies)))
        return result, missing
    finally:
        db.close()


def read_history_view(root, limit=200, task_id=None):
    """Return explicit source/availability without confusing a failure with no rows."""
    source, error, message = _source(Path(root).resolve())
    result = dict(state=error or 'ready', message=message, source=source, rows=[], missing_columns=[])
    if error:
        return result
    if not source['enabled']:
        result.update(state='disabled', message='任务记录已关闭。')
        return result
    path = Path(source['path'])
    try:
        path.stat()  # Unlike is_file(), distinguish permission errors from missing paths.
        if not path.is_file():
            raise OSError('journal path is not a file')
        result['rows'], result['missing_columns'] = _read_rows(path, limit)
        if task_id and not any(r['id'] == task_id for r in result['rows']):
            focused, _ = _read_rows(path, 1, ident=task_id)
            result['rows'] = focused + result['rows']
    except FileNotFoundError:
        result.update(state='missing', message='指定的任务库尚不存在；没有创建或改用其他数据库。')
    except ValueError:
        result.update(state='schema_error', message='任务库结构不兼容，无法显示记录；查看器没有修改数据库。')
    except (sqlite3.Error, OSError):
        result.update(state='unavailable', message='指定的任务库暂不可读或正被锁定，请稍后刷新。')
    else:
        result['message'] = '暂无任务记录。' if not result['rows'] else '共 %d 条 · 时间为本地时间' % len(result['rows'])
        if result['missing_columns']:
            result['message'] += ' · 旧记录库缺少详细结果字段，已只读兼容显示'
        if result['rows'] and not source['actions']:
            result['message'] += ' · 查看/补发/结清指令需后台加载新版后使用'
    return result


def read_history(root, limit=200):
    """Compatibility list API; desktop callers use read_history_view for status."""
    result = read_history_view(root, limit)
    if result['state'] not in ('ready', 'disabled', 'missing'):
        raise sqlite3.DatabaseError(result['message'])
    return result['rows']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--view', action='store_true', help='Include source and availability metadata')
    parser.add_argument('--task', help='Include this receipt even when older than the recent list')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        print(json.dumps(read_history_view(args.root, task_id=args.task) if args.view else read_history(args.root), ensure_ascii=False))
    except (sqlite3.Error, OSError, ValueError):
        print('{"error":"无法读取任务历史，请稍后刷新。"}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
