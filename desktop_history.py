"""Narrow desktop task actions using the running bridge and its actual journal."""
import os
import re
import uuid

from ilink_client import Message
from task_journal import TaskJournal

DIRECT_ACTIONS = ('cancel_queued', 'resume', 'checked', 'abandoned', 'archived')


def history_action(bridge, request):
    operation, ident = request.get('operation'), request.get('task_id')
    if operation not in DIRECT_ACTIONS or not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
        return '不支持的任务操作，请刷新任务记录。'
    journal = getattr(bridge, 'journal', None)
    path = request.get('journal_path')
    if not isinstance(journal, TaskJournal) or not isinstance(path, str) or not path:
        return '任务记录未启用或来源未知，请刷新。'
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.realpath(journal.path)):
        return '任务库来源已变化，请刷新后重新操作。'
    try:
        # Flush any acceptance notice only after releasing the desktop lock.
        with bridge._reply_scope():
            with bridge._desktop_lock:
                if bridge._desktop_stop.is_set():
                    return '后台正在停止，请稍后操作。'
                with journal.connect(timeout=.1, read_only=True) as db:
                    row = db.execute('SELECT uid,state,resolution FROM tasks WHERE id=?', (ident,)).fetchone()
                if not row or row[0] not in bridge.allowed:
                    return '未找到当前白名单用户的任务，操作未执行。'
                uid, state, resolution = row
                if operation == 'cancel_queued':
                    return bridge._admin_queue_manage(uid, 'cancel', ident, None)
                if operation == 'resume':
                    if state != 'recoverable' or resolution:
                        return '任务已变化或不再可恢复，请刷新。'
                    if uid in bridge._draining or (bridge.standby_enabled and uid not in bridge._awake):
                        return '请先在微信说“开始对话”，再恢复这条任务。'
                    context = bridge._last_ctx.get(uid)
                    if not context or context[0] != uid or not context[1]:
                        return '本次启动后尚无该用户的微信回复通道，请先在微信说“开始对话”。'
                    msg = Message(dict(from_user_id=uid, message_id='desktop-' + uuid.uuid4().hex,
                                       context_token=context[1], item_list=[]))
                    msg._desktop_origin = True
                    answer = bridge._enqueue((msg, '', None), msg, recovery_id=ident)
                    return answer or '已将未开始的任务重新加入队列，结果仍发到该用户微信。'
                if journal.resolve(uid, ident, operation):
                    return {'checked': '已标记为人工核对', 'abandoned': '已放弃恢复', 'archived': '已归档'}[operation] + '，原始任务状态保留。'
                return '任务状态已变化，操作未执行，请刷新。'
    except Exception:
        return '任务操作未能确认，请刷新记录核对当前状态后再操作。'
