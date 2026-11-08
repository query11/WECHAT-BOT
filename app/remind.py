# -*- coding: utf-8 -*-
"""定时提醒：「明天九点提醒我看那个报告」。

和「定时进会」「定时关机」共用同一条计时线程（5 秒一跳，见 bridge_meeting._timer_loop），
这个文件只管提醒自己：解析、落盘、到点该说哪句。

**时间解析整块复用 `wemeet`**（`_match_time` 认出哪一段是时刻、`_shift_hour` 把「下午三点」
折成 15 点、`stamp_label` 给出给人看的标签）。这个项目已经吃过「两个文件各存一份时间解析
必然走岔」的亏（见部署指南里 power.py 那一段），所以这里一个字都不重写，只加一样
wemeet 没有的东西：**跨天**。

跨天的口径和进会/关机**相反，这是故意的**：

  · 进会、关机遇上「今天已经过了」只回一句「过去了」，**不顺延**——那两件事过期就没意义了
    （一场开完的会、一个你想让它关机的时刻），而顺延过的时刻到点你早忘了。
  · 提醒反过来：下午两点说「九点提醒我吃药」，几乎总是指**明天**九点。所以**没明说日子、
    而今天那个候选都过了**时，顺延到下一个还没到的候选（也就是明天早上那个），
    并且**回话里把理解成的时刻写出来**——说「九点」的人只有靠那句才纠得过来，
    看错了立刻说「取消提醒」重来。
  · 但**明说了日子的不滚**：「今晚八点」说了今天，过了就是过了，直说「今天 20:00 已经过去了」。

碰这个词（`_TRIGGERS`）和时刻**两个都要有**才算提醒，所以「提醒功能怎么用」这种问句
不会被误吞——没有时刻，照旧交给 Claude。
"""

import copy
import hashlib
import json
import math
import uuid
import logging
import os
import re
import threading
import time

import wemeet

log = logging.getLogger("remind")

# 说明「这是在让我提醒你」的词。要有它 + 一个时刻才算提醒。
# 「记得」「别忘」也收：它们在人嘴里就是这个意思，而认错的代价很小——真认错了
# 你把那条提醒说「取消提醒」删掉就行，不会像关机那样不可逆。
_TRIGGERS = ("提醒我", "提醒一下我", "到时候提醒我", "到时提醒我", "记得提醒我",
             "提醒", "叫我", "记得", "别忘")
# 否定：说了这些就不是「新建一条提醒」（「别提醒我」这种）
_NEGATIVE = ("别提醒", "不用提醒", "不要提醒", "别再提醒", "取消提醒", "别叫我",
             "不用叫", "别忘提醒")            # 「别忘提醒」自相矛盾，交给 Claude 问清楚
# 认出日子。**明说了日子就不顺延**，见文件开头。长的排前面（「大后天」先于「后天」）
_DAYS = {"今天": 0, "今日": 0, "今晚": 0, "今早": 0, "今晚上": 0, "今天晚上": 0,
         "明天": 1, "明日": 1, "明早": 1, "明晚": 1, "明天早上": 1, "明天晚上": 1,
         "后天": 2, "大后天": 3}
_DAY_RE = re.compile("|".join(sorted(_DAYS, key=len, reverse=True)))
# 「每天」这类重复：**只认这三个词**（也只做这一种重复）。「每周一」「每月1号」那种
# 周期性说法各有各的历法口径（工作日要排节假日、每月要排大小月），一次做不对就会在
# 你没想到的那天响，所以宁可漏认，整句交给 Claude 问清楚——见下面的 `_PERIOD`。
_DAILY = ("每天", "每日", "每一天")
# 一眼看得出「这是周期性说法、而我不做」的词。命中就**整句不认**（交给 Claude），
# 而不是当成一次性提醒——那样会「只在明天响一次、还在错的那天」，比不认更坏
# （「每周一早上九点提醒我吃药」被记成「明天九点提醒你：每周一吃药」就是这种）。
# 故意不含「每天/每日/每一天」：那是 `_DAILY`，真做。
_PERIOD = ("每周", "每星期", "每礼拜", "每季度", "每月", "每年", "每隔", "每逢",
           "每个工作日", "每工作日")


# ---------- 「到点跑一句话」（定时任务）----------
# 「明天九点帮我跑：把那个表处理一下」这类：到点把那句话交给 Claude 去执行。
# **必须同时有「帮我/到点/定时」+ 一个「干」动词**，光有「跑一下」不算——
# 「我明天九点要跑一下步」说的是人自己，认了就成了到点花一次额度去跑一句废话。
_RUN_WORDS = ("帮我跑", "替我跑", "帮我执行", "替我执行", "帮我处理", "帮我弄",
              "帮我运行", "到点跑", "到点执行", "定时跑", "定时执行",
              "自动跑", "自动执行", "到点帮我", "定时帮我")
# 否定：说了这些就不是「让它到点去跑」。**「取消任务」也在这儿**——它是取消那条
# 指令（另有专门解析），不该反被当成一条新任务。
_RUN_NEGATIVE = ("别帮我", "不用帮我", "不要帮我", "别跑", "不用跑", "别执行",
                 "不用执行", "别自动", "别定时", "别弄", "取消任务")
# 在问办法的，一律交给 Claude（「怎么定时跑一个脚本」不是在派活）
_ASK_WORDS = ("怎么", "如何", "为什么", "为啥", "什么", "咋", "能不能", "可不可以",
              "教程", "行不行", "咋样")
# 一句提醒别太长：长了更像在跟 Claude 说事，不是让它记一句话
MAX_TEXT = 300
# 提醒能定到多远的未来。进会/关机是 24 小时（只管今天），提醒要能「后天」「大后天」，
# 所以放到 7 天；再远的多半是「下个月」那种，交给 Claude 更好
MAX_AHEAD = 7 * 86400
# 恰好等于现在（差几秒）的那个候选也算「还没到」，免得整点发过去被判成过去了
LATE_OK = 60


def _at_day(h, m, days, now):
    """今天 + days 那天的 h:m（按本地日历算，mktime 会把越界的日子自己理顺）。"""
    t = time.localtime(now)
    base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, 0, 0, 0, 0, 0, -1))
    return base + h * 3600 + m * 60


def _day_of(ts):
    t = time.localtime(ts)
    return (t.tm_year, t.tm_yday)


def _day_offset(text):
    """这句话明说了哪一天吗。返回 (天数偏移, 命中的原文)；没说返回 (None, None)。"""
    m = _DAY_RE.search(text or "")
    if not m:
        return None, None
    return _DAYS[m.group(0)], m.group(0)


def parse_remind(text, now=None, _words=None):
    """「明天九点提醒我看报告」→ dict；认不出返回 None。

      {"kind": "remind", "at": 时间戳, "label": "明天 09:00", "text": "看报告",
       "slipped": 是不是顺延过的}
      {"kind": "past",  "label": "20:00"}    明说了日子、那个点已经过了
      {"kind": "far",   "label": …}          超过 7 天
      {"kind": "empty", "at": …, "label": …} 时间和词都有，但没说提醒什么

    `_words` 是「什么词代表这是在让我记一条」的词表，默认 `_TRIGGERS`。
    **只有 `parse_task` 会传别的**（`_RUN_WORDS`），好让「到点跑一句话」和「提醒」
    共用这一份时间解析——时间口径只能有一份，两份必然走岔（见文件开头）。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > MAX_TEXT:
        return None
    for w in _NEGATIVE:
        if w in raw:
            return None
    words = _words or _TRIGGERS
    if not any(t in raw for t in words):
        return None
    # 周期性说法（每周一、每月1号…）**整句不认**——不是漏了，是刻意交给 Claude 问，
    # 理由见 `_PERIOD` 那段注释。放在时刻解析之前，省得白算一遍
    if any(w in raw for w in _PERIOD):
        return None
    hit = wemeet._match_time(wemeet._time_text(raw))
    if not hit:
        return None                       # 有「提醒」没时刻 → 交给 Claude 问清楚
    span, kind, vals = hit
    now = time.time() if now is None else now
    days, dayword = _day_offset(raw)
    dailyword = next((w for w in _DAILY if w in raw), None)
    slipped = False

    if dailyword:
        # 「每天早上九点提醒我吃药」：**每天** + 钟点。相对时长（「每天10分钟后」）和
        # 明说的日子（「每天明天」）都是自相矛盾的说法，一律不认 —— 交给 Claude 问清楚
        if kind == "rel" or days is not None:
            return None
        h, minute, period = vals
        if minute > 59:
            return None
        base = wemeet._shift_hour(h, period)
        # 「每天九点」到底九点还是晚上九点：**重复提醒取早的那个读法**（9 点）。
        # 一次性提醒靠「哪个还没到」消歧（过了的那个没用），但重复提醒描述的是一张固定的
        # 作息表、跟现在几点无关，而「每天九点」最自然就是早上九点（要说晚上会说「晚上九点」）。
        # 解析出来的那一个**存进表里**，以后每天固定用它，不会来回跳；标签也写清是几点。
        hour = base if (period or h > 12) else min(h, h + 12)
        ts = _next_daily(hour, minute, now)
        what = _content(raw, span, dayword, dailyword, words=words)
        label = "每天 %02d:%02d" % (hour, minute)
        if not what:
            return {"kind": "empty", "at": ts, "label": label}
        return {"kind": "remind", "at": ts, "label": label, "text": what,
                "slipped": False,
                "daily": {"hour": time.localtime(ts).tm_hour,
                          "minute": time.localtime(ts).tm_min}}

    if kind == "rel":
        ts = now + vals[0] * 60
        if ts - now > MAX_AHEAD:
            return {"kind": "far", "label": wemeet.stamp_label(ts, now)}
    else:
        h, minute, period = vals
        if minute > 59:
            return None
        base = wemeet._shift_hour(h, period)
        # 「下午三点」这种已经写明上下午（或本来就用 24 小时制）的，只有一个候选；
        # 光说「九点」的给两个（9 点和 21 点），由「哪个还没到」来定
        hours = [base] if (period or h > 12) else sorted({h, h + 12})
        if days is None:
            # 没说日子：今天和明天一起排，取**最早那个还没到的**
            # （早上八点说「九点」→ 今天 9 点；晚上十点说「九点」→ 明天 9 点）
            cands = sorted([_at_day(x, minute, 0, now) for x in hours]
                           + [_at_day(x, minute, 1, now) for x in hours])
            future = [t for t in cands if t > now - LATE_OK]
            if not future:                # 理论上到不了这儿（明天一定在未来）
                return None
            ts = future[0]
            slipped = _day_of(ts) != _day_of(now)
        else:
            # 明说了日子：就那一天，过了就是过了，不顺延
            cands = sorted(_at_day(x, minute, days, now) for x in hours)
            future = [t for t in cands if t > now - LATE_OK]
            if not future:
                return {"kind": "past",
                        "label": time.strftime("%H:%M", time.localtime(max(cands)))}
            ts = future[0]
        if ts - now > MAX_AHEAD:
            return {"kind": "far", "label": wemeet.stamp_label(ts, now)}

    label = wemeet.stamp_label(ts, now)
    what = _content(raw, span, dayword, words=words)
    if not what:
        return {"kind": "empty", "at": ts, "label": label}
    return {"kind": "remind", "at": ts, "label": label, "text": what,
            "slipped": slipped}


def parse_task(text, now=None):
    """「明天九点帮我跑：把那个表处理一下」→ 到点把那句话交给 Claude 去跑。

    返回的形状和 `parse_remind` 一样，只是 `kind` 是 `"task"`（不是 `"remind"`）：

      {"kind": "task", "at": …, "label": "明天 09:00", "text": "把那个表处理一下"}
      {"kind": "past"/"far", "label": …}      和提醒同一个口径
      {"kind": "empty", "at": …, "label": …}  说了时间、没说让跑什么

    **时间、跨天、顺延、每天这些一个字都不重写**：整块交给 `parse_remind`，只把
    触发词换成 `_RUN_WORDS`。这一层只多三道筛子：否定、在问办法、以及
    「帮我/到点」这类词必须在句子里（词表见 `_RUN_WORDS`）。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > MAX_TEXT:
        return None
    if any(w in raw for w in _RUN_NEGATIVE):
        return None
    if any(w in raw for w in _ASK_WORDS):
        return None                      # 在问办法，不是在派活
    plan = parse_remind(raw, now, _RUN_WORDS)
    if plan is None:
        return None
    if plan.get("kind") == "remind":
        plan = dict(plan)
        plan["kind"] = "task"            # 到点交给 Claude 跑，而不是发一条提醒
    return plan


def _content(raw, span, dayword, dailyword=None, words=None):
    """把时刻、日子词、「每天」、「提醒我」（或「帮我跑」）这些挖掉，剩下的就是正事。

    `words` 决定挖哪一种触发词：提醒挖 `_TRIGGERS`，「到点跑一句话」挖 `_RUN_WORDS`。
    """
    rest = raw.replace(span, " " * len(span)) if span else raw
    for w in (dayword, dailyword):
        if w:
            rest = rest.replace(w, " ", 1)    # 只挖命中的那一个，别动正文里的同名词
    for t in sorted(words or _TRIGGERS, key=len, reverse=True):
        if t in rest:
            rest = rest.replace(t, " ", 1)
            break
    got = rest.strip(" \t　，。,.、;；:：!！?？「」\"'“”")
    # 紧跟触发词后面的「一下/一遍/一次」是语气，不是正事：
    # 「提醒我一下交房租」要的是「交房租」，「帮我跑一下备份」要的是「备份」。
    for w in ("一下", "一遍", "一次"):
        if got.startswith(w):
            return got[len(w):].lstrip(" \t　，。,.、;；:：")
    return got


def _next_daily(hour, minute, now, slack=LATE_OK):
    """「每天 hour:minute」的下一次（今天那个还没到就今天，否则明天）。

    hour 是**已经解析好的**（0-23，不再有 9/21 那种歧义），所以这里只差一个今天/明天。

    `slack` 是「差这么几秒也算还没到」的宽容度，两个调用方要的不一样：
      · 刚定下这条时用默认的 `LATE_OK`——整点那一刻说「每天九点」，不该被判成明天；
      · **发完一条往下排**时必须传 0，否则 09:00 刚发出去，现在 09:00:30 又把
        「今天 09:00」算成下一次 → 同一分钟里一遍遍重发。
    """
    cands = [_at_day(hour, minute, 0, now), _at_day(hour, minute, 1, now)]
    future = [t for t in cands if t > now - slack]
    return future[0] if future else cands[0]


def _daily_of(obj):
    """把落盘/传进来的「每天」归一成 `{"hour": int, "minute": int}`；不成样子返回 None。

    唯一的归一入口：`ReminderStore._load`（读回来）和 `add`（写进去）都过这里，
    所以表里的 `daily` 一定要么没有、要么是干净的 0-23/0-59 整数，`due` 排下一天时能直接用。
    """
    d = obj.get("daily") if isinstance(obj, dict) else None
    if not isinstance(d, dict):
        return None
    try:
        hour = int(d.get("hour"))
        minute = int(d.get("minute"))
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return {"hour": hour, "minute": minute}


def _kind_of(obj):
    """这条是「提醒」还是「定时任务」。落盘数据可能是老版本（没有 `kind`），一律当提醒。"""
    return "task" if str(obj.get("kind") or "") == "task" else "remind"


def _key(it):
    """一条东西的身份：谁 + 哪个时刻 + 什么内容 + 哪一种。比对「是不是同一条」一律用它。

    带上 `uid` 是必要的：不带的话，两个用户各定一条同样时刻、同样内容的提醒，
    取消/发出其中一条会把另一条也连带删掉。带 `kind` 同理——同一句话的「提醒」
    和「定时任务」是两条不同的东西。
    """
    return (it.get("uid"), it.get("at"), it.get("text"), _kind_of(it))


class ScheduleError(ValueError):
    pass


class ReminderStore(object):
    """待发的提醒表：内存一份，落盘一份（reminders.json）。

    和 `wemeet.MeetingSchedule` 一个套路（整份重写 + 临时文件 + `os.replace`），理由也一样：
    定了「明天九点」的提醒，中间关一次开关不该让它无声消失。

    这个文件里装的是**你要提醒自己的事**（往往挺私人），所以它同时进了 `.gitignore`
    和「不发回微信」的名单——和 meetings.json 同一个待遇。

    **两种东西共用这一张表**（`kind` 区分）：`"remind"` 到点发一条微信；`"task"`
    到点把那句话交给 Claude 去跑（「明天九点帮我跑：把那个表处理一下」）。共用一张表
    是刻意的：同一个计时线程、同一条「看提醒 / 取消提醒」的路，少一套要同步的东西。
    """

    def __init__(self, path, max_pending=10):
        self.path = path
        self.max_pending = max(1, int(max_pending or 1))
        self._lock = threading.RLock()
        self.items = []
        self.history = []
        self.load_error = False
        self._load()

    # ---- 落盘 ----

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict) or not isinstance(raw.get('pending'), list):
                raise ValueError('invalid schedule file')
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.load_error = True
            return
        out, seen = [], set()
        try:
            for index, it in enumerate(raw['pending']):
                if not isinstance(it, dict):
                    raise ValueError('invalid reminder')
                at = float(it.get('at') or 0)
                what = str(it.get('text') or '').strip()
                set_at = float(it.get('set_at') or at)
                if not math.isfinite(at) or at <= 0 or not math.isfinite(set_at) or not what:
                    raise ValueError('invalid reminder time')
                time.localtime(at)
                ident = it.get('id')
                if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
                    # Stable identities for old files without changing disk on read.
                    ident = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([index, it], sort_keys=True, ensure_ascii=False)).hex
                if ident in seen:
                    raise ValueError('duplicate schedule id')
                seen.add(ident)
                out.append(dict(uid=str(it.get('uid') or ''), at=at, label=str(it.get('label') or ''),
                                text=what, set_at=set_at, daily=_daily_of(it),
                                kind=_kind_of(it), id=ident, paused=it.get('paused') is True,
                                revision=max(1, int(it.get('revision') or 1))))
            history = raw.get('history', [])
            if not isinstance(history, list):
                raise ValueError('invalid run history')
            for row in history:
                if not isinstance(row, dict):
                    raise ValueError('invalid occurrence')
                for key in ('id', 'schedule_id'):
                    if not isinstance(row.get(key), str) or not re.fullmatch('[a-f0-9]{32}', row[key]):
                        raise ValueError('invalid occurrence id')
                for key in ('scheduled_at', 'fired_at'):
                    if type(row.get(key)) not in (int, float) or not math.isfinite(row[key]) or row[key] <= 0:
                        raise ValueError('invalid occurrence time')
                    time.localtime(row[key])
                if row.get('kind') not in ('remind', 'task') or any(not isinstance(row.get(k), str) for k in ('uid', 'text', 'state', 'detail')):
                    raise ValueError('invalid occurrence fields')
                if not isinstance(row.get('task_id', ''), str):
                    raise ValueError('invalid receipt id')
            self.history = self._trim_history([dict(r, state='uncertain', detail='上次触发中断，请核对任务记录。')
                                               if r.get('state') == 'dispatching' else r
                                               for r in history if isinstance(r, dict)])
            self.items = sorted(out, key=lambda x: x['at'])
        except (OSError, ValueError, TypeError, OverflowError):
            self.items = []; self.history = []; self.load_error = True

    @staticmethod
    def _trim_history(rows):
        counts, kept = {}, []
        for row in reversed(rows):
            uid = str(row.get('uid') or '')
            counts[uid] = counts.get(uid, 0) + 1
            if counts[uid] <= 50:
                kept.append(row)
        return list(reversed(kept))

    def _commit(self, items, history=None):
        if self.load_error:
            raise ScheduleError('提醒文件不可读，未保存任何修改；请检查本机日志和文件。')
        items = copy.deepcopy(sorted(items, key=lambda x: x['at']))
        history = copy.deepcopy(self._trim_history(self.history if history is None else history))
        tmp = self.path + '.' + uuid.uuid4().hex + '.tmp'
        try:
            with open(tmp, 'x', encoding='utf-8') as fh:
                json.dump(dict(pending=items, history=history), fh, ensure_ascii=False, indent=2, allow_nan=False)
                fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        # Publish only after durable replacement. Failed writes leave memory intact.
        self.items, self.history = items, history

    # ---- 增删查 ----

    def add(self, uid, at, label, text, daily=None, kind="remind"):
        """返回 (记下的那条, 拒绝的理由)。理由非空就是没记下：

          · `"same"`——同一件事、同一个点（每天的还要求钟点也一样），已经记着；
          · `"dup"`——同一件事已经有「每天几点」那条了，但钟点不一样（返回的是**旧的那条**，
            让桥说清楚是改还是加，见下面那段注释）；
          · `"full"`——这个用户的条数到 `max_pending` 了。

        `daily` 不为空就是「每天几点」那条（`{"hour": …, "minute": …}`）：**它到点不会被
        删掉，而是排到下一天**（见 `due`）。

        `kind` 是这条东西的**种类**：`"remind"`（到点发一条微信）或 `"task"`（到点把那句话
        交给 Claude 去跑）。**提醒和定时任务共用这一张表**：同一张表、同一个计时线程、
        同一条「看提醒 / 取消提醒」的路，少一套要同步的东西。
        """
        what = (text or "").strip()
        if not what:
            return None, "empty"
        daily = _daily_of({"daily": daily})
        kind = _kind_of({"kind": kind})
        with self._lock:
            for it in self.items:
                # **kind 不一样的不是「同一条」**：同一句话让「提醒我」和「帮我跑」
                # 各记一条是两件事（一个发消息、一个真去执行），不能说成重了
                if (it["uid"] != str(uid) or it["text"] != what
                        or _kind_of(it) != kind):
                    continue
                mine = it.get("daily")
                if daily and mine == daily:
                    return None, "same"        # 同一件事、同一个每天的点
                if daily and mine:
                    # 同一件事（都叫「吃药」）、但每天的点不一样：**既不悄悄改掉旧的那条，
                    # 也不记成两条**——两条会在两个钟点各响一次，正是「提醒重复」那个毛病。
                    # 把旧的那条带回去让桥说清楚，改还是加由你一句话定。
                    return dict(it), "dup"
                if not daily and not mine and abs(it["at"] - at) < 60:
                    return None, "same"        # 同一件事、差不到一分钟
            if len([it for it in self.items if it["uid"] == str(uid)]) >= self.max_pending:
                return None, "full"
            item = {"uid": str(uid), "at": float(at), "label": label or "",
                    "text": what, "set_at": time.time(), "kind": kind,
                    "id": uuid.uuid4().hex, "paused": False, "revision": 1}
            if daily:
                item["daily"] = daily
            self._commit(self.items + [item])
            return dict(item), ""

    def all(self, uid=None):
        with self._lock:
            return copy.deepcopy([it for it in self.items if uid is None or it["uid"] == str(uid)])

    def cancel(self, uid, index=None):
        """取消这个用户的提醒。`index` 是按时间排的**1 起序号**；None = 全部。

        序号越界返回 None（调用方要能区分「没有可取消的」和「序号不对」）。
        """
        with self._lock:
            mine = sorted([it for it in self.items if it["uid"] == str(uid)],
                          key=lambda x: x["at"])
            if index is None:
                gone = mine
            else:
                if not (1 <= index <= len(mine)):
                    return None
                gone = [mine[index - 1]]
            if not gone:
                return []
            keys = {_key(it) for it in gone}
            self._commit([it for it in self.items if _key(it) not in keys])
            return [dict(it) for it in gone]

    def due(self, now=None):
        """到点的都取出来，每条带上迟了多少秒。

        **一次性的取出来就从表里删了**；「每天」那条（带 `daily` 的）**不删，改排到下一天**
        ——这样桥中途关过、隔几天再起来，也只补发一次，然后接着按那张固定的作息表往下走。

        迟到的处理由桥决定：提醒晚了也还有用，所以和进会那条「错过了就不问」不一样。
        """
        now = time.time() if now is None else now
        with self._lock:
            if self.load_error:
                raise ScheduleError("提醒文件不可读，未触发任务。")
            hit = [it for it in self.items if not it.get("paused") and it["at"] <= now]
            if not hit:
                return []
            fired = {it["id"] for it in hit}
            left = []
            for it in self.items:
                if it["id"] not in fired:
                    left.append(it)
                    continue
                d = it.get("daily")
                if not d:
                    continue                  # 一次性：到点就算发过了，不再留
                nxt = dict(it)
                # slack=0：排到**严格还没到**的下一个点。用默认的宽容度会出事——
                # 09:00 刚发出去，09:00:30 那一跳又把「今天 09:00」算成下一次，一遍遍重发
                nxt["revision"] += 1
                nxt["at"] = _next_daily(d["hour"], d["minute"], now, slack=0)
                left.append(nxt)
            out, history = [], list(self.history)
            for it in hit:
                d = copy.deepcopy(it)
                d.update(late=max(0.0, now-it['at']), run_id=uuid.uuid4().hex)
                out.append(d)
                history.append(dict(id=d['run_id'], schedule_id=it['id'], uid=it['uid'], kind=_kind_of(it),
                                    text=it['text'], scheduled_at=it['at'], fired_at=now,
                                    state='dispatching', detail='正在提交本次触发', task_id=''))
            self._commit(left, history)
            return out

    @staticmethod
    def version(item):
        return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False,
                                         allow_nan=False).encode('utf-8')).hexdigest()

    def snapshot(self, uid):
        with self._lock:
            if self.load_error:
                raise ScheduleError('提醒文件不可读，请检查本机日志和文件后重新读取。')
            return (copy.deepcopy([x for x in self.items if x['uid'] == uid]),
                    copy.deepcopy([x for x in self.history if x.get('uid') == uid]))

    def checked(self, uid, ident, version):
        with self._lock:
            item = next((x for x in self.items if x['uid'] == uid and x['id'] == ident), None)
            if not item or not isinstance(version, str) or self.version(item) != version:
                raise ScheduleError('计划已触发、修改或删除，请重新读取后操作。')
            return copy.deepcopy(item)

    def save_plan(self, uid, plan, ident=None, version=None):
        with self._lock:
            old = self.checked(uid, ident, version) if ident else None
            for other in self.items:
                if other['uid'] != uid or other['id'] == ident or other['text'] != plan['text'] or _kind_of(other) != plan['kind']:
                    continue
                if (plan.get('daily') and other.get('daily')) or (not plan.get('daily') and not other.get('daily') and abs(other['at']-plan['at']) < 60):
                    raise ScheduleError('相同内容已有计划，请编辑原计划或更明确地区分内容。')
            if not old and sum(x['uid'] == uid for x in self.items) >= self.max_pending:
                raise ScheduleError('计划已达上限（%d 条），请先删除不再需要的计划。' % self.max_pending)
            item = dict(plan, uid=uid, id=ident or uuid.uuid4().hex,
                        set_at=old['set_at'] if old else time.time(),
                        paused=old['paused'] if old else False,
                        revision=old['revision']+1 if old else 1)
            self._commit([x for x in self.items if x['id'] != item['id']] + [item])
            return copy.deepcopy(item)

    def manage(self, uid, ident, version, operation, now=None):
        now = time.time() if now is None else now
        with self._lock:
            item = self.checked(uid, ident, version)
            left = [x for x in self.items if x['id'] != ident]
            if operation not in ('pause', 'resume', 'delete'):
                raise ScheduleError('不支持的计划操作。')
            if operation != 'delete':
                item['paused'] = operation == 'pause'
                if operation == 'resume':
                    if item.get('daily'):
                        d = item['daily']; item['at'] = _next_daily(d['hour'], d['minute'], now, slack=0)
                    elif item['at'] <= now + 5:
                        raise ScheduleError('这条一次性计划的时间已过，请先编辑为未来时间再恢复。')
                item['revision'] += 1
                left.append(item)
            self._commit(left)

    def record_run(self, item, state, detail, task_id=''):
        with self._lock:
            history = copy.deepcopy(self.history)
            row = next((x for x in history if x['id'] == item.get('run_id') and x['uid'] == item['uid']), None)
            if row is None:
                return
            row.update(state=state, detail=detail, task_id=task_id)
            self._commit(self.items, history)

    def retry(self, item, at):
        """Requeue a rejected occurrence without replacing a concurrent edit/pause."""
        with self._lock:
            current = next((x for x in self.items if x['id'] == item['id']), None)
            if item.get('daily'):
                if not current or current['revision'] != item['revision']+1 or current['paused']:
                    return False
                nxt = dict(current, at=at, revision=current['revision']+1)
            else:
                if current or sum(x['uid'] == item['uid'] for x in self.items) >= self.max_pending:
                    return False
                nxt = {k: v for k, v in item.items() if k not in ('late', 'run_id')}
                nxt.update(at=at, revision=item['revision']+1)
            self._commit([x for x in self.items if x['id'] != item['id']] + [nxt])
            return True


# ---------- 看清单 / 取消 ----------
# 这两个和新建走的不一样：它们**不用带 /cc**（和「取消定时」「/meetings」一个待遇），
# 所以判定放在桥的免前缀那条链里。
_LIST_WORDS = ("/reminders", "/remind", "/提醒", "看提醒", "有什么提醒",
               "提醒清单", "有哪些提醒", "我的提醒",
               # 定时任务和提醒是同一张表，所以「看任务」也走这条（回的是同一份清单）
               "/tasks", "/任务", "看任务", "有什么任务", "任务清单", "定时任务",
               "有哪些任务", "看定时任务")
# 只认「取消提醒」「取消提醒 2」「取消任务」这几种。夹着别的事的不认，交给 Claude
_CANCEL_RE = re.compile(u"^(?:取消提醒|删掉提醒|删除提醒|取消我的提醒|取消任务|"
                        u"删掉任务|删除任务|取消定时任务)"
                        u"\\s*第?\\s*([0-9０-９一二三四五六七八九十]+)?\\s*条?$")


def parse_list(body):
    """是不是在问「有哪些提醒」。"""
    s = (body or "").strip().lower().rstrip("？?。.!！")
    return s in _LIST_WORDS


def parse_cancel(body):
    """「取消提醒」/「取消提醒 2」→ (True, 序号或 None)；不是这个句式 → (False, None)。

    序号是按时间排的 1 起序号。全角数字先折成半角，中文数字交给 `wemeet._cn_int`。
    """
    s = (body or "").strip()
    m = _CANCEL_RE.match(s)
    if not m:
        return False, None
    raw = m.group(1)
    if not raw:
        return True, None                  # 「取消提醒」= 全取消
    norm = "".join(chr(ord(c) - 0xFEE0) if u"０" <= c <= u"９" else c for c in raw)
    n = wemeet._cn_int(norm)
    if not n or n <= 0:
        return False, None                 # 序号看不懂就当不是这条（交给 Claude）
    return True, n
