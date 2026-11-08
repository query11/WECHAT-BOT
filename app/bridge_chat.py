# -*- coding: utf-8 -*-
"""对话模式：发「开始对话」之后这个用户的消息免 /cc 前缀，「结束对话」退出。

从 wechat_bridge.py 拆出来的（2026-09-02），逻辑一行没改，注释原样搬。

状态（_chat / _chat_expired）只在内存里，桥重启就回到每条都要带前缀的状态——这一点
拆分前后一样。哪些句子算开关是 bridge_text.parse_chat_switch 的事，这边只管开关本身、
空闲超时，以及超时之后怎么把话说清楚（免前缀的消息被静默丢掉，看着和桥挂了一样）。
"""

import logging
import time

from bridge_text import CHAT_ON_TIPS, parse_chat_switch

log = logging.getLogger("bridge")


class ChatMixin(object):
    """要靠 Bridge 提供这些：client.send_text、prefix，配置来的 chat_mode_enable、
    chat_idle_seconds、no_prefix_hint_seconds，以及 _chat、_chat_expired、_last_ctx、
    _hint_at（_hint_at 是和「没带前缀」那句提示共用的，所以两者不会连着来）。"""

    def _idle_hint(self):
        if not self.chat_idle_seconds:
            return ""
        return "（%d 分钟不说话会自动退出，桥重启也会。）\n" % (self.chat_idle_seconds // 60)

    def _in_chat(self, uid, now=None):
        """这个用户现在是不是免前缀状态。顺手把空闲超时的踢掉。"""
        if uid not in self._chat:
            return False
        return not self._expire_one(uid, now)

    def _expire_one(self, uid, now=None):
        """空闲超时就退出对话模式并返回 True。idle_seconds 为 0 表示永不自动退出。"""
        at = self._chat.get(uid)
        if at is None or not self.chat_idle_seconds:
            return False
        if (now if now is not None else time.time()) - at <= self.chat_idle_seconds:
            return False
        self._chat.pop(uid, None)
        self._chat_expired.add(uid)
        log.info("对话模式空闲超时自动退出 user=%s", uid)
        return True

    def _expire_chat_modes(self, now=None):
        """轮询循环里定时调用：超时的立刻退出并主动发一条微信，不用等下一句话。"""
        if not self.chat_idle_seconds or not self._chat:
            return
        now = time.time() if now is None else now
        for uid in list(self._chat):
            if self._expire_one(uid, now) and self._notify_expired(uid):
                self._chat_expired.discard(uid)

    def _notify_expired(self, uid):
        """主动推一条「已自动退出」。这里用的 context_token 是上一条消息留下的，
        可能已经失效——推不出去不算错，标记留着，下一条消息进来时再补一句。"""
        ctx = self._last_ctx.get(uid)
        if not ctx:
            return False
        text = ("🔕 %d 分钟没说话，对话模式自动退出了。\n"
                "要接着聊就发「开始对话」，或者给单条消息带上 %s。"
                % (self.chat_idle_seconds // 60, self.prefix))
        try:
            self.client.send_text(ctx[0], text, ctx[1])
            self._hint_at[uid] = time.time()   # 这条已经把话说清了，别紧接着再提示一遍
            return True
        except Exception as exc:
            log.warning("对话模式自动退出的通知没发出去（下一条消息里补）：%s", exc)
            return False

    def _expired_note(self, uid):
        """自动退出后第一条不带前缀的消息：说清为什么没反应，别让人以为桥死了。
        主动推已经成功过就不再啰嗦（那时标记就清了）。"""
        if uid not in self._chat_expired:
            return None
        self._chat_expired.discard(uid)
        return ("🔕 这条我没执行：空闲超过 %d 分钟，对话模式已经自动退出了。\n"
                "发「开始对话」重新进去，或者给这条带上 %s 再发一遍。"
                % (self.chat_idle_seconds // 60, self.prefix))

    def _no_prefix_hint(self, uid):
        """不带前缀的消息本来是无声丢掉的，和桥挂了看着一模一样：2026-08-30 真机上
        「开始会话」打错一个字，人在那头白等，日志里只有一行「跳过」。所以回一句提示。

        两条克制：① 同一个人 no_prefix_hint_seconds 内只提示一次，免得手机上一串
        闲聊被刷屏；② 提示里绝不带原文，免得把会议密码这类东西回显出去。
        自动退出的那条说明（_expired_note）也算一次提示，所以两者不会连着来。
        返回 None 表示这次不提示。
        """
        if not self.no_prefix_hint_seconds:
            return None
        now = time.time()
        last = self._hint_at.get(uid)
        if last is not None and now - last < self.no_prefix_hint_seconds:
            return None
        self._hint_at[uid] = now
        how = "带上 %s" % self.prefix
        if self.chat_mode_enable:
            how += "，或者发「开始对话」之后就不用带了"
        return "🔕 这条我没执行：要让我干活得%s。" % how

    def _try_chat_switch(self, uid, body):
        """开关对话模式。返回要回的话；返回 None 表示这条不是开关。"""
        want = parse_chat_switch(body)
        if want is None:
            return None
        if not self.chat_mode_enable:
            return ("❌ 对话模式没启用（config.json 里 chat_mode.enable）。"
                    "现在每条都要带 %s。" % self.prefix)
        if want == "on":
            already = uid in self._chat
            self._chat[uid] = time.time()
            self._chat_expired.discard(uid)
            if already:
                return "✅ 已经在对话模式里了，直接说就行。"
            log.info("对话模式开启 user=%s", uid)
            return ("✅ 已进入对话模式\n"
                    "%s接下来直接说话就行，不用带 %s。\n"
                    "下面这些都能直接说：\n\n%s\n\n"
                    "说「结束对话」退出；带 %s 也照旧能用。"
                    % (self._idle_hint(), self.prefix, CHAT_ON_TIPS, self.prefix))
        if uid not in self._chat:
            return "🔕 本来就不在对话模式，这会儿每条都要带 %s。" % self.prefix
        self._chat.pop(uid, None)
        self._chat_expired.discard(uid)
        log.info("对话模式关闭 user=%s", uid)
        return "✅ 已退出对话模式。要让我干活就重新带上 %s。" % self.prefix
