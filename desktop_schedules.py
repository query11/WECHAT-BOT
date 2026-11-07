"""Desktop schedule plans and read-only links to actual task receipts."""
import math
import time
import uuid

from remind import ReminderStore, ScheduleError, MAX_TEXT, MAX_AHEAD, _next_daily

STATES = dict(dispatching='提交中', uncertain='待核对', queued='排队中', running='处理中',
              completed='处理结束', failed='执行失败', cancelled='已取消', interrupted='已中断',
              recoverable='待恢复', waiting='等待确认', sent='提醒已发送', unconfirmed='发送未确认',
              expired='已过期', skipped='未执行', retry='等待重试', rejected='未入队')


def stamp(value):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value))


def schedule_store(bridge):
    store = getattr(bridge, 'reminders', None)
    if not isinstance(store, ReminderStore):
        raise ScheduleError('当前未启用定时提醒，请启用提醒功能并重启后台。')
    if store.load_error:
        raise ScheduleError('提醒文件不可读，请检查本机日志和文件后重新读取。')
    return store


def schedule_view(bridge, uid):
    try:
        store = schedule_store(bridge)
        items, history = store.snapshot(uid)
    except ScheduleError as exc:
        return dict(enabled=False, message=str(exc), items=[], history=[])
    # Query by occurrence as well as task ID, so a lost post-enqueue history write
    # still resolves to the receipt that was durably created by _enqueue.
    history.reverse()
    warning = ''
    try:
        with bridge.journal.connect(timeout=.1, read_only=True) as db:
            for row in history:
                if row['kind'] != 'task':
                    continue
                task = db.execute('SELECT id,state,delivery_state,summary FROM tasks WHERE uid=? '
                                  'AND (id=? OR message_id=?) ORDER BY updated DESC LIMIT 1',
                                  (uid, row.get('task_id', ''), 'schedule-'+row['id'])).fetchone()
                if task:
                    row.update(task_id=task[0], state=task[1], delivery=task[2], detail=task[3] or '请打开任务记录查看本次执行。')
    except Exception:
        warning = '任务状态暂不可读，下方保留最近一次触发记录。'
    for row in history:
        row.update(state_label=STATES.get(row['state'], '待核对'),
                   scheduled_label=stamp(row['scheduled_at']), fired_label=stamp(row['fired_at']))
        if row.get('delivery') in ('pending', 'unconfirmed'):
            row['detail'] += '；结果发送未确认。'
    latest = {}
    for row in history:
        latest.setdefault(row['schedule_id'], row)
    for item in items:
        item.update(version=store.version(item), next_label=stamp(item['at']),
                    kind_label='自动任务' if item['kind'] == 'task' else '微信提醒',
                    state_label='已暂停' if item['paused'] else '已启用', latest=latest.get(item['id']))
    return dict(enabled=True, message=warning, items=items, history=history, limit=store.max_pending)


def validate_plan(raw, now=None):
    now = time.time() if now is None else now
    if not isinstance(raw, dict) or raw.get('kind') not in ('remind', 'task'):
        raise ScheduleError('请选择微信提醒或自动任务。')
    body = raw.get('text')
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_TEXT:
        raise ScheduleError('请输入 1～%d 字的提醒内容或任务要求。' % MAX_TEXT)
    repeat = raw.get('repeat')
    if repeat == 'daily':
        hour, minute = raw.get('hour'), raw.get('minute')
        if type(hour) is not int or type(minute) is not int or not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ScheduleError('每天执行的时刻无效。')
        daily = dict(hour=hour, minute=minute)
        at = _next_daily(hour, minute, now+5, slack=0)
        label = '每天 %02d:%02d' % (hour, minute)
    elif repeat == 'once':
        at = raw.get('at')
        if type(at) not in (int, float) or not math.isfinite(at) or not (5 < at-now <= MAX_AHEAD):
            raise ScheduleError('一次性计划请选择至少 5 秒后、7 天以内的时间。')
        daily, label = None, stamp(at)
    else:
        raise ScheduleError('请选择一次性或每天重复。')
    return dict(kind=raw['kind'], text=body.strip(), at=at, label=label, daily=daily)


def schedule_action(bridge, uid, request):
    store = schedule_store(bridge)
    action = request['operation']
    now = time.monotonic()
    plans = {k: v for k, v in getattr(bridge, '_schedule_plans', {}).items() if now-v['created'] < 900}
    bridge._schedule_plans = plans
    if action == 'schedule_preview':
        plan = validate_plan(request.get('plan'))
        ident, version = request.get('schedule_id'), request.get('expected_version')
        old = store.checked(uid, ident, version) if ident else None
        if len(plans) >= 30:
            raise ScheduleError('待确认计划较多，请稍后重新核对。')
        token = uuid.uuid4().hex
        plans[token] = dict(created=now, uid=uid, store=store, plan=plan, ident=ident, version=version)
        return dict(token=token, review=dict(plan, next_label=stamp(plan['at']), paused=bool(old and old['paused'])),
                    message='请核对时间、内容和微信用户，确认后保存计划。核对有效期为 15 分钟。')
    if action == 'schedule_save':
        token = request.get('token')
        draft = plans.get(token) if isinstance(token, str) else None
        if not draft or draft['uid'] != uid or draft['store'] is not store:
            raise ScheduleError('核对已过期或后台已变化，请重新核对计划。')
        if draft.get('response'):
            return draft['response']
        plan = draft['plan']
        if plan['at'] <= time.time()+5:
            raise ScheduleError('核对时的执行时间已临近或已过，请重新核对。')
        item = store.save_plan(uid, plan, draft['ident'], draft['version'])
        # Cache the outcome immediately after durable save, before reading views.
        draft['response'] = dict(schedule_id=item['id'], message='计划已保存。' + ('当前保持暂停。' if item['paused'] else '到点后自动触发。'))
        return draft['response']
    if action in ('schedule_pause', 'schedule_resume', 'schedule_delete'):
        store.manage(uid, request.get('schedule_id'), request.get('expected_version'), action.split('_')[1])
        message = {'schedule_pause': '计划已暂停，已入队或正在处理的任务不受影响。',
                   'schedule_resume': '计划已恢复，按下一次时间触发。',
                   'schedule_delete': '计划已删除，已有触发记录和任务结果仍保留。'}[action]
        return dict(message=message)
    raise ScheduleError('不支持的定时任务操作。')
