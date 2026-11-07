"""Runtime-owned template editing; never starts tasks or reads model results as instructions."""
import json
import os
import re

from task_templates import TaskTemplateStore, TemplateError, validate_snapshot
from task_journal import TaskJournal
from bridge_templates import template_output_dir


def template_action(bridge, request):
    try:
        with bridge._desktop_lock:
            store = getattr(bridge, 'template_store', None)
            journal = getattr(bridge, 'journal', None)
            if bridge._desktop_stop.is_set():
                raise TemplateError('后台正在停止，请稍后重新读取。')
            if not isinstance(store, TaskTemplateStore) or not isinstance(journal, TaskJournal):
                raise TemplateError('任务模板记录未启用。')
            path = request.get('journal_path')
            if not isinstance(path, str) or os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.realpath(journal.path)):
                raise TemplateError('任务库来源已变化，请重新打开模板窗口。')
            users = sorted(bridge.allowed)
            if not users:
                raise TemplateError('尚未配置微信用户，无法保存模板。')
            action = request.get('operation')
            uid = request.get('uid') or users[0]
            if uid not in users:
                raise TemplateError('该用户不在当前白名单中。')
            result = {'ok': True, 'uid': uid, 'users': users}
            if action == 'view':
                result.update(templates=store.list(uid), message='已读取任务模板。')
            elif action == 'save':
                definition = request.get('definition')
                if not isinstance(definition, dict):
                    raise TemplateError('请填写模板内容。')
                template_output_dir(definition.get('output_dir', ''), bridge.workdir)
                if type(request.get('expected_revision')) is not int:
                    raise TemplateError('缺少模板版本，请重新读取。')
                saved = store.save(uid, request.get('name'), definition,
                                   expected_revision=request['expected_revision'])
                result.update(template=saved, templates=store.list(uid), message='已保存模板，第 %d 版。' % saved['revision'])
            elif action == 'delete':
                if type(request.get('expected_revision')) is not int:
                    raise TemplateError('缺少模板版本，请重新读取。')
                store.delete(uid, request.get('name'), expected_revision=request['expected_revision'])
                result.update(templates=store.list(uid), message='已删除模板，已经排队的任务仍保留原版本。')
            elif action == 'seed':
                ident = request.get('task_id', '')
                if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
                    raise TemplateError('任务编号无效。')
                with journal.connect(timeout=.1, read_only=True) as db:
                    row = db.execute('SELECT uid,label,state,template,inputs FROM tasks WHERE id=?', (ident,)).fetchone()
                if not row or row[0] not in users:
                    raise TemplateError('未找到当前用户的任务。')
                uid, label, state, raw_template, raw_inputs = row
                if state in ('queued', 'running', 'waiting', 'recoverable'):
                    raise TemplateError('请任务结束后再保存为模板。')
                snapshot = json.loads(raw_template or '{}')
                if snapshot:
                    snapshot = validate_snapshot(snapshot, uid)
                    definition = dict(snapshot['definition'])
                    seed_name = (snapshot['name'][:36] + '副本')
                    note = '已复制本次任务使用的模板定义，请检查后另存。'
                else:
                    inputs = json.loads(raw_inputs or '{}')
                    records = inputs.get('files', []) if isinstance(inputs, dict) else []
                    filenames = [str(r.get('orig') or r.get('name') or '') for r in records[:12] if isinstance(r, dict)]
                    definition = dict(inputs=('参考本次材料：' + '、'.join(filenames))[:2000] if filenames else '',
                                      steps=str(label or '')[:8000], output_dir='工作目录', output_format='')
                    seed_name = ''
                    note = '普通任务只保留摘要，已填入处理步骤草稿；请补齐输入要求和输出格式，检查后保存。'
                result.update(uid=uid, draft=dict(name=seed_name, definition=definition),
                              templates=store.list(uid), message=note)
            else:
                raise TemplateError('不支持的模板操作。')
            return result
    except TemplateError as exc:
        return dict(ok=False, message=str(exc))
    except Exception:
        return dict(ok=False, message='模板记录暂不可用，修改未能确认，请重新读取后核对。')
