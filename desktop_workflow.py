"""Reviewed desktop launches, pinned templates and attachment checklist actions."""
import copy
import json
import os
from pathlib import Path
import time
import uuid

from attachment_batches import AttachmentBatchStore, BatchError
from bridge_templates import template_output_dir
from ilink_client import Message
from task_history import _read_rows, safe_text
from task_journal import TaskJournal, decode_result_checks
from task_templates import TaskTemplateStore, TemplateError, validate_snapshot
from task_revisions import RevisionError, revision_source, preview_revision, preserve_revision
from desktop_file_tasks import (LocalFileError, DELIVERY, preview_local_files,
                                preserve_local_files, local_delivery_reason)
from desktop_schedules import schedule_view, schedule_action
from remind import ScheduleError

TERMINAL = {'completed', 'failed', 'cancelled', 'uncertain', 'interrupted'}


def launch_reason(bridge, uid):
    if bridge._desktop_stop.is_set():
        return '后台正在停止，请稍后重新读取。'
    if uid in bridge._draining or (bridge.standby_enabled and uid not in bridge._awake):
        return '请先在微信说“开始对话”，再确认运行。'
    context = bridge._last_ctx.get(uid)
    if not context or context[0] != uid or not context[1]:
        return '本次启动后尚无微信回复通道，请先在微信说“开始对话”。'
    if getattr(bridge, '_receiving_blocked', False):
        return '任务接收暂缓，请检查本机存储后再试。'
    return ''


def result_card(row):
    checks = row.get('result_checks') or []
    if any(c.get('status') == 'failed' for c in checks):
        check = '结果检查未通过，请核对文件内容'
    elif any(c.get('status') != 'passed' for c in checks):
        check = '部分结果未能检查，请人工核对'
    elif checks:
        check = '已配置的结果检查通过'
    else:
        check = '未配置结果检查'
    labels = {'completed': '处理结束', 'failed': '执行失败', 'cancelled': '已取消',
              'uncertain': '结果不确定', 'interrupted': '已中断'}
    files, seen = [], set()
    for item in row.get('artifacts', []):
        path = item.get('path')
        if path and path not in seen:
            files.append(dict(path=path, name=item.get('label') or item.get('name') or Path(path).name,
                              note=item.get('reason') or '记录时的文件核验信息'))
            seen.add(path)
    for path in row.get('outputs', []):
        if path and path not in seen:
            files.append(dict(path=path, name=Path(path).name, note='关联路径，未登记为已核验产物'))
            seen.add(path)
    tone = ('error' if row['state'] == 'failed' else
            'warning' if row['state'] in ('uncertain', 'interrupted') or
            any(c.get('status') != 'passed' for c in checks) else 'neutral')
    action = ('查看失败原因' if row['state'] == 'failed' else
              '查看并核对' if tone == 'warning' else '查看完整回复')
    return dict(id=row['id'], label=row['label'], state=row['state'], updated=row['updated'],
                summary=row.get('summary') or row['label'],
                conclusion=labels.get(row['state'], row['state']) + ' · ' + check,
                tone=tone, action_label=action,
                can_revise=bool(row.get('has_result_text') or row.get('result_text') or
                                any(a.get('status') == 'valid' for a in row.get('artifacts', []))),
                revision=row.get('revision') or {},
                files=files[:20], result_text=row.get('result_text') or '没有仍在保留期内的完整回复。')


def attention_items(bridge):
    """Read all unresolved receipts, including protected tasks older than 200 rows.

    A task is one item even when several reasons apply. No reply bodies, tokens
    or task payloads are read into the status snapshot.
    """
    items = []
    with bridge.journal.connect(timeout=.1, read_only=True) as db:
        rows = db.execute("SELECT id,uid,label,state,updated,delivery_state,result_checks FROM tasks "
                          "WHERE COALESCE(resolution,'')='' AND state NOT IN ('queued','running','waiting')")
        for ident, uid, label, state, updated, delivery, raw_checks in rows:
            if uid not in bridge.allowed:
                continue
            reasons = []
            if state == 'recoverable':
                reasons.append('可手动恢复')
            elif state in ('uncertain', 'interrupted'):
                reasons.append('结果待核对')
            elif state == 'failed':
                reasons.append('执行失败')
            if delivery in ('unconfirmed', 'pending'):
                reasons.append('发送未确认')
            checks = decode_result_checks(raw_checks or '')
            if any(c.get('status') != 'passed' for c in checks):
                reasons.append('检查待核对')
            if reasons:
                items.append(dict(id=ident, uid=uid, kind='task', title=safe_text(label),
                                  detail=' · '.join(reasons), updated=updated,
                                  action_label='查看任务记录'))
        # Query open batches directly: older unresolved batches must not fall
        # out of the normal recent-ten catalog.
        batches = db.execute("SELECT id,uid FROM attachment_batches WHERE state='open'").fetchall()
    for ident, uid in batches:
        if uid not in bridge.allowed:
            continue
        batch = bridge.batch_store.get(uid, ident)
        if not batch or batch['state'] != 'open':
            continue
        if batch['failed']:
            detail = '附件缺失待处理'
        elif batch['files'] and not batch['pending']:
            detail = '附件清单待确认'
        else:
            continue
        items.append(dict(id=ident, uid=uid, kind='batch', title=safe_text(batch['name']),
                          detail=detail, updated=batch['updated'], action_label='核对附件'))
    return sorted(items, key=lambda item: (-item['updated'], item['id']))


def user_view(bridge, uid, focus_id=None):
    templates = bridge.template_store.list(uid)
    pins = bridge.template_store.pinned(uid)
    batches = bridge.batch_store.list(uid)
    if focus_id and not any(b['id'] == focus_id for b in batches):
        focused = bridge.batch_store.get(uid, focus_id)
        if focused:
            batches.insert(0, focused)
    for batch in batches:
        batch['version'] = bridge.batch_store.desktop_version(batch)
    rows, _ = _read_rows(Path(bridge.journal.path), 200, uid=uid)
    if focus_id and not any(r['id'] == focus_id for r in rows):
        focused, _ = _read_rows(Path(bridge.journal.path), 1, uid=uid, ident=focus_id)
        rows = focused + rows
    results = [result_card(row) for row in rows if row['state'] in TERMINAL]
    focused = next((r for r in results if r['id'] == focus_id), None)
    results = ([focused] + [r for r in results if r['id'] != focus_id]) if focused else results
    return dict(templates=templates, pins=pins, batches=batches,
                results=results[:30], schedules=schedule_view(bridge, uid),
                file_delivery=dict(enabled=bool(bridge.send_file_enable), limit=bridge.send_file_limit,
                                   max_mb=bridge.send_file_max_mb),
                launch_reason=launch_reason(bridge, uid))


def overview(bridge):
    """Small read-only dashboard; a storage error never becomes a connection error."""
    try:
        store, journal = bridge.template_store, bridge.journal
        if not isinstance(store, TaskTemplateStore) or not isinstance(journal, TaskJournal):
            return dict(favorites=[], result=None, attention=None, message='任务记录未启用')
        favorites = []
        for uid in sorted(bridge.allowed):
            catalog = {t['id']: t for t in store.list(uid)}
            for ident in store.pinned(uid):
                t = catalog[ident]
                favorites.append(dict(id=ident, uid=uid, name=t['name'], revision=t['revision']))
        rows, _ = _read_rows(Path(journal.path), 200, include_replies=False)
        # Old users removed from the whitelist must not appear in this dashboard.
        with journal.connect(timeout=.1, read_only=True) as db:
            owners = dict(db.execute('SELECT id,uid FROM tasks'))
        row = next((r for r in rows if r['state'] in TERMINAL and r.get('resolution') != 'archived'
                    and owners.get(r['id']) in bridge.allowed), None)
        card = result_card(row) if row else None
        if card:
            card.pop('result_text', None)
        attention = attention_items(bridge)
        return dict(favorites=favorites[:50], result=card,
                    attention=dict(total=len(attention), items=attention[:100]), message='')
    except Exception:
        return dict(favorites=[], result=None, attention=None, message='任务记录暂不可读，请稍后刷新')


def workflow_action(bridge, request):
    try:
        with bridge._reply_scope():
            with bridge._desktop_lock:
                if bridge._desktop_stop.is_set():
                    raise TemplateError('后台正在停止，请稍后重新读取。')
                journal = getattr(bridge, 'journal', None)
                if (not isinstance(journal, TaskJournal)
                        or not isinstance(getattr(bridge, 'template_store', None), TaskTemplateStore)
                        or not isinstance(getattr(bridge, 'batch_store', None), AttachmentBatchStore)):
                    raise TemplateError('任务记录未启用，无法使用快捷任务。')
                path = request.get('journal_path')
                if not isinstance(path, str) or os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.realpath(journal.path)):
                    raise TemplateError('任务库来源已变化，请重新打开窗口。')
                users = sorted(bridge.allowed)
                action = request.get('operation')
                uid = request.get('uid')
                focus_id = request.get('item_id')
                if focus_id is not None and (not isinstance(focus_id, str) or len(focus_id) != 32
                                             or any(c not in '0123456789abcdef' for c in focus_id)):
                    raise TemplateError('记录编号无效，请重新打开入口。')
                if action == 'view' and not uid:
                    uid = users[0] if users else None
                    if request.get('template_id'):
                        with journal.connect(timeout=.1, read_only=True) as db:
                            row = db.execute('SELECT uid FROM task_templates WHERE id=?', (request['template_id'],)).fetchone()
                        uid = row[0] if row else uid
                    if focus_id:
                        with journal.connect(timeout=.1, read_only=True) as db:
                            row = db.execute('SELECT uid FROM tasks WHERE id=? UNION ALL '
                                             'SELECT uid FROM attachment_batches WHERE id=?',
                                             (focus_id, focus_id)).fetchone()
                        if not row:
                            raise TemplateError('这条记录已不可用，请刷新运行中心。')
                        uid = row[0]
                if not isinstance(uid, str) or uid not in users:
                    raise TemplateError('请选择当前白名单中的微信用户。')
                result = dict(ok=True, uid=uid, users=users)
                store = bridge.template_store
                if action == 'view':
                    result.update(user_view(bridge, uid, focus_id), message='已读取快捷任务。')
                elif isinstance(action, str) and action.startswith('schedule_'):
                    result.update(schedule_action(bridge, uid, request))
                    if action != 'schedule_preview':
                        result['schedules'] = schedule_view(bridge, uid)
                elif action == 'files_preview':
                    plan = preview_local_files(bridge, uid, request.get('paths'), request.get('body'),
                                               request.get('delivery'), request.get('name'), request.get('expected_revision'))
                    now = time.monotonic()
                    plans = {k: v for k, v in getattr(bridge, '_file_plans', {}).items() if now-v['at'] < 900}
                    if len(plans) >= 30:
                        raise LocalFileError('待确认文件任务较多，请稍后重新核对。')
                    plan['at'] = now; plans[plan['id']] = plan; bridge._file_plans = plans
                    result.update(token=plan['id'], review=dict(files=[dict(name=r['orig'], path=r['path'], size=r['size']) for r in plan['files']],
                                  body=plan['body'], output_dir=plan['output_dir'], template=plan['template'],
                                  delivery=plan['delivery'], delivery_label=DELIVERY[plan['delivery']],
                                  send_limit=bridge.send_file_limit, send_max_mb=bridge.send_file_max_mb),
                                  launch_reason=launch_reason(bridge, uid) or local_delivery_reason(bridge, plan['delivery']),
                                  message='核对有效期为 15 分钟。确认后保存输入副本并创建任务，结果摘要将发往所选微信用户。')
                elif action == 'files_run':
                    token = request.get('token')
                    plan = getattr(bridge, '_file_plans', {}).get(token) if isinstance(token, str) else None
                    if not plan or plan['uid'] != uid or time.monotonic()-plan['at'] >= 900:
                        raise LocalFileError('确认已过期或后台已重启，请重新核对。')
                    if plan.get('response'):
                        return plan['response']
                    reason = launch_reason(bridge, uid) or local_delivery_reason(bridge, plan['delivery'])
                    if reason:
                        raise LocalFileError(reason)
                    if bridge.queue.full():
                        raise LocalFileError('任务队列已满，请稍后再确认；本次尚未保存副本或提交任务。')
                    plan['response'] = dict(ok=False, message='提交未能确认，请查看任务记录；不会自动重复提交。')
                    snapshot = preserve_local_files(bridge, plan)
                    msg = Message(dict(from_user_id=uid, message_id='desktop-files-'+token,
                                       context_token=bridge._last_ctx[uid][1], item_list=[]))
                    msg._desktop_origin = True
                    answer = bridge._enqueue((msg, plan['body'], {'local_files': snapshot}), msg)
                    plan['response'] = dict(ok=not bool(answer), uid=uid, users=users,
                                            task_id=getattr(msg, '_journal_id', ''),
                                            message=answer or ('文件任务已加入队列，输入副本已保存。' + DELIVERY[plan['delivery']] + '。'))
                    return plan['response']
                elif action == 'revision_source':
                    result.update(revision_source=revision_source(bridge, uid, request.get('task_id')),
                                  message='选择修改对象并填写本次要求，再核对运行。')
                elif action == 'revision_preview':
                    plan = preview_revision(bridge, uid, request.get('task_id'), request.get('mode'),
                                            request.get('indexes'), request.get('body'))
                    now = time.monotonic()
                    plans = {k: v for k, v in getattr(bridge, '_revision_plans', {}).items() if now-v['at'] < 900}
                    if len(plans) >= 30:
                        raise RevisionError('待确认修改任务较多，请稍后重新核对。')
                    plan['at'] = now; plans[plan['id']] = plan; bridge._revision_plans = plans
                    result.update(token=plan['id'], review=dict(source_label=plan['source_label'],
                                  mode=plan['mode'], files=[r['orig'] for r in plan['files']],
                                  instructions=plan['instructions'], output_dir=plan['output_dir']),
                                  launch_reason=launch_reason(bridge, uid),
                                  message='确认后保存旧版本副本并创建新任务；核对结果在 15 分钟内有效。')
                elif action == 'revision_run':
                    token = request.get('token')
                    plan = getattr(bridge, '_revision_plans', {}).get(token) if isinstance(token, str) else None
                    if not plan or plan['uid'] != uid or time.monotonic()-plan['at'] >= 900:
                        raise RevisionError('确认已过期或后台已重启，请重新核对。')
                    if plan.get('response'):
                        return plan['response']
                    reason = launch_reason(bridge, uid)
                    if reason:
                        raise RevisionError(reason)
                    if bridge.queue.full():
                        raise RevisionError('任务队列已满，请稍后再确认；本次尚未保存副本或提交任务。')
                    plan['response'] = dict(ok=False, message='提交未能确认，请查看任务记录；不会自动重复提交。')
                    snapshot = preserve_revision(bridge, plan)
                    msg = Message(dict(from_user_id=uid, message_id='desktop-revision-'+token,
                                       context_token=bridge._last_ctx[uid][1], item_list=[]))
                    msg._desktop_origin = True
                    answer = bridge._enqueue((msg, plan['instructions'], {'revision': snapshot}), msg)
                    plan['response'] = dict(ok=not bool(answer), uid=uid, users=users,
                                            task_id=getattr(msg, '_journal_id', ''),
                                            message=answer or '修改任务已加入队列，旧版本副本已保存；新结果将发回所选微信用户。')
                    return plan['response']
                elif action == 'pin':
                    store.pin(uid, request.get('name'), request.get('enabled'), request.get('expected_revision'))
                    result.update(user_view(bridge, uid), message='已更新运行中心的常用模板。')
                elif action in ('remove', 'dismiss', 'seal'):
                    version = request.get('expected_version')
                    if not isinstance(version, str) or len(version) != 64:
                        raise BatchError('请先读取并核对附件清单。')
                    if action == 'seal':
                        bridge.batch_store.seal(uid, request.get('batch_id'), expected_version=version)
                        message = '清单已确认，可以选用这批附件运行模板。'
                    else:
                        bridge.batch_store.repair(uid, request.get('batch_id'), action, request.get('index'), expected_version=version)
                        message = '已更新清单，磁盘原文件和接收记录仍保留。'
                    result.update(user_view(bridge, uid), message=message)
                elif action == 'preview':
                    t = store.get(uid, request.get('name'))
                    if not t or type(request.get('expected_revision')) is not int or t['revision'] != request['expected_revision']:
                        raise TemplateError('模板已经变化，请重新读取后核对。')
                    t = validate_snapshot(t, uid)
                    t['definition']['output_dir'] = template_output_dir(t['definition']['output_dir'], bridge.workdir)
                    body = request.get('body', '')
                    if not isinstance(body, str) or len(body) > 8000:
                        raise TemplateError('本次补充要求最多 8000 个字。')
                    batch = None
                    if request.get('batch_id'):
                        batch = bridge.batch_store.get(uid, request['batch_id'])
                        if not batch or batch['state'] != 'ready' or batch['pending'] or batch['failed']:
                            raise BatchError('请先在附件页核对并确认清单，再选择这批附件。')
                    now = time.monotonic()
                    plans = getattr(bridge, '_desktop_plans', {})
                    plans = {key: value for key, value in plans.items() if now - value['at'] < 900}
                    if len(plans) >= 100:
                        raise TemplateError('待确认任务过多，请稍后再试。')
                    token = uuid.uuid4().hex
                    plans[token] = dict(at=now, uid=uid, template=t, batch=copy.deepcopy(batch), body=body,
                                        workdir=os.path.realpath(bridge.workdir))
                    bridge._desktop_plans = plans
                    result.update(token=token, template=t, batch=batch, body=body,
                                  launch_reason=launch_reason(bridge, uid),
                                  message='请核对模板版本、输入与输出位置；确认在 15 分钟内有效。')
                elif action == 'run':
                    token = request.get('token')
                    plan = getattr(bridge, '_desktop_plans', {}).get(token) if isinstance(token, str) else None
                    if not plan or plan['uid'] != uid or time.monotonic() - plan['at'] >= 900:
                        raise TemplateError('确认已经过期或后台已重启，请重新预览。')
                    if plan.get('response'):
                        return plan['response']
                    reason = launch_reason(bridge, uid)
                    if reason:
                        raise TemplateError(reason)
                    if os.path.normcase(os.path.realpath(bridge.workdir)) != os.path.normcase(plan['workdir']):
                        raise TemplateError('工作目录已经变化，请重新预览。')
                    template_output_dir(plan['template']['definition']['output_dir'], bridge.workdir)
                    job = {'template': copy.deepcopy(plan['template'])}
                    if plan['batch']:
                        batch = bridge.batch_store.get(uid, plan['batch']['id'])
                        if not batch or bridge.batch_store.desktop_version(batch) != bridge.batch_store.desktop_version(plan['batch']):
                            raise BatchError('附件批次已经变化或已被使用，请重新读取。')
                        job['batch'] = batch
                    # Mark uncertain before enqueue so an exception or lost response can never submit twice.
                    plan['response'] = dict(ok=False, message='提交未能确认，请查看任务记录后重新核对，不会自动重试。')
                    msg = Message(dict(from_user_id=uid, message_id='desktop-template-' + token,
                                       context_token=bridge._last_ctx[uid][1], item_list=[]))
                    msg._desktop_origin = True
                    answer = bridge._enqueue((msg, plan['body'], job), msg)
                    plan['response'] = dict(ok=not bool(answer), uid=uid, users=users,
                                            task_id=getattr(msg, '_journal_id', ''),
                                            message=answer or '已加入队列，任务结果将发送给所选微信用户。')
                    return plan['response']
                else:
                    raise TemplateError('不支持的快捷任务操作。')
                return result
    except (TemplateError, BatchError, RevisionError, LocalFileError, ScheduleError) as exc:
        return dict(ok=False, message=str(exc))
    except Exception:
        return dict(ok=False, message='操作未能确认，请刷新清单或任务记录后核对。')
