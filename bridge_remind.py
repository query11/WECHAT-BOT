# -*- coding: utf-8 -*-
"""定时提醒 + 定时跑一句话在桥这一层的落点：新建、看清单、取消，以及到点动手。

和别的 mixin 一样，真正的解析和落盘都在 `remind.py`，这儿只做三件事：
把 `parse_remind` / `parse_task` 的结果翻译成要回的话、按固定的顺序问那几个解析器、
记日志。

**两种东西，两种到点行为**（同一张表，靠 `kind` 分）：

  · `remind`——到点发一条微信。**不用像进会那样先问一句「确认」**：提醒没有「后果」，
    它只是一句话，而且错过它的代价比发错高（你本来就是怕忘才定的）。
  · `task`——到点把**你那句话交给 Claude 去跑**（「明天九点帮我跑：把那个表处理一下」）。
    这是真去执行、要花额度，所以它排在提醒后面解析（带「提醒我」的一律归提醒），
    而且到点会先说一句「我开始跑了」。

迟到的口径和进会都不同：进会错过了就只说明一句（人不在电脑前，进一场早开完的会没意义），
而这两样晚了都还有用。区别是：**提醒照发**、只在正文里写明晚了多久；**任务不照跑**
（一条三天前的指令现在执行很可能是错的，而且它要动盘上的东西），只说明一句它作废了。
超过 `remind.stale_hours` 两样都作废。
"""

import logging
import time
import uuid

import remind

log = logging.getLogger("bridge.remind")

# 清单里定时任务前面挂这个，一眼分得清「发一条消息」和「真去执行」。
# 做成模块常量只为一件事：`.append()` 里出现中文字面量会被文案检查（selftest_style）盯着，
# 而这是**拼进清单的一行**、不是一条独立回话，不该带头部的状态记号。
TASK_MARK = "到点跑："
PAUSED_MARK = "已暂停 · "
TASK_HINT = ("带「%s」的那些到点会交给 Claude 真去执行（花额度），其余到点只发一条消息。"
             % TASK_MARK)


class FiredMsg(object):
    """到点那条「合成消息」：`_loop_worker` 和 `reply` 只用到下面这几个字段。

    定任务的那条消息早就处理完了，`context_token` 用的是这个用户**最近一次说话**时留下
    的那个（和 `_push` 用的是同一份），过期就发不出去——那种情况由 `_push` 的
    「攒着、下条消息里补」兜底，这里照旧只当它是一句普通消息入队。
    """

    def __init__(self, uid, text, ctx=None):
        self.from_user_id = uid
        self.context_token = (ctx or ("", ""))[1] if ctx else ""
        self.message_id = "schedule-" + uuid.uuid4().hex
        self.text = text
        self.quoted = None
        self.attachments = []
        self.message_type = 1


class RemindMixin(object):
    """要靠 Bridge 提供这些：reminders（ReminderStore 或 None）、prefix、
    remind_stale_seconds、_push、_peek，以及 `_enqueue` / `_last_ctx`（定时任务要入队）。"""

    # ---------- 新建（要带 /cc，或者在对话模式里）----------

    def _try_remind(self, uid, body):
        """像「明天九点提醒我看报告」就自己记下来，1 秒回；认不出返回 None 交给别的通道。"""
        plan = remind.parse_remind(body)
        if plan is None:
            return None
        if self.reminders is None:
            return ("❌ 提醒的功能是关着的（config.json 里 remind.enable）。"
                    "这句话我没接。")
        kind, label = plan["kind"], plan.get("label") or ""

        if kind == "past":
            return ("❌ 今天 %s 已经过去了。你这句里明说了日子（今天/今晚），"
                    "所以我**不替你顺延**——要明天的话把日子改了，"
                    "比如「明天 %s 提醒我<要提醒的事>」。" % (label, label))
        if kind == "far":
            return "❌ %s 太远了（我只认 7 天以内）。" % label
        if kind == "empty":
            return ("要提醒你什么？光给时间我记不住——"
                    "写成「%s 提醒我看那个报告」这样。\n（时间我认成 %s。）"
                    % (label, label))

        item, err = self.reminders.add(uid, plan["at"], label, plan["text"],
                                       daily=plan.get("daily"))
        if err == "same":
            return ("🔕 这条已经记过了：%s 提醒你「%s」。"
                    % (label, plan["text"]))
        if err == "dup":
            # 同一件事已经有一条「每天」的了，只是钟点不同。不悄悄改旧的、也不记成两条
            # （两条会在两个钟点各响一次），把话说清楚让你定。
            return ("🔕 「%s」已经有一条「%s」的了，我没再记一条——"
                    "两条会在两个钟点各响一次。\n"
                    "要改成「%s」，先说「取消提醒」再重说一次；"
                    "要早上晚上各一条，就把内容说得不一样（比如「早上吃药」「晚上吃药」）。"
                    % (plan["text"], item["label"], label))
        if err == "full":
            return ("❌ 最多同时记 %d 条，先说「取消提醒」腾个位置，"
                    "或者发 /reminders 看清单。" % self.reminders.max_pending)
        if err:
            return "❌ 这条没记下（%s）。" % err
        log.info("记下提醒 user=%s 时刻=%s 每天=%s 晚了=%s", uid, label,
                 bool(plan.get("daily")),
                 "顺延" if plan.get("slipped") else "没顺延")
        if plan.get("daily"):
            return ("✅ 记下了：%s 提醒你「%s」——**每天到点都会发**，"
                    "发完自动排到第二天，不用天天重说。\n"
                    "那一刻开关得开着、电脑别睡。\n"
                    "反悔就说「取消提醒」，发 /reminders 看清单。"
                    % (label, plan["text"]))
        slip = ""
        if plan.get("slipped"):
            slip = "\n（你说的那个点今天已经过了，我按**明天**记的——不对就重说一次。）"
        return ("✅ 记下了：%s 提醒你「%s」。%s\n"
                "到点我发一条微信；那一刻开关得开着、电脑别睡。\n"
                "反悔就说「取消提醒」，发 /reminders 看清单。"
                % (label, plan["text"], slip))

    def _try_task(self, uid, body):
        """「明天九点帮我跑：把那个表处理一下」——到点把那句话交给 Claude 去跑。

        排在 `_try_remind` 后面：两边词表不重叠（那边要「提醒我」这类词，这边要
        「帮我跑」这类），所以谁先谁后其实都不抢，但顺序写着更清楚——
        **带「提醒我」的一律按提醒办**。
        """
        plan = remind.parse_task(body)
        if plan is None:
            return None
        if self.reminders is None:
            return ("❌ 定时任务和提醒共用一张表（config.json 里 remind.enable），"
                    "现在是关着的，这句我没接。")
        kind, label = plan["kind"], plan.get("label") or ""
        if kind == "past":
            return ("❌ 今天 %s 已经过去了。你这句里明说了日子，我不替你顺延——"
                    "要明天的话把日子改了。" % label)
        if kind == "far":
            return "❌ %s 太远了（我只认 7 天以内）。" % label
        if kind == "empty":
            return ("要我到点跑什么？光给时间我不知道跑啥——"
                    "写成「%s 帮我跑：<要干的事>」这样。\n（时间我认成 %s。）"
                    % (label, label))

        item, err = self.reminders.add(uid, plan["at"], label, plan["text"],
                                       daily=plan.get("daily"), kind="task")
        if err == "same":
            return ("🔕 这条已经记过了：%s 跑「%s」。"
                    % (label, plan["text"]))
        if err == "dup":
            return ("🔕 「%s」已经有一条「%s」的了，我没再记一条——"
                    "两条会在两个钟点各跑一次。\n"
                    "要改成「%s」，先说「取消提醒」再重说一次。"
                    % (plan["text"], item["label"], label))
        if err == "full":
            return ("❌ 最多同时记 %d 条（提醒和任务共用这一张表），"
                    "先说「取消提醒」腾个位置。" % self.reminders.max_pending)
        if err:
            return "❌ 这条没记下（%s）。" % err
        log.info("记下定时任务 user=%s 时刻=%s 内容=%s", uid, label,
                 self._peek(plan["text"]))
        if plan.get("daily"):
            return ("✅ 记下了：每天%s 我会把「%s」交给 Claude 跑一遍。\n"
                    "⚠️ 这是**真去执行**：会花额度，也可能改盘上的文件。\n"
                    "那一刻开关得开着、电脑别睡；每次都会先回一句「开始跑了」再动手。\n"
                    "反悔就说「取消提醒」，发 /reminders 看清单。"
                    % (label.replace("每天", ""), plan["text"]))
        return ("✅ 记下了：%s 我会把「%s」交给 Claude 跑一遍。\n"
                "⚠️ 这是**真去执行**：会花额度，也可能改盘上的文件——"
                "不像提醒只发一条消息。\n"
                "到点那一刻开关得开着、电脑别睡；我会先回一句「开始跑了」再动手。\n"
                "反悔就说「取消提醒」，发 /reminders 看清单。"
                % (label, plan["text"]))

    # ---------- 看清单 / 取消（不用带前缀）----------

    def _remind_list(self, uid):
        if self.reminders is None:
            return "❌ 提醒的功能是关着的（config.json 里 remind.enable）。"
        items = self.reminders.all(uid)
        if not items:
            return ("没有记下的提醒。要说「明天九点提醒我交房租」这样，"
                    "到时候我发微信给你。")
        lines = ["记下的提醒（按时间排）："]
        has_task = False
        for i, it in enumerate(items, 1):
            what = it["text"]
            if remind._kind_of(it) == "task":
                has_task = True
                what = TASK_MARK + what
            lines.append("%d. %s%s  %s" % (i, PAUSED_MARK if it.get('paused') else '', it["label"], what))
        if has_task:
            lines.append(TASK_HINT)
        lines.append("说「取消提醒」全取消，「取消提醒 2」只取消第 2 条。")
        return "\n".join(lines)

    def _remind_cancel(self, uid, body):
        """「取消提醒」/「取消提醒 2」/「取消任务」。不是这个句式返回 None。"""
        hit, index = remind.parse_cancel(body)
        if not hit:
            return None
        if self.reminders is None:
            return "❌ 提醒的功能是关着的（config.json 里 remind.enable）。"
        gone = self.reminders.cancel(uid, index)
        if gone is None:
            return ("❌ 没有第 %d 条。发 /reminders 看清单。" % index)
        if not gone:
            return "🔕 本来就没有记下的提醒。"
        log.info("取消提醒 user=%s 取消了几条=%d", uid, len(gone))
        bits = ["%s 那条（%s%s）" % (it["label"],
                                     TASK_MARK if remind._kind_of(it) == "task" else "",
                                     it["text"]) for it in gone[:5]]
        more = "，还有 %d 条" % (len(gone) - 5) if len(gone) > 5 else ""
        return "✅ 取消了：%s%s。" % ("、".join(bits), more)

    # ---------- 到点 ----------

    def _remind_tick(self, now=None):
        """到点的发出去 / 跑起来。跑在计时线程上（和定时进会共用那条 5 秒心跳）。"""
        store = self.reminders
        if store is None or (getattr(self, '_desktop_stop', None) and self._desktop_stop.is_set()):
            return
        now = time.time() if now is None else now
        for item in store.due(now):
            try:
                if hasattr(self, 'allowed') and item['uid'] not in self.allowed:
                    self._record_schedule(store, item, 'skipped', '用户已移出白名单，本次未执行。')
                elif getattr(self, '_desktop_stop', None) and self._desktop_stop.is_set():
                    self._record_schedule(store, item, 'skipped', '后台正在停止，本次未执行。')
                elif item.get("kind") == "task":
                    self._fire_task(item, now, store)
                else:
                    sent = self._fire_remind(item, now)
                    expired = self.remind_stale_seconds and item['late'] > self.remind_stale_seconds
                    state = 'expired' if expired else 'sent' if sent else 'unconfirmed'
                    detail = '超出补发期限，提醒已作废。' if expired else '微信接口已接受提醒。' if sent else '发送未确认，已留待下次微信消息时补充。'
                    self._record_schedule(store, item, state, detail)
            except Exception:
                log.exception('定时触发未获确认 id=%s', item['run_id'])
                self._record_schedule(store, item, 'uncertain', '本次触发未获确认，请核对任务记录。')

    def _record_schedule(self, store, item, state, detail, task_id=''):
        try:
            store.record_run(item, state, detail, task_id)
        except Exception:
            # External effects may already have happened. Never replay on a history write failure.
            log.exception('无法保存定时触发结果 id=%s', item.get('run_id'))

    def _fire_remind(self, item, now):
        uid = item["uid"]
        late = int(item.get("late") or 0)
        if self.remind_stale_seconds and late > self.remind_stale_seconds:
            # 太旧了：一条三天前的提醒打扰你没意义，只说明一句
            log.info("提醒过期作废 user=%s 晚了=%d 秒", uid, late)
            return self._push(uid, "⏰ 有一条约好的提醒（%s 那条）当时没发出来，"
                                   "现在过去太久了，我就不打扰你了。" % item["label"])
        note = ""
        if late >= 60:
            note = ("\n（晚了 %d 分钟——到点的时候开关没开着，刚起来我就补上了。）"
                    % max(1, late // 60))
        log.info("发提醒 user=%s 晚了=%d 秒", uid, late)
        return self._push(uid, "⏰ 提醒你：%s%s" % (item["text"], note))

    def _fire_task(self, item, now, store=None):
        """到点了：把那句话当一条消息交给自己的队列去跑（走 `_enqueue` 唯一的门）。

        **太旧的不跑**（和提醒刻意不同）：一条三天前的指令现在执行很可能是错的，
        而且它要动盘上的东西——宁可作废、让你再说一次。
        """
        uid, text = item["uid"], item["text"]
        store = store or self.reminders
        late = int(item.get("late") or 0)
        if self.remind_stale_seconds and late > self.remind_stale_seconds:
            self._record_schedule(store, item, 'expired', '超出执行期限，本次任务未执行。')
            log.info("定时任务过期作废 user=%s 晚了=%d 秒", uid, late)
            return self._push(uid, "⏰ 有一条定好的任务（%s 那条）当时没跑成，"
                                   "过去太久了，我没执行——要跑就再发一次。\n「%s」"
                              % (item["label"], text))
        msg = FiredMsg(uid, text, self._last_ctx.get(uid))
        if item.get('run_id'):
            msg.message_id = 'schedule-' + item['run_id']
        full = self._enqueue((msg, text, None), msg)
        if full is not None:
            retry = store.retry(item, now + 60)
            self._record_schedule(store, item, 'retry' if retry else 'rejected',
                                  '未入队，过一分钟再试。' if retry else '未入队；计划已变化或容量不足，未安排重试。')
            return self._push(uid, "⏰ 到点了要跑「%s」，但未能排队。%s%s"
                              % (text, '我过一分钟再试。' if retry else '请到定时任务页核对计划。', full))
        self._record_schedule(store, item, 'queued', '已加入任务队列，请查看任务记录。', getattr(msg, '_journal_id', ''))
        log.info("定时任务已交给队列 user=%s 内容=%s", uid, self._peek(text))
        return self._push(uid, "⏰ 到点了，我把你说的这条交给 %s 去跑了：\n%s"
                          % (getattr(self, 'backend_name', 'Claude'), text))
