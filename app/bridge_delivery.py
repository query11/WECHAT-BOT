"""Send outside the desktop lock; retain only explicitly replayable result text."""
from contextlib import contextmanager
import logging
import threading

from ilink_client import ILinkClient, ILinkError
from task_journal import TaskJournal, RESOLUTIONS

log = logging.getLogger('bridge')


class DeliveryMixin:
    @contextmanager
    def _reply_scope(self):
        # Nested handle/enqueue calls share one list. No network I/O takes place
        # while the outer state mutation holds the desktop lock.
        with self._desktop_lock:
            local = getattr(self, '_reply_local', None)
            if local is None:
                local = self._reply_local = threading.local()
            nested = getattr(local, 'pending', None) is not None
            if not nested:
                local.pending, local.ready = [], []
        if nested:
            with self._desktop_lock:
                yield
            return
        try:
            with self._desktop_lock:
                yield
        finally:
            pending, ready = local.pending, local.ready
            del local.pending, local.ready
            try:
                for msg, text, options in pending:
                    try:
                        self.reply(msg, text, **options)
                    except Exception:
                        log.exception('通知发送失败；已接收任务与控制状态保持有效')
            finally:
                for event in ready:
                    event.set()

    def _delivery_write(self, reply_id, accepted, total, state):
        if reply_id is None:
            return
        try:
            self.journal.record_delivery(reply_id, accepted, total, state)
        except Exception as exc:
            log.warning('文字发送回执未能保存（%s）；补发前请检查微信已收到的部分', type(exc).__name__)

    def reply(self, msg, text, *, outcome=None, result=None, retained=None, after_send=None,
              include_result_command=False):
        local = getattr(self, '_reply_local', None)
        pending = getattr(local, 'pending', None)
        if pending is not None:
            pending.append((msg, text, dict(outcome=outcome, result=result,
                                           retained=retained, after_send=after_send,
                                           include_result_command=include_result_command)))
            return False  # Queued locally; no transport confirmation yet.
        worker = threading.current_thread() is self._worker
        is_result = worker if result is None else result
        if outcome is not None:
            msg._receipt_outcome = msg._execution_state = outcome
        reply_id = retained['id'] if retained else None
        accepted = retained['accepted_chunks'] if retained else 0
        total = retained['total_chunks'] if retained else 0
        chunk_size = retained.get('chunk_size', 1800) if retained else None
        if is_result:
            self._set_stage(msg, '正在发送任务回复', phase='send')
            # Chunk receipts must refer to the exact text retained for resend.
            text = self._mask(text)
            msg._result_summary = text[:1000]
            journal = getattr(self, 'journal', None)
            if isinstance(journal, TaskJournal) and getattr(msg, '_journal_id', None):
                try:
                    size = self.client.max_reply_chars if isinstance(self.client, ILinkClient) else 1800
                    saved_text = text
                    if include_result_command:
                        saved_text += '\n\n查看本次任务结果：\n/result %s' % msg._journal_id
                    reply_id = journal.prepare_reply(msg._journal_id, saved_text,
                        getattr(msg, '_execution_state', getattr(msg, '_receipt_outcome', 'completed')), size)
                    if reply_id is not None:
                        # Retain the exact command-bearing text for chunk receipts
                        # and resend; never advertise an unsaved full result.
                        text = saved_text
                except Exception as exc:
                    log.warning('完整结果未能缓存（%s）；本次文字仍会尝试发送', type(exc).__name__)
                    text += '\n⚠️ 本次完整结果缓存未能保存，无法保证之后补发；请保留收到的文字。'
        try:
            if isinstance(self.client, ILinkClient) and reply_id is not None:
                def on_chunk(index, count):
                    nonlocal accepted, total
                    accepted, total = index, count
                    self._delivery_write(reply_id, accepted, total, 'accepted' if index == count else 'pending')
                total = self.client.send_text(msg.from_user_id, text, msg.context_token,
                    start_chunk=accepted, chunk_size=chunk_size, on_chunk=on_chunk)
                accepted = total
            else:
                self.client.send_text(msg.from_user_id, text, msg.context_token)
                accepted = total = 1
            self._delivery_write(reply_id, accepted, total, 'accepted')
            if after_send is not None:
                after_send()
            return True
        except ILinkError as exc:
            if is_result:
                # Delivery uncertainty must not overwrite the known execution
                # status. The legacy combined state remains conservative.
                if not hasattr(msg, '_execution_state'):
                    msg._execution_state = getattr(msg, '_receipt_outcome', 'completed')
                msg._receipt_outcome = 'uncertain'
            self._delivery_write(reply_id, accepted, total, 'unconfirmed')
            log.error('回消息失败：%s', self._mask(str(exc)))
            return False

    def _history_command(self, msg, text):
        parts = text.split()
        command = parts[0].lower() if parts else ''
        if command not in ('/result', '/resend', '/resolve'):
            return False
        journal = getattr(self, 'journal', None)
        if not isinstance(journal, TaskJournal):
            self.reply(msg, '🔕 任务记录未启用，无法查看、补发或结清记录。')
            return True
        if len(parts) != (3 if command == '/resolve' else 2):
            self.reply(msg, '📋 用法：/result 编号；/resend 编号；/resolve 编号 checked（已核对）|abandoned（放弃恢复）|archived（归档）。')
            return True
        uid, ident = msg.from_user_id, parts[1]
        try:
            if command == '/resolve':
                action = parts[2].lower()
                if action not in RESOLUTIONS:
                    self.reply(msg, '🔕 结清操作仅支持 checked、abandoned 或 archived。')
                elif journal.resolve(uid, ident, action):
                    self.reply(msg, '✅ ' + RESOLUTIONS[action] + '。原始任务状态仍保留，记录不会自动重跑。')
                else:
                    self.reply(msg, '🔕 未找到可执行该操作的记录；只能操作自己的任务，执行中或已结清的任务不能改为另一种结清状态。')
                return True
            entries = journal.replies(uid, ident, only_pending=command == '/resend')
            if entries is None:
                self.reply(msg, '🔕 未找到自己的已结束任务；执行中或等待确认的任务暂不能补发。')
            elif not entries:
                self.reply(msg, '📋 没有可补发的文字：结果可能已全部获接口确认、尚未保存或已超过 7 天保留期。附件仍需单独检查。')
            elif command == '/result':
                self.reply(msg, '📋 保存的任务结果（文字；附件请查看原消息）：\n' + '\n\n'.join(r['body'] for r in entries), result=False)
            else:
                self.reply(msg, '📋 正在补发未获确认的文字部分。最后一次未确认的部分可能重复，请核对手机；任务和附件保持原来的执行结果。')
                for entry in entries:
                    self.reply(msg, entry['body'], result=False, retained=entry)
        except Exception as exc:
            log.warning('任务结果操作暂不可用（%s）', type(exc).__name__)
            self.reply(msg, '⚠️ 任务记录暂时无法读取或保存，请稍后重试；本次没有重新执行任务。')
        return True
