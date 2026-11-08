# -*- coding: utf-8 -*-
"""进腾讯会议 + 定时进会：拼 wemeet:// 链接直接入会，定时的到点先问一句。

从 wechat_bridge.py 拆出来的（2026-09-02），逻辑一行没改，注释原样搬。

做成 mixin 而不是独立对象，是因为 selftest.py 里是直接 `b._timer_tick(...)` 这么
调的——挂在 Bridge 上这些写法才一个都不用改；方法之间互相调（_timer_tick →
_fire_meeting → _push）也照旧走 self，不用来回传引用。
"""

import logging
import time

import wemeet

log = logging.getLogger("bridge")


class MeetingMixin(object):
    """要靠 Bridge 提供这些：

    配置来的   prefix、meeting（MeetingJoiner 或 None）、schedule（MeetingSchedule
               或 None）、meeting_confirm_seconds、meeting_late_seconds
    内存状态   _await_code、_pending_meeting、_notes、_last_ctx，以及 _pending
               ——那是点击那边的，同一句「确认」不能两头抢，所以这边也要看一眼
    别处的方法 client.send_text、_peek
    """

    def _try_meeting(self, uid, body):
        """「进会 413231713」就自己拉起腾讯会议入会；说得不像就返回 None 交给 Claude。

        挂在 _try_app 之前：带了会议号的「打开腾讯会议 413231713」在开应用那条通道里
        是匹配不到名字的（它刻意不收参数），得先在这里截住。
        没带号的「进会」不落给 Claude——它没有 Bash 权限，做不了这件事，
        1 秒回一句「把会议号发我」比花一次额度更有用。
        带时刻的（「六点四十进会 …」）先走定时那条：真机上出过一次「六点四十」被
        当成「现在」立刻把人拉进会，所以时刻和「现在」必须分得干净。
        """
        now = time.time()
        plan = wemeet.parse_schedule(body, now)
        if plan is not None:
            return self._handle_schedule(uid, plan, now)
        code, pwd = wemeet.parse_join(body)
        if code and self.schedule is not None:
            wait = self._await_code.get(uid)
            if wait and now < wait["at"]:
                # 上一条说了时间、这条才把号发过来（真机上就是这么分两条发的）
                return self._arrange(uid, code, pwd, wait["at"], wait["label"], now)
        asking = wemeet.parse_join_asking(body)
        if not code and not asking:
            return None
        if self.meeting is None:
            return ("❌ 进会的功能是关着的（config.json 里 meeting.enable）。"
                    "现在只能「%s 打开腾讯会议」，然后自己输号。" % self.prefix)
        if asking:
            return ("把会议号发我，9 到 11 位那串数字，比如「%s 进会 413231713」。"
                    "有入会密码就写成「进会 413231713 密码 8888」，"
                    "要定时就写成「六点四十进会 413231713」。" % self.prefix)
        text, handled = self.meeting.join(code, pwd)
        if not handled:
            return None
        # 密码绝不进日志；会议号也按正文对待，log_message_text 关着时只记位数
        log.info("走进会通道 user=%s 会议号=%s", uid, self._peek(code))
        return text

    # ---------- 定时进会 ----------

    def _handle_schedule(self, uid, plan, now):
        """parse_schedule 认出来了：可能是定上、可能还差号、也可能那个点已经过去。"""
        if self.meeting is None:
            return ("❌ 进会的功能是关着的（config.json 里 meeting.enable），"
                    "定时也就无从谈起。")
        if self.schedule is None:
            return ("❌ 定时进会是关着的（config.json 里 meeting.timer_enable）。"
                    "现在只能到点了自己说「%s 进会 <号>」。" % self.prefix)
        kind = plan["kind"]
        if kind == "past":
            return ("❌ %s 今天已经过去了（现在 %s），我只认当天还没到的时刻。\n"
                    "现在就进的话直接说「进会 <号>」；明天的会等明天再说。"
                    % (plan["label"], time.strftime("%H:%M", time.localtime(now))))
        if kind == "far":
            return "❌ %s 超过 24 小时了，我只认当天的时刻。" % plan["label"]
        if kind == "need_code":
            self._await_code[uid] = {"at": plan["at"], "label": plan["label"],
                                     "set_at": now}
            log.info("定时进会等会议号 user=%s 时刻=%s", uid, plan["label"])
            return ("✅ %s 这个点我记下了。把会议号发过来就算定上"
                    "（整段腾讯邀请转过来也行）。\n到点我会先问你一句，回「确认」才进。"
                    % plan["label"])
        return self._arrange(uid, plan["code"], plan["pwd"], plan["at"],
                             plan["label"], now)

    def _arrange(self, uid, code, pwd, at, label, now):
        """真正把这一场记进待办表。"""
        if self.schedule is None:
            return ("❌ 定时进会是关着的（config.json 里 meeting.timer_enable）。")
        item, err = self.schedule.add(uid, code, at, label, pwd)
        if err == "same":
            self._await_code.pop(uid, None)
            return "🔕 这场已经定过了：%s 进 %s。" % (label, wemeet.pretty_code(code))
        if err == "full":
            return ("❌ 最多同时定 %d 场，先说「取消定时」腾个位置，或者发 /meetings 看清单。"
                    % self.schedule.max_pending)
        if err:
            return "❌ 这个会议号我没接住（%s）。" % err
        self._await_code.pop(uid, None)
        log.info("定时进会已记下 user=%s 会议号=%s 时刻=%s 还有=%d 场",
                 uid, self._peek(code), label, len(self.schedule.all()))
        note = ""
        if self.meeting_late_seconds:
            note = ("重开之后要是刚过去不到 %d 分钟，我还会补问一次。"
                    % (self.meeting_late_seconds // 60))
        return ("✅ 记下了：%s 进 %s%s。\n到点我先发一条问你，回「确认」才真的拉起来"
                "（%d 分钟不回就作废）。\n开关得一直开着才算数——关掉或者电脑睡了就到不了点，"
                "%s\n提前反悔就说「取消定时」，发 /meetings 看清单。"
                % (label, wemeet.pretty_code(code),
                   "（带密码）" if pwd else "",
                   self.meeting_confirm_seconds // 60, note))

    def _list_timers(self, uid):
        if self.schedule is None:
            return ("❌ 定时进会是关着的（config.json 里 meeting.timer_enable）。")
        lines = []
        p = self._pending_meeting.get(uid)
        if p:
            lines.append("· 正在等你确认：%s 那场（%s），回「确认」我就进"
                         % (p["label"], wemeet.pretty_code(p["code"])))
        w = self._await_code.get(uid)
        if w:
            lines.append("· %s 那场还差会议号，把号发过来就算定上" % w["label"])
        for it in self.schedule.all(uid):
            lines.append("· %s  %s%s" % (it["label"], wemeet.pretty_code(it["code"]),
                                         "（带密码）" if it.get("pwd") else ""))
        if not lines:
            return ("没有定下的会。要定就说「六点四十进会 413231713」，"
                    "到点我先问你一句。")
        return ("待进的会：\n%s\n到点我都会先问一句，回「确认」才进。"
                "说「取消定时」全部取消。" % "\n".join(lines))

    def _cancel_timers(self, uid):
        gone = self.schedule.cancel(uid) if self.schedule is not None else []
        wait = self._await_code.pop(uid, None)
        asked = self._pending_meeting.pop(uid, None)
        bits = []
        if asked:
            bits.append("正在等确认的那场（%s）" % asked["label"])
        for it in gone:
            bits.append("%s 那场（%s）" % (it["label"], wemeet.pretty_code(it["code"])))
        if wait:
            bits.append("%s 那个还没给号的" % wait["label"])
        if not bits:
            return "🔕 本来就没有定下的会。"
        log.info("取消定时进会 user=%s 取消了=%d 场", uid, len(bits))
        return "✅ 取消了：%s。" % "、".join(bits)

    def _resolve_meeting_confirm(self, uid, body):
        """到点那条问话的答复。返回要回的话；None 表示这条跟它无关。"""
        p = self._pending_meeting.get(uid)
        if not p:
            return None
        kind, explicit = wemeet.read_confirm(body)
        if kind is None:
            return None            # 说的是别的事：那条问话继续等着，到时间自己作废
        if not explicit and uid in self._pending:
            return None            # 光一个「确认」，而且还有个待确认的点击：让给点击
        if time.time() - p["asked_at"] > self.meeting_confirm_seconds:
            self._pending_meeting.pop(uid, None)
            return ("🔕 那条问话已经超过 %d 分钟作废了。要进就说「进会 %s」。"
                    % (self.meeting_confirm_seconds // 60,
                       wemeet.pretty_code(p["code"])))
        self._pending_meeting.pop(uid, None)
        if kind == "no":
            return "✅ 好，不进了。"
        if self.meeting is None:
            return "❌ 进会的功能这会儿是关着的（config.json 里 meeting.enable）。"
        text, handled = self.meeting.join(p["code"], p.get("pwd"))
        if not handled:
            return "❌ 这个会议号我没接住，没拉起来。"
        log.info("定时进会已确认并拉起 user=%s 会议号=%s", uid, self._peek(p["code"]))
        return text

    def _push(self, uid, text):
        """主动发一条微信。用的 context_token 是上一条消息留下的，可能已经失效——
        发不出去不算错，攒着，等这个用户下次说话时补上。"""
        ctx = self._last_ctx.get(uid)
        if ctx:
            try:
                self.client.send_text(ctx[0], text, ctx[1])
                return True
            except Exception as exc:                       # noqa: BLE001
                log.warning("主动通知没发出去（下条消息里补）：%s", exc)
        self._notes.setdefault(uid, []).append(text)
        return False

    def _timer_loop(self, tick=5):
        while True:
            try:
                self._timer_tick()
            except Exception:
                log.exception("定时进会的计时线程出错，这一轮跳过")
            time.sleep(tick)

    def _timer_tick(self, now=None):
        """到点的问一句、问过没人理的作废、说了时间又一直没给号的也作废。

        **这条心跳也带着定时提醒**（`_remind_tick`）——两者共用一个 5 秒的计时线程，
        所以这里不能因为「没装进会」就提前 return，不然提醒会在关掉进会时一起哑掉。
        """
        now = time.time() if now is None else now
        self._remind_tick(now)
        if self.schedule is None:
            return
        for it in self.schedule.due(now):
            self._fire_meeting(it, now)
        for uid, p in list(self._pending_meeting.items()):
            if now - p["asked_at"] > self.meeting_confirm_seconds:
                self._pending_meeting.pop(uid, None)
                log.info("定时进会等不到确认，作废 user=%s", uid)
                self._push(uid, "❌ %s 那场会（%s）我没进：等了 %d 分钟没等到确认。"
                           % (p["label"], wemeet.pretty_code(p["code"]),
                              max(1, self.meeting_confirm_seconds // 60)))
        for uid, w in list(self._await_code.items()):
            if now >= w["at"]:
                self._await_code.pop(uid, None)
                self._push(uid, "❌ %s 到了，但那场会你一直没把会议号发我，这条我作废了。"
                           % w["label"])

    def _fire_meeting(self, item, now):
        """到点了：不直接进，先问一句。真正的拉起在收到「确认」之后。"""
        uid, code = item["uid"], item["code"]
        late = int(item.get("late") or 0)
        if self.meeting_late_seconds and late > self.meeting_late_seconds:
            # 到点的时候桥没在跑（关了开关、电脑睡了）。早过去的就别再问了
            log.info("定时进会过期作废 user=%s 晚了=%d 秒", uid, late)
            return self._push(uid, "❌ %s 那场会（%s）我没进：到那个点的时候开关没开着"
                                   "（晚了 %d 分钟），这条作废了。"
                              % (item["label"], wemeet.pretty_code(code),
                                 max(1, late // 60)))
        self._pending.pop(uid, None)        # 待确认的点击让位，同一个「确认」不能两头抢
        self._pending_meeting[uid] = {"code": code, "pwd": item.get("pwd"),
                                      "label": item["label"], "asked_at": now}
        log.info("定时进会到点，发问话 user=%s 会议号=%s 晚了=%d 秒",
                 uid, self._peek(code), late)
        overdue = ("（比 %s 晚了 %d 分钟，开关刚才没开着。）\n"
                   % (item["label"], max(1, late // 60))) if late >= 60 else ""
        self._push(uid, "⏰ 到点了：%s 那场会，会议号 %s。\n%s回「确认」我就拉起腾讯会议，"
                        "回「取消」就不进；%d 分钟不回自动作废。"
                   % (item["label"], wemeet.pretty_code(code), overdue,
                      self.meeting_confirm_seconds // 60))
        return True
