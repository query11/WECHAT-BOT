# -*- coding: utf-8 -*-
"""定时提醒：解析、落盘、桥这一层的新建/清单/取消，以及到点推送。

三件事单独说：

**① 解析全部传死 `now`。** 「九点」「明天」这种跟着墙上时钟走，不传死的话同一个用例
半夜跑和中午跑结果不一样（部署指南里那条教训：钟点用例假红）。下面用 `_at()` 造固定的
基准时刻。

**② 顺延是这块唯一和进会/关机相反的地方，所以正反两面都卡住**：
没明说日子、今天那个候选都过了 → 顺延到下一个还没到的（并且 `slipped` 标记为真）；
明说了「今天/今晚」 → 报 `past`、不顺延。

**③ 重复提醒（「每天」）自己一套口径**：钟点取「早的那个读法」（每天九点 = 09:00）、
**不随现在几点跳**；到点**不删、排到下一天**（桥中途关过也只补一次）；同一件事换个钟点
既不悄悄改旧的那条、也不记成两条。别的周期（每周一、每月1号…）一律不认，整句交给 Claude。
"""

import os
import time

import remind
import wechat_bridge

from tests.selftest_base import USER, check, drain, make_bridge, texts, wecom_msg


def _at(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


NOON = _at(2026, 9, 4, 14, 0)        # 下午两点
NIGHT = _at(2026, 9, 4, 22, 30)      # 晚上十点半
MORNING = _at(2026, 9, 4, 8, 0)      # 早上八点


def test_remind_parse():
    """解析：认得出哪些、各算成几点、哪些该交给 Claude。"""
    p = remind.parse_remind

    def got(text, now=NOON):
        return p(text, now) or {}

    # ---- 该认的（下午两点这个基准）----
    for text, want in (("九点提醒我吃药", "21:00"),
                       ("提醒我九点吃药", "21:00"),
                       ("提醒我吃药，九点", "21:00"),
                       ("晚上九点提醒我看报告", "21:00"),
                       ("今晚八点提醒我看电视", "20:00"),
                       ("明天九点提醒我交房租", "明天 09:00"),
                       ("后天下午三点提醒我开会", "09-06 15:00"),
                       ("20分钟后提醒我看锅", "14:20"),
                       ("一个半小时后提醒我看锅", "15:30")):
        r = got(text)
        check("认得出「%s」" % text, r.get("kind") == "remind" and r.get("label") == want,
              r)

    check("提醒内容被单独抠出来了",
          got("明天九点提醒我交房租").get("text") == "交房租",
          got("明天九点提醒我交房租"))
    check("动词在前也能抠对内容",
          got("提醒我看报告，明天九点").get("text") == "看报告",
          got("提醒我看报告，明天九点"))
    check("没顺延的不标 slipped", got("明天九点提醒我交房租").get("slipped") is False)

    # ---- 顺延：这块唯一和进会/关机相反的口径 ----
    r = got("九点提醒我吃药", NIGHT)
    check("晚上十点半说「九点」→ 顺延到明天 09:00（最近的还没到的那个）",
          r.get("label") == "明天 09:00" and r.get("slipped") is True, r)
    r = got("八点提醒我吃药", NIGHT)
    check("晚上十点半说「八点」→ 明天 08:00", r.get("label") == "明天 08:00", r)
    check("早上八点说「九点」→ 今天 09:00（还没到就不滚）",
          got("九点提醒我吃药", MORNING).get("label") == "09:00",
          got("九点提醒我吃药", MORNING))

    # ---- 明说了日子就不滚 ----
    for text in ("今晚九点提醒我吃药", "今天九点提醒我吃药"):
        r = got(text, NIGHT)
        check("「%s」说过了今天，就报 past、不顺延" % text,
              r.get("kind") == "past" and r.get("label") == ("21:00" if "九点" in text and "晚" in text else "09:00"),
              r)

    # ---- 该报 empty 的：有时间有词，但没说要提醒什么 ----
    check("「九点提醒我」缺内容", got("九点提醒我").get("kind") == "empty",
          got("九点提醒我"))
    check("「一个半小时后提醒我」缺内容",
          got("一个半小时后提醒我").get("kind") == "empty",
          got("一个半小时后提醒我"))

    # ---- 该交给 Claude 的（宁可漏认不误认）----
    for text in ("提醒功能怎么用", "别提醒我九点吃药", "不用提醒我明天九点吃药",
                 "取消提醒", "取消提醒 2", "九点吃药", "提醒我吃药",
                 "把提醒关掉", "提醒我" + "啊" * 400):
        check("不该认成提醒：%s" % text, p(text, NOON) is None)

    # ---- 太远的 ----

    # ---- 太远的。**必须用小时**：「10天后」这种日子+天的说法 _match_time 根本不收
    # （那是刻意留给 Claude 的），所以那样写认不出来，不是 far ----
    check("「10天后」这种写法不认（日子+天的说法留给 Claude）",
          p("10天后提醒我交房租", NOON) is None)
    r = p("200小时后提醒我交房租", NOON) or {}
    check("超过 7 天报 far", r.get("kind") == "far", r)


def test_remind_list_cancel_parse():
    """清单和取消的句式（这两个不用带前缀）。"""
    for s in ("/reminders", "/remind", "/提醒", "看提醒", "有什么提醒", "提醒清单"):
        check("认得出清单说法：%s" % s, remind.parse_list(s))
    for s in ("提醒我看报告", "取消关机", "/meetings"):
        check("不误认成清单：%s" % s, not remind.parse_list(s))

    for s, want in (("取消提醒", (True, None)), ("取消提醒 2", (True, 2)),
                    ("取消提醒第3条", (True, 3)), ("取消提醒 ２", (True, 2)),
                    ("取消提醒二", (True, 2)), ("删掉提醒", (True, None))):
        check("认得出取消：%s" % s, remind.parse_cancel(s) == want, remind.parse_cancel(s))
    for s in ("取消提醒所有", "取消关机", "别取消提醒", "取消提醒然后看日志"):
        check("不误认成取消：%s" % s, remind.parse_cancel(s)[0] is False,
              remind.parse_cancel(s))


def test_remind_store(tmpdir):
    """落盘表：加、查、按序号取消、到点取出来、重启还在。"""
    path = os.path.join(tmpdir, "reminders.json")
    st = remind.ReminderStore(path, max_pending=3)
    now = NOON

    a, err = st.add(USER, now + 600, "15:00", "交房租")
    check("加得上", a and not err, (a, err))
    _, err = st.add(USER, now + 600, "15:00", "交房租")
    check("同一条不重复记", err == "same", err)
    st.add(USER, now + 1200, "15:10", "吃药")
    st.add(USER, now + 1800, "15:20", "取快递")
    _, err = st.add(USER, now + 2400, "15:30", "第 4 条")
    check("定满了明说", err == "full", err)
    check("空内容不收", st.add(USER, now + 60, "14:01", "  ")[1] == "empty")

    got = st.all(USER)
    check("按时间排", [x["text"] for x in got] == ["交房租", "吃药", "取快递"],
          [x["text"] for x in got])
    check("落盘了", os.path.exists(path))

    reloaded = remind.ReminderStore(path, max_pending=3)
    check("重启后还在", len(reloaded.all(USER)) == 3, reloaded.all(USER))

    check("序号越界返回 None", st.cancel(USER, 9) is None)
    one = st.cancel(USER, 2)
    check("按序号取消取的是第 2 条", [x["text"] for x in one] == ["吃药"], one)
    check("取消后只剩两条", [x["text"] for x in st.all(USER)] == ["交房租", "取快递"],
          st.all(USER))
    rest = st.cancel(USER)
    check("不給序号就全取消", len(rest) == 2 and st.all(USER) == [], rest)
    check("再取消一次不报错", st.cancel(USER) == [])

    # ---- 到点 ----
    st2 = remind.ReminderStore(os.path.join(tmpdir, "r2.json"))
    st2.add(USER, now + 10, "14:00", "到点了")
    st2.add(USER, now + 9000, "16:30", "还早")
    due = st2.due(now + 60)
    check("只取到点的", [x["text"] for x in due] == ["到点了"], due)
    check("带上迟了多少秒", due and abs(due[0]["late"] - 50) < 2, due)
    check("取出来就从表里删了", st2.due(now + 60) == [])
    check("还早那条留着", [x["text"] for x in st2.all(USER)] == ["还早"], st2.all(USER))


def test_remind_chain(tmpdir, fake_claude):
    """桥这一层：新建、清单、取消、到点推送。"""
    b = make_bridge(tmpdir, fake_claude)
    b.reminders = remind.ReminderStore(os.path.join(tmpdir, "rem.json"))
    b.remind_stale_seconds = 24 * 3600

    drain(b, [wecom_msg("/cc 明天九点提醒我交房租", "r1")])
    t = texts()
    check("桥拦下了提醒请求", any("记下了" in x for x in t), t)
    check("回话里报出了理解成的时刻", any("明天 09:00" in x for x in t), t)
    check("回话里提醒了「开关得开着」", any("开关得开着" in x for x in t), t)
    check("新建提醒不花额度", not any("已处理" in x for x in t), t)
    check("真记进表了", len(b.reminders.all(USER)) == 1, b.reminders.all(USER))

    drain(b, [wecom_msg("看提醒", "r2")])          # 不带前缀也认
    t = texts()
    check("清单不用带前缀", any("记下的提醒" in x for x in t), t)
    check("清单里有内容", any("交房租" in x for x in t), t)

    drain(b, [wecom_msg("/cc 明天九点提醒我交房租", "r3")])
    check("重复那条会说「已经记过了」",
          any("已经记过" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 明天十点提醒我吃药", "r4")])
    drain(b, [wecom_msg("取消提醒 1", "r5")])       # 不带前缀
    t = texts()
    check("按序号取消", any("取消了" in x and "交房租" in x for x in t), t)
    check("表里只剩吃药", [x["text"] for x in b.reminders.all(USER)] == ["吃药"],
          b.reminders.all(USER))

    drain(b, [wecom_msg("取消提醒 9", "r6")])
    check("序号越界会说没有第 9 条", any("没有第 9 条" in x for x in texts()), texts())

    drain(b, [wecom_msg("取消提醒", "r7")])
    check("不给序号全取消", any("取消了" in x for x in texts()), texts())
    check("表空了", b.reminders.all(USER) == [], b.reminders.all(USER))
    drain(b, [wecom_msg("取消提醒", "r8")])
    check("再取消一次如实说「本来就没有」",
          any("本来就没有" in x for x in texts()), texts())

    # ---- 到点：直接发，不问确认 ----
    at = time.time() + 5
    b.reminders.add(USER, at, "现在", "该吃药了")
    b._remind_tick(at + 10)
    t = texts()
    check("到点发出来了", any("提醒你：该吃药了" in x for x in t), t)
    check("不用回「确认」就发了（和进会不一样）", not b._pending_meeting, b._pending_meeting)
    check("发完就从表里删了", b.reminders.all(USER) == [], b.reminders.all(USER))

    # ---- 迟到：晚一点照发，但写明晚了多久 ----
    b.reminders.add(USER, time.time() - 900, "刚才", "该喝水了")
    b._remind_tick()
    t = texts()
    check("晚 15 分钟还是发了", any("该喝水了" in x for x in t), t)
    check("并且写明晚了多久", any("晚了 15 分钟" in x for x in t), t)

    # ---- 太旧：只说明一句，不打扰 ----
    b.reminders.add(USER, time.time() - 3 * 86400, "三天前", "早过期了")
    b._remind_tick()
    t = texts()
    check("超过 stale 的不再报内容", not any("早过期了" in x for x in t), t)
    check("但会说一句它作废了", any("过去太久了" in x for x in t), t)


def test_remind_off_and_gated(tmpdir, fake_claude):
    """功能关掉时说清楚；而且新建**要带前缀**（清单/取消不用）。"""
    b = make_bridge(tmpdir, fake_claude)          # 默认 remind.enable=False
    check("默认没装提醒表", b.reminders is None, b.reminders)

    drain(b, [wecom_msg("/cc 明天九点提醒我交房租", "g1")])
    t = texts()
    check("关着时直说没启用", any("提醒的功能是关着的" in x for x in t), t)
    check("关着时不落回 Claude（直说比让它去解释有用）",
          not any("已处理" in x for x in t), t)

    b2 = make_bridge(tmpdir, fake_claude)
    b2.reminders = remind.ReminderStore(os.path.join(tmpdir, "g.json"))
    drain(b2, [wecom_msg("明天九点提醒我交房租", "g2")])     # 没带 /cc
    check("新建提醒要带前缀（不带就照旧跳过）", not texts(), texts())

    b3 = make_bridge(tmpdir, fake_claude)
    b3.reminders = remind.ReminderStore(os.path.join(tmpdir, "g3.json"))
    b3._chat[USER] = time.time()
    drain(b3, [wecom_msg("明天九点提醒我交房租", "g3")])
    check("对话模式里免前缀照样能建", len(b3.reminders.all(USER)) == 1,
          b3.reminders.all(USER))


def test_remind_config(tmpdir):
    """配置层：默认值、坏值、关掉。"""
    check("默认就开着", wechat_bridge.DEFAULTS["remind"]["enable"] is True)
    check("默认最多 10 条", wechat_bridge.DEFAULTS["remind"]["max_pending"] == 10)
    check("默认 stale 24 小时", wechat_bridge.DEFAULTS["remind"]["stale_hours"] == 24)
    check("关掉就返回 None",
          wechat_bridge.make_reminders({"remind": {"enable": False}}) is None)
    st = wechat_bridge.make_reminders({"remind": {"enable": True, "max_pending": 2},
                                       "workdir": tmpdir})
    check("开着就装上", isinstance(st, remind.ReminderStore), st)
    check("max_pending 传下去了", st.max_pending == 2, st.max_pending)
    check("落盘文件名是 reminders.json", st.path.endswith("reminders.json"), st.path)
    check("配置里写坏值也整块不装",
          wechat_bridge.make_reminders({"remind": {"enable": True,
                                                   "max_pending": "不是数"}}) is None)


def test_remind_daily_parse():
    """「每天」那种重复：认哪三个词、钟点取哪个读法、别的周期一概不认。"""
    p = remind.parse_remind

    # ---- 认得出，标签里写明是「每天」----
    for text, want, hour, minute in (("每天早上九点提醒我吃药", "每天 09:00", 9, 0),
                                     ("每天9点提醒我吃药", "每天 09:00", 9, 0),
                                     ("每日下午三点提醒我开会", "每天 15:00", 15, 0),
                                     ("每天晚上八点半提醒我吃药", "每天 20:30", 20, 30),
                                     ("每一天23点提醒我睡觉", "每天 23:00", 23, 0)):
        r = p(text, NOON) or {}
        check("认得出「%s」是 %s" % (text, want),
              r.get("kind") == "remind" and r.get("label") == want
              and r.get("daily") == {"hour": hour, "minute": minute}, r)

    check("「每天」那条的正文也抠干净了",
          (p("每天早上九点提醒我吃药", NOON) or {}).get("text") == "吃药",
          p("每天早上九点提醒我吃药", NOON))
    check("每天那条不标 slipped（重复的没有「顺延」一说）",
          (p("每天早上九点提醒我吃药", NOON) or {}).get("slipped") is False)

    # ---- 「每天九点」取早的那个读法，且**不随现在几点跳** ----
    # 这是和一次性提醒最要紧的差别：一次性靠「哪个还没到」消歧（过了的那个没用），
    # 而重复提醒描述的是一张固定的作息表，跟现在几点无关——不这样的话，
    # 同一条「每天九点」早上说和晚上说会变成两个不同的钟点。
    for base in (NOON, NIGHT, _at(2026, 9, 4, 2, 0), _at(2026, 9, 4, 9, 0),
                 _at(2026, 9, 4, 21, 30)):
        r = p("每天早上九点提醒我吃药", base) or {}
        check("现在 %s 说「每天九点」，意思还是每天 09:00"
              % time.strftime("%H:%M", time.localtime(base)),
              r.get("daily") == {"hour": 9, "minute": 0}, r)

    # ---- 下一次：今天那个还没到就今天，过了就明天 ----
    check("每天九点（现在 08:00）→ 今天 09:00 就响",
          abs((p("每天九点提醒我吃药", _at(2026, 9, 4, 8, 0)) or {})["at"]
              - _at(2026, 9, 4, 9, 0)) < 2)
    check("每天九点（现在 10:00）→ 排到明天 09:00",
          abs((p("每天九点提醒我吃药", _at(2026, 9, 4, 10, 0)) or {})["at"]
              - _at(2026, 9, 5, 9, 0)) < 2)
    check("整点那一刻说「每天九点」算今天（别白等一天）",
          abs((p("每天九点提醒我吃药", _at(2026, 9, 4, 9, 0)) or {})["at"]
              - _at(2026, 9, 4, 9, 0)) < 2)

    # ---- empty：光有「每天九点」没说提醒什么，也要把下一次带上 ----
    r = p("每天九点提醒我", NOON) or {}
    check("「每天九点提醒我」缺内容报 empty，但带上了每天几点",
          r.get("kind") == "empty" and r.get("label") == "每天 09:00"
          and r.get("at"), r)

    # ---- 该不认的（宁可漏认不误认）----
    # 尤其是「每周一」这种：以前会当成一次性提醒记成「明天九点提醒你：每周一吃药」，
    # 只响一次、还在错的那天——比不认更坏，所以现在整句交给 Claude。
    for text in ("每天10分钟后提醒我看锅",     # 每天 + 相对时长：自相矛盾
                 "每天明天九点提醒我吃药",     # 每天 + 明说的日子：自相矛盾
                 "提醒我每天早上吃药",         # 「每天」是内容的一部分，不是周期
                 "每周一早上九点提醒我吃药",   # 礼拜口径：交给 Claude
                 "每星期一九点提醒我吃药",
                 "每个工作日九点提醒我吃药",   # 要排节假日
                 "每月1号九点提醒我吃药",      # 要排大小月
                 "每年元旦九点提醒我吃药",
                 "每隔一天九点提醒我吃药"):
        check("不该认成每天：%s" % text, p(text, NOON) is None, p(text, NOON))

    # ---- 「每天」的归一：表里的 daily 要么没有、要么是干净的整数 ----
    check("daily 是整数就收",
          remind._daily_of({"daily": {"hour": 9, "minute": 5}})
          == {"hour": 9, "minute": 5})
    check("字符串数字也收（json 读回来可能是这样）",
          remind._daily_of({"daily": {"hour": "9", "minute": "5"}})
          == {"hour": 9, "minute": 5})
    for bad in ({"daily": {"hour": 24, "minute": 0}},
                {"daily": {"hour": 9, "minute": 60}},
                {"daily": {"hour": 9}},
                {"daily": "每天九点"}, {"daily": None}, {}):
        check("不成样子的 daily 一律当没有：%s" % (bad,),
              remind._daily_of(bad) is None, remind._daily_of(bad))


def test_remind_daily_store(tmpdir):
    """「每天」那条的排期：到点不删、同一分钟只响一次、闹钟停过也只补一次。"""
    path = os.path.join(tmpdir, "daily.json")
    st = remind.ReminderStore(path, max_pending=5)
    plan = remind.parse_remind("每天早上九点提醒我吃药", NOON)
    item, err = st.add(USER, plan["at"], plan["label"], plan["text"],
                       daily=plan["daily"])
    check("每天那条加得上", item and not err, (item, err))
    check("每天几点落进了表里",
          item and item["daily"] == {"hour": 9, "minute": 0}, item)
    first = item["at"]

    check("还没到点不响", st.due(NOON + 60) == [])
    got = st.due(first + 10)
    check("到点响了", [x["text"] for x in got] == ["吃药"], got)
    check("写明迟了 10 秒", got and abs(got[0]["late"] - 10) < 1, got)
    check("★发完**没有**从表里删掉（明天还得响）",
          [x["text"] for x in st.all(USER)] == ["吃药"], st.all(USER))
    check("已经排到第二天同一时刻",
          abs(st.all(USER)[0]["at"] - (first + 86400)) < 2, st.all(USER))
    check("同一分钟里再问一次不会又响一遍", st.due(first + 30) == [])
    check("第二天到点再响一次",
          [x["text"] for x in st.due(first + 86400 + 5)] == ["吃药"])

    # ---- 同一件事、同一个钟点：直说「记过了」 ----
    p2 = remind.parse_remind("每天早上九点提醒我吃药", NOON)
    check("同一件事同一个钟点不重复记",
          st.add(USER, p2["at"], p2["label"], p2["text"], daily=p2["daily"])[1]
          == "same")

    # ---- 同一件事、换个钟点：既不悄悄改旧的，也不记成两条 ----
    p3 = remind.parse_remind("每天晚上八点半提醒我吃药", NOON)
    old, err = st.add(USER, p3["at"], p3["label"], p3["text"], daily=p3["daily"])
    check("同一件事换钟点报 dup", err == "dup", err)
    check("dup 把旧的那条带回来（好让桥说清是几点）",
          old and old["label"] == "每天 09:00", old)
    check("没有偷偷记成两条", len(st.all(USER)) == 1, st.all(USER))
    check("旧的钟点没被悄悄改掉",
          st.all(USER)[0]["daily"] == {"hour": 9, "minute": 0}, st.all(USER))

    # ---- 落盘读回：「每天」这个标记不能丢 ----
    reloaded = remind.ReminderStore(path, max_pending=5)
    check("重启后「每天」还在",
          [x["daily"] for x in reloaded.all(USER)] == [{"hour": 9, "minute": 0}],
          reloaded.all(USER))
    check("重启后还是那条", [x["text"] for x in reloaded.all(USER)] == ["吃药"])

    # ---- 闹钟响了三天没人管：只补一次，不补三条 ----
    st2 = remind.ReminderStore(os.path.join(tmpdir, "daily2.json"))
    st2.add(USER, first, "每天 09:00", "吃药", daily={"hour": 9, "minute": 0})
    got = st2.due(first + 3 * 86400 + 5)
    check("桥停了三天只补发一条（不是三条）", len(got) == 1, got)
    check("补发的写明了晚了多久", got and got[0]["late"] > 3 * 86400 - 10, got)
    check("并且排到下一个还没到的九点",
          abs(st2.all(USER)[0]["at"] - _at(2026, 9, 9, 9, 0)) < 2,
          st2.all(USER))

    # ---- 「每天」和一次性混在一起时互不干扰 ----
    st3 = remind.ReminderStore(os.path.join(tmpdir, "daily3.json"))
    st3.add(USER, first, "每天 09:00", "吃药", daily={"hour": 9, "minute": 0})
    st3.add(USER, first, "明天 09:00", "交房租")
    got = st3.due(first + 5)
    check("同一个时刻两条都响",
          sorted(x["text"] for x in got) == ["交房租", "吃药"], got)
    check("一次性的删了、每天的排走了",
          [(x["text"], x["daily"] is not None) for x in st3.all(USER)]
          == [("吃药", True)], st3.all(USER))

    # ---- 取消：每天那条也是普通一条，一次就干净 ----
    check("按序号取消每天那条", [x["text"] for x in st.cancel(USER, 1)] == ["吃药"])
    check("取消后表空了", st.all(USER) == [])


def test_remind_daily_chain(tmpdir, fake_claude):
    """桥这一层：每天那条的说法、清单、换钟点的答复，以及发完还在表里。"""
    b = make_bridge(tmpdir, fake_claude)
    b.reminders = remind.ReminderStore(os.path.join(tmpdir, "dchain.json"))
    b.remind_stale_seconds = 24 * 3600

    drain(b, [wecom_msg("/cc 每天早上九点提醒我吃药", "dd1")])
    t = texts()
    check("桥记下了每天那条", len(b.reminders.all(USER)) == 1, b.reminders.all(USER))
    check("回话里写明「每天 09:00」", any("每天 09:00" in x for x in t), t)
    check("回话说明这是每天的（不用天天重说）",
          any("每天到点都会发" in x for x in t), t)
    check("daily 真落进了表里",
          b.reminders.all(USER)[0]["daily"] == {"hour": 9, "minute": 0},
          b.reminders.all(USER))

    drain(b, [wecom_msg("看提醒", "dd2")])
    check("清单里带「每天」", any("每天 09:00" in x for x in texts()), texts())

    # ---- 到点：发一条，表里还在（这是和一次性最要紧的差别）----
    b.reminders = remind.ReminderStore(os.path.join(tmpdir, "dchain2.json"))
    at = time.time() + 5
    b.reminders.add(USER, at, "每天 09:00", "该吃药了",
                    daily={"hour": 9, "minute": 0})
    b._remind_tick(at + 10)
    check("到点发出来了", any("提醒你：该吃药了" in x for x in texts()), texts())
    check("★发完没被删掉（明天还得响）", len(b.reminders.all(USER)) == 1,
          b.reminders.all(USER))
    check("已经排到下一次", b.reminders.all(USER)[0]["at"] > at + 10,
          b.reminders.all(USER))
    b._remind_tick(at + 20)
    check("同一跳再 tick 不会又发一遍",
          len([x for x in texts() if "该吃药了" in x]) == 1, texts())

    # ---- 同一件事换个钟点：说清楚，不记两条 ----
    b.reminders = remind.ReminderStore(os.path.join(tmpdir, "dchain3.json"))
    b.reminders.add(USER, time.time() + 3600, "每天 09:00", "吃药",
                    daily={"hour": 9, "minute": 0})
    drain(b, [wecom_msg("/cc 每天晚上八点半提醒我吃药", "dd3")])
    t = texts()
    check("换个钟点会说清「已经有一条每天 09:00 的了」",
          any("已经有一条「每天 09:00」" in x for x in t), t)
    check("并告诉你怎么改（先「取消提醒」再重说）",
          any("取消提醒" in x and "重说" in x for x in t), t)
    check("没有偷偷记成两条", len(b.reminders.all(USER)) == 1, b.reminders.all(USER))

    # ---- 取消每天那条 ----
    drain(b, [wecom_msg("取消提醒", "dd4")])
    check("每天那条取消得掉", b.reminders.all(USER) == [], b.reminders.all(USER))
    check("回话里报的是每天那条", any("每天 09:00" in x for x in texts()), texts())


# ---------- 「到点跑一句话」（定时任务）----------

def test_task_parse():
    """「到点跑一句话」：认什么、不认什么。**时间口径和提醒共用一份**。"""
    p = remind.parse_task

    r = p("明天九点帮我跑：把那个表处理一下", NOON) or {}
    check("认得出「明天九点帮我跑：…」",
          r.get("kind") == "task" and r.get("label") == "明天 09:00"
          and r.get("text") == "把那个表处理一下", r)
    check("★时间口径和提醒就是同一份（只是把触发词换成 _RUN_WORDS）",
          p("明天九点帮我跑备份", NOON)["at"]
          == remind.parse_remind("明天九点提醒我备份", NOON)["at"])
    check("「帮我跑一下备份」抠出来的正文不带「一下」",
          (p("下午三点帮我跑一下备份", NOON) or {}).get("text") == "备份",
          p("下午三点帮我跑一下备份", NOON))
    check("「九点帮我执行一下那个脚本」也认",
          (p("九点帮我执行一下那个脚本", NOON) or {}).get("text") == "那个脚本")
    check("「到点跑一下备份，明天九点」这种倒装也认",
          (p("到点跑一下备份，明天九点", NOON) or {}).get("text") == "备份")

    r = p("每天早上九点帮我跑一下备份", NOON) or {}
    check("「每天…帮我跑」是重复任务（每天跑一次）",
          r.get("kind") == "task" and r.get("label") == "每天 09:00"
          and r.get("daily") == {"hour": 9, "minute": 0}, r)

    check("说了时间没说跑什么 → empty，桥会回来问一句",
          (p("明天九点帮我跑", NOON) or {}).get("kind") == "empty",
          p("明天九点帮我跑", NOON))
    check("太远的报 far（和提醒一个口径）",
          (p("200小时后帮我跑备份", NOON) or {}).get("kind") == "far")
    check("明说了今天、那个点过了 → past（不顺延）",
          (p("今天九点帮我跑备份", _at(2026, 9, 4, 22, 0)) or {}).get("kind") == "past")

    # ---- 不该认的。**最要紧的一条：「跑」这个字说的是人自己** ----
    for text in ("提醒我明天九点跑那个脚本", "明天九点提醒我吃药",
                 "我明天九点要跑一下步", "怎么定时跑一个脚本",
                 "帮我看看怎么让它定时跑", "别帮我跑备份", "不用帮我跑备份",
                 "别定时跑备份", "取消任务", "每周一早上九点帮我跑备份",
                 "明天九点帮我跑" + "啊" * 400, "帮我跑一下备份", "明天九点帮我干活"):
        check("不认成定时任务：%s" % text[:16], p(text, NOON) is None, p(text, NOON))

    # 和提醒各认各的：带「提醒我」的归提醒，任务解析一个字都不认
    check("「提醒我」那句归提醒",
          remind.parse_remind("明天九点提醒我交房租", NOON) is not None)
    check("任务解析不抢「提醒我」那句",
          p("明天九点提醒我交房租", NOON) is None)
    check("提醒解析也不抢「帮我跑」那句",
          remind.parse_remind("明天九点帮我跑备份", NOON) is None)


def test_task_store(tmpdir):
    """同一张表里的两种东西：kind 分得开、互不误删、落盘读得回来。"""
    path = os.path.join(tmpdir, "tasks.json")
    st = remind.ReminderStore(path)
    a, err = st.add(USER, NOON + 600, "15:00", "备份", kind="task")
    check("任务加得上", a and not err, (a, err))
    check("kind 记下来了", a and a["kind"] == "task", a)

    b, err = st.add(USER, NOON + 600, "15:00", "备份", kind="remind")
    check("同一句话的「提醒」是另一条（kind 不同，不算重复）", b and not err, (b, err))
    check("两条都在，kind 各是各的",
          sorted(remind._kind_of(x) for x in st.all(USER)) == ["remind", "task"],
          st.all(USER))
    _, err = st.add(USER, NOON + 600, "15:00", "备份", kind="task")
    check("同一条任务再来一次才算重复", err == "same", err)

    # 落盘读回：kind 不能丢（丢了任务会变成一条普通提醒：**真去执行**变成只发消息）。
    # **必须在 due() 之前看**：due() 会把到点的一次性条目从表里删掉（那正是它的活）
    back = remind.ReminderStore(path)
    check("重启后 kind 还在（那条任务还是任务）",
          len(back.all(USER)) == 2
          and sorted(remind._kind_of(x) for x in back.all(USER)) == ["remind", "task"],
          back.all(USER))
    check("落盘文件里真写了 kind",
          "task" in open(path, encoding="utf-8").read())

    got = st.due(NOON + 900)
    check("到点两条一起出来（谁也不会被漏掉）",
          sorted(x["text"] for x in got) == ["备份", "备份"], got)
    check("两条都带上 kind（桥靠它决定「发一条」还是「去跑」）",
          sorted(remind._kind_of(x) for x in got) == ["remind", "task"], got)

    check("老数据（没有 kind）一律当提醒",
          remind._kind_of({}) == "remind" and remind._kind_of({"kind": "remind"}) == "remind")


def test_task_chain(tmpdir, fake_claude):
    """桥这一层：定任务、看清单、到点入队交给 Claude、太旧的不跑、太满的先排队。"""
    b = make_bridge(tmpdir, fake_claude)
    b.reminders = remind.ReminderStore(os.path.join(tmpdir, "tchain.json"))
    b.remind_stale_seconds = 24 * 3600
    b._last_ctx[USER] = (USER, "ctx-1")

    drain(b, [wecom_msg("/cc 明天九点帮我跑：把那个表处理一下", "t1")])
    t = texts()
    check("桥拦下了定时任务", any("交给 Claude 跑一遍" in x for x in t), t)
    check("回话里说清这是真去执行、会花额度", any("会花额度" in x for x in t), t)
    check("回话里报出了理解成的时刻", any("明天 09:00" in x for x in t), t)
    check("定任务不花额度（不是现在就跑）", not any("已处理" in x for x in t), t)
    items = b.reminders.all(USER)
    check("表里那条是 task",
          items and remind._kind_of(items[0]) == "task", items)

    drain(b, [wecom_msg("看提醒", "t2")])
    t = texts()
    check("清单里标出「到点跑：」", any("到点跑：把那个表处理一下" in x for x in t), t)
    check("清单里说明它会真去执行", any("真去执行" in x for x in t), t)

    # ---- 到点：先回一句，再入队交给 Claude（走 _enqueue 唯一的门）----
    drain(b, [])
    at = time.time() + 5
    b.reminders.add(USER, at, "刚才", "看一下 D 盘有哪些文件夹", kind="task")
    b._remind_tick(at + 3)
    b.queue.join()                     # 假 claude 秒回，等它跑完
    time.sleep(0.2)
    t = texts()
    check("到点先说一句「交给 Claude 去跑了」", any("交给 Claude 去跑" in x for x in t), t)
    check("真的入队跑了（假 claude 回了一条「已处理」）",
          any("已处理" in x for x in t), t)
    check("一次性的：跑完就从表里没了",
          not any(x["text"] == "看一下 D 盘有哪些文件夹"
                  for x in b.reminders.all(USER)), b.reminders.all(USER))

    # ---- 太旧的不跑（和提醒刻意不同：提醒照发，任务不照跑）----
    drain(b, [])
    b.reminders.add(USER, time.time() - 3 * 86400, "三天前", "把那个表删了", kind="task")
    b._remind_tick()
    b.queue.join()
    time.sleep(0.2)
    t = texts()
    check("太旧的任务不执行", not any("已处理" in x for x in t), t)
    check("但会说一句它作废了（还把原话念回来）",
          any("过去太久了" in x and "把那个表删了" in x for x in t), t)

    # ---- 队列满：**不许悄悄丢掉**，排到一分钟后重试 ----
    drain(b, [])
    b._enqueue = lambda item, msg: "❌ 队列满了"
    when = time.time()
    b.reminders.add(USER, when - 1, "刚才", "备份一下", kind="task")
    b._remind_tick()
    time.sleep(0.1)
    t = texts()
    check("队列满时回了话", any("过一分钟再试" in x for x in t), t)
    left = [x for x in b.reminders.all(USER) if x["text"] == "备份一下"]
    check("★没丢掉：排到一分钟后再来",
          len(left) == 1 and left[0]["at"] > when + 30, left)

    # ---- 提醒照旧：到点只发一条，不入队 ----
    drain(b, [])
    b.reminders.add(USER, time.time() - 5, "刚才", "该吃药了")
    b._remind_tick()
    time.sleep(0.1)
    t = texts()
    check("提醒还是老样子：发一条，不交给 Claude",
          any("提醒你：该吃药了" in x for x in t)
          and not any("已处理" in x for x in t), t)

    # ---- 取消：「取消任务」和「取消提醒」是同一条路（同一张表）----
    b.reminders.add(USER, time.time() + 3600, "一小时后", "备份", kind="task")
    drain(b, [wecom_msg("取消任务", "t9")])
    t = texts()
    check("「取消任务」也能取消", any("取消了" in x for x in t), t)
    check("回话里把任务标出来（「到点跑：」）", any("到点跑：" in x for x in t), t)

    # ---- 关掉提醒 = 任务也关掉（共用一张表）----
    off = make_bridge(tmpdir, fake_claude)          # 默认 remind.enable=False
    drain(off, [wecom_msg("/cc 明天九点帮我跑备份", "t10")])
    check("提醒关着时任务也直说关着", any("共用一张表" in x for x in texts()), texts())
