"""微信唤醒闸门：身份验证之后、所有业务分发之前运行。"""
import time
from bridge_text import parse_chat_switch
from bridge_admin import parse_admin


class StandbyMixin:
    def _standby_gate(self, msg, uid, text):
        if not self.standby_enabled:
            return False
        body = text.strip()
        if self.prefix and body.lower().startswith(self.prefix.lower()):
            body = body[len(self.prefix):].lstrip(" :：,，").strip()
        switch = parse_chat_switch(body)
        if switch == "on" or body.lower() in ("启动助手", "唤醒助手", "/wake"):
            self._draining.discard(uid)
            self._awake.add(uid)
            if self.chat_mode_enable:
                self._chat[uid] = time.time()
                self._chat_expired.discard(uid)
            self.reply(msg, "✅ 已唤醒助手并开始对话，直接发任务即可。说「结束对话」回到后台待机。")
            return True
        if switch == "off" or body.lower() in ("停止助手", "助手待机", "/standby"):
            self._awake.discard(uid)
            self._chat.pop(uid, None)
            self._chat_expired.discard(uid)
            pending_inbox = self._inbox_pending.get(uid)
            if pending_inbox:
                self._inbox_receipt(pending_inbox.get("msg"), "cancelled")
            for pending in (self._pending, self._pending_meeting, self._await_code,
                            self._inbox_pending):
                pending.pop(uid, None)
            if self.queue.unfinished_tasks or self._inbox_q.unfinished_tasks or self._running:
                self._draining.add(uid)
                self.reply(msg, "⏳ 已结束对话，不再接收新任务。已接收的任务和附件会继续处理，结束后自动待机；无需再说一次。")
                return True
            self._draining.discard(uid)
            self.reply(msg, "✅ 已进入后台待机，不执行新任务、不接收附件。发「开始对话」唤醒；已设置的提醒仍会通知。")
            return True
        if body.lower() in ("/status", "/状态", "状态", "桥的状态"):
            mode = "正在收尾，完成后自动待机" if uid in self._draining else ("工作中" if uid in self._awake else "待机中")
            self.reply(msg, "✅ 后台在线，助手%s。发「开始对话」唤醒，发「结束对话」待机。\n%s"
                       % (mode, self._try_admin(uid, "/status")))
            return True
        if uid in self._draining:
            admin = parse_admin(body)
            if body.lower() in ("/stop", "/stopall", "/queue", "/queue clear") or (admin and admin[0] in ('queue_manage', 'queue_help')):
                return False
            self.reply(msg, "🔕 正在收尾，这条新任务未接收。完成后自动待机；要继续发新任务请说「开始对话」。")
            return True
        if uid in self._awake:
            return False
        self.reply(msg, "🔕 助手正在后台待机，这条未执行、附件未接收。发「开始对话」后再发任务。")
        return True

    def _finish_draining(self):
        with self._desktop_lock:
            if self.queue.unfinished_tasks or self._inbox_q.unfinished_tasks or self._running:
                return
            for uid in self._draining:
                pending = self._inbox_pending.pop(uid, None)
                if pending:
                    self._inbox_receipt(pending.get("msg"), "cancelled")
                self._pending.pop(uid, None)
                self._pending_meeting.pop(uid, None)
            self._draining.clear()
