# -*- coding: utf-8 -*-
"""进腾讯会议 + 定时进会：怎么认会议号、拼链接、到点先问一句，以及各种边界。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。

NOW_1838 是钉死的那个「2026-08-30 18:38」——定时那几条都拿它当「现在」，
不然测试在真实时间里跑，过了 18:38 结果就变。
"""

import logging
import os
import time

import wechat_bridge
import wemeet

from tests.selftest_base import (ACCOUNT, BASE, OTHER, USER, FakeILink, check, drain,
                           make_bridge, make_launcher, texts, wecom_msg)


# ---------- 进腾讯会议 ----------

def test_meeting_parse():
    """真机原话在这组里：「帮我打开腾讯会议，输入会议号，进会」和「再试一下打开腾讯会议」
    当初都白花了一次额度，这组盯着它们别再退化。裸数字串必须不认。"""
    cases = [
        ("进会 413231713", ("413231713", None)),
        ("进会413231713", ("413231713", None)),
        ("帮我打开腾讯会议 413-231-713", ("413231713", None)),
        ("加入会议413231713", ("413231713", None)),
        ("腾讯会议 4132317131", ("4132317131", None)),
        ("进会 413231713 密码 8888", ("413231713", "8888")),
        ("进会 413-231-713 入会密码 abc123", ("413231713", "abc123")),
        # 下面这些一个都不许认
        ("413231713", (None, None)),                       # 裸数字串：可能是别的号
        ("我手机号是13812345678", (None, None)),
        ("进会 4132317138888", (None, None)),              # 连着 13 位，分不清哪段是号
        ("进会 413231713 和 987654321", (None, None)),      # 两串，不猜
        ("进会 12345", (None, None)),                      # 位数不够
        ("进会 https://meeting.tencent.com/dm/abc", (None, None)),   # 链接交回 Claude
        ("会议纪要放在 D:\\会议\\413231713.md", (None, None)),
        ("打开腾讯会议", (None, None)),                     # 没号，该走开应用那条
        ("帮我打开腾讯会议，输入会议号，进会", (None, None)),
        ("再试一下打开腾讯会议", (None, None)),
        ("明天开会记得叫我", (None, None)),
        # 否定、推迟、还夹着别的事：一律交回 Claude，它能问清楚，桥不能
        ("别进 413231713 那个会", (None, None)),
        ("这个会不用进 413231713", (None, None)),
        ("明天进会 413231713", (None, None)),
        ("待会进会 413231713", (None, None)),
        ("进会 413231713 然后把纪要发我", (None, None)),
        ("#腾讯会议：413231713 记到日程里", (None, None)),
    ]
    for text, want in cases:
        got = wemeet.parse_join(text)
        check("进会解析：%s" % text, got == want, got)

    asks = ["进会", "入会", "进会议", "帮我进会吧", "能不能进会", "加入会议"]
    for text in asks:
        check("没给号的「%s」要问一句" % text, wemeet.parse_join_asking(text) is True)
    for text in ["打开腾讯会议", "进会 413231713", "进会议室看一下投影", "会议记录发我",
                 "开会", "结束会议"]:
        check("「%s」不当成「等着我给号」" % text,
              wemeet.parse_join_asking(text) is False, text)
    check("9 位号断成 3-3-3 好核对", wemeet.pretty_code("413231713") == "413-231-713",
          wemeet.pretty_code("413231713"))


def test_meeting_invite():
    """整段转过来的腾讯邀请文案也要认（用户 2026-08-30 要的）。

    那段里必然有 `https://meeting.tencent.com/dm/…` 和一堆日期时段数字，短句通道
    会（也应该）拒掉它；靠的是 `#腾讯会议：<号>` 这个腾讯自己生成的标记。这组盯住
    两件事：整段能认出来，以及标记之外的数字（日期、时段、链接尾巴）不许被当成号。
    """
    invite = ("我预定了腾讯会议，等你来开会哦~\n"
              "会议主题：周三周会\n"
              "会议时间：2026/08/30 20:00-21:00\n"
              "点击链接入会，或添加至会议列表：\n"
              "https://meeting.tencent.com/dm/abcdEFGH12345\n"
              "#腾讯会议：413-231-713")
    check("整段邀请文案能认出号", wemeet.parse_join(invite) == ("413231713", None),
          wemeet.parse_join(invite))
    check("邀请文案里带密码也认",
          wemeet.parse_join(invite + "\n会议密码：8888") == ("413231713", "8888"),
          wemeet.parse_join(invite + "\n会议密码：8888"))
    check("同一个号写两遍不算歧义",
          wemeet.parse_join(invite + "\n会议号：413231713") == ("413231713", None),
          wemeet.parse_join(invite + "\n会议号：413231713"))
    check("两个不同的号一律不猜",
          wemeet.parse_join(invite + "\n#腾讯会议：987654321") == (None, None))
    # 标记必须在：光有链接和日期的一段话不许凭空猜出个号来
    no_mark = invite.rsplit("\n", 1)[0]
    check("没有标记就不猜", wemeet.parse_join(no_mark) == (None, None),
          wemeet.parse_join(no_mark))
    check("短句通道仍然不认链接",
          wemeet.parse_join("进会 https://meeting.tencent.com/dm/abc") == (None, None))
    check("超长的一段（>%d 字）不看" % wemeet.MAX_INVITE_LEN,
          wemeet.parse_join("废话" * 300 + "#腾讯会议：413231713") == (None, None))
    for text in ("#腾讯会议:413231713", "# 腾讯会议 413-231-713", "会议号：413231713",
                 "会议号码 413-231-713"):
        check("标记的几种写法都认：%s" % text,
              wemeet.parse_join(text) == ("413231713", None), wemeet.parse_join(text))


def test_meeting_url():
    """进 URL 的只能是数字和字母：注入面为零。密码参数名可配，改配置就能换。"""
    opened = []
    j = wemeet.MeetingJoiner(opener=opened.append)
    text, handled = j.join("413231713")
    check("拉起的是 wemeet 协议链接",
          opened == ["wemeet://page/inmeeting?meeting_code=413231713"], opened)
    check("回复里带断好的会议号", handled and "413-231-713" in text, text)
    check("回复说清麦克风摄像头控制不了", "摄像头" in text, text)

    j.join("413-231-713", "8888")
    check("密码拼在 password 参数上", opened[-1].endswith("&password=8888"), opened[-1])
    check("带分隔符的号进 URL 前被剥干净", "meeting_code=413231713" in opened[-1], opened[-1])

    j2 = wemeet.MeetingJoiner(url_template="x://join?id={code}", password_param="pwd",
                              opener=opened.append)
    j2.join("413231713", "8888")
    check("模板和密码参数名都可配", opened[-1] == "x://join?id=413231713&pwd=8888", opened[-1])

    check("位数不对的号一律不动手", j.join("12345") == (None, False), j.join("12345"))
    check("带别的字符的号一律不动手", j.join("413231713; calc") == (None, False))
    check("密码不是字母数字就不拼进 URL",
          j.build_url("413231713", "8888&x=1").endswith("meeting_code=413231713"),
          j.build_url("413231713", "8888&x=1"))

    def boom(url):
        raise OSError("no protocol handler")

    text, handled = wemeet.MeetingJoiner(opener=boom).join("413231713")
    check("协议拉不起时说清怎么办", handled and "拉不起" in text and "打开腾讯会议" in text, text)


def test_meeting_chain(tmpdir, fake_claude):
    """桥这一层：带号的自己拉起、不带号的问一句，两种都不该花额度。"""
    b = make_bridge(tmpdir, fake_claude)
    opened = []
    b.meeting = wemeet.MeetingJoiner(opener=opened.append)

    drain(b, [wecom_msg("/cc 进会 413231713", "j1")])
    t = texts()
    check("桥拦下进会请求", any("413-231-713" in x for x in t), t)
    check("进会不经过 Claude", not any("已处理" in x for x in t), t)
    check("桥确实拉起了链接", len(opened) == 1 and "413231713" in opened[0], opened)

    drain(b, [wecom_msg("/cc 进会", "j2")])
    t = texts()
    check("没给号就回一句问号", any("把会议号发我" in x for x in t), t)
    check("问号那次没拉起任何东西", len(opened) == 1, opened)
    check("问号那次也没花额度", not any("已处理" in x for x in t), t)

    drain(b, [wecom_msg("进会 413231713", "j3")])
    check("不带前缀不会误进会", texts() == [] and len(opened) == 1, (texts(), opened))

    drain(b, [wecom_msg("/cc 进会 413231713", "j4", user=OTHER)])
    check("非白名单用户进不了会", texts() == [] and len(opened) == 1, (texts(), opened))

    drain(b, [wecom_msg("/cc 413231713", "j5")])
    t = texts()
    check("光发一串数字交给 Claude", any("已处理" in x for x in t), t)
    check("光发数字没拉起会议", len(opened) == 1, opened)

    invite = ("/cc 我预定了腾讯会议，等你来开会哦~\n会议主题：周会\n"
              "会议时间：2026/08/30 20:00-21:00\n点击链接入会，或添加至会议列表：\n"
              "https://meeting.tencent.com/dm/abcdEFGH12345\n#腾讯会议：413-231-713")
    drain(b, [wecom_msg(invite, "j5b")])
    t = texts()
    check("整段邀请文案原样转来也能进会", any("413-231-713" in x for x in t), t)
    check("邀请文案那次不经过 Claude", not any("已处理" in x for x in t), t)
    check("邀请文案那次真拉起了", len(opened) == 2 and "413231713" in opened[1], opened)

    # 「打开腾讯会议」（没带号）不许被进会通道吞掉：它该走开应用那条
    lc = make_launcher(tmpdir)[0]
    b.launcher = lc
    drain(b, [wecom_msg("/cc 打开腾讯会议", "j6")])
    t = texts()
    check("不带号的「打开腾讯会议」不走进会通道", len(opened) == 2, opened)
    check("它落回开应用/Claude 那条路", any("已处理" in x for x in t), t)
    b.launcher = None

    off = make_bridge(tmpdir, fake_claude)
    check("meeting.enable=false 时不装", off.meeting is None)
    drain(off, [wecom_msg("/cc 进会 413231713", "j7")])
    t = texts()
    check("功能关掉时明说是关着的", any("关着" in x for x in t), t)
    check("功能关掉时不甩给 Claude 白花额度", not any("已处理" in x for x in t), t)

    grabbed = []

    class Grab(logging.Handler):
        def emit(self, record):
            try:
                grabbed.append(record.getMessage())
            except Exception:
                pass

    h = Grab()
    root = logging.getLogger()
    root.addHandler(h)
    prev = root.level
    root.setLevel(logging.INFO)
    try:
        drain(b, [wecom_msg("/cc 进会 413231713 密码 87654321", "j8")])
        check("入会密码绝不进日志", not any("87654321" in x for x in grabbed),
              [x for x in grabbed if "87654321" in x])
        check("默认日志里连会议号也只记位数",
              not any("413231713" in x for x in grabbed),
              [x for x in grabbed if "413231713" in x])
        check("但日志里能看出走了进会通道", any("走进会通道" in x for x in grabbed),
              [x for x in grabbed if "进会" in x])
        check("密码确实拼进了链接", opened[-1].endswith("&password=87654321"), opened[-1])
    finally:
        root.removeHandler(h)
        root.setLevel(prev)


# ---------- 定时进会 ----------

NOW_1838 = time.mktime((2026, 8, 30, 18, 38, 0, 0, 0, -1))


def test_schedule_parse():
    """真机原话在这组第一条：「六点四十进腾讯会议，我把会议号发给你 / #腾讯会议：853-329-165」
    当时被当成「现在」立刻把人拉进了会。这组盯住两件事：带时刻的必须落到定时那条，
    以及「明天」「然后…」这类一律不接（桥猜不了，Claude 能问）。
    """
    now = NOW_1838
    live = ("六点四十进腾讯会议，我把会议号发给你\n#腾讯会议： 853-329-165")
    cases = [
        (live, ("schedule", "853329165", "18:40")),
        ("18:40 进会 413231713", ("schedule", "413231713", "18:40")),
        ("进会 413231713 18:40", ("schedule", "413231713", "18:40")),
        ("晚上八点半进会 413231713", ("schedule", "413231713", "20:30")),
        ("20分钟后进会 413231713", ("schedule", "413231713", "18:58")),
        ("半小时后进会 413231713", ("schedule", "413231713", "19:08")),
        ("一个半小时后进会 413231713", ("schedule", "413231713", "20:08")),
        ("23:59 进会 413231713", ("schedule", "413231713", "23:59")),
        # 说了时间还没给号：记着这个点，等号
        ("六点四十进腾讯会议，我把会议号发给你", ("need_code", None, "18:40")),
        ("七点开会，加入会议", ("need_code", None, "19:00")),
        # 号本身不可信的，也只当「还没给号」，绝不猜
        ("七点进会 41323171388888", ("need_code", None, "19:00")),
        ("八点进会 413231713 和 987654321", ("need_code", None, "20:00")),
        # 那个点今天已经过去了：值得回一句，但不许顺延到明天
        ("六点半进腾讯会议 413231713", ("past", None, "18:30")),
        ("下午三点进会 413231713", ("past", None, "15:00")),
    ]
    for text, want in cases:
        got = wemeet.parse_schedule(text, now)
        kind = got and got["kind"]
        code = got and got.get("code")
        label = got and got.get("label")
        check("定时解析：%s" % text.replace("\n", " / ")[:26],
              (kind, code, label) == want, (kind, code, label))

    got = wemeet.parse_schedule("19:00 进会 413231713 密码 8888", now)
    check("定时也认入会密码", got["pwd"] == "8888", got)

    for text in ["明天六点四十进会 413231713",      # 只认当天
                 "后天八点进会 413231713",
                 "待会六点四十进会 413231713",
                 "别在六点四十进会 413231713",
                 "六点四十进会 413231713 然后把纪要发我",
                 "六点四十那个会记到日程里 413231713",
                 "六点四十提醒我",                  # 没在说会
                 "18:40",                          # 光一个时刻
                 "进会 413231713",                 # 没说时间：立刻进那条
                 "十点前把报告写完 413231713"]:
        check("这条不该走定时：%s" % text, wemeet.parse_schedule(text, now) is None,
              wemeet.parse_schedule(text, now))

    check("带时刻的不会被立刻进会那条抢走",
          wemeet.parse_join("六点四十进会 413231713") == (None, None),
          wemeet.parse_join("六点四十进会 413231713"))
    invite = ("我预定了腾讯会议，等你来开会哦~\n会议主题：周会\n"
              "会议时间：2026/08/30 20:00-21:00\n点击链接入会：\n"
              "https://meeting.tencent.com/dm/abcdEFGH12345\n#腾讯会议：413-231-713")
    check("整段邀请里的「会议时间」不许被当成定时",
          wemeet.parse_schedule(invite, now) is None, wemeet.parse_schedule(invite, now))
    check("整段邀请照旧是立刻进会",
          wemeet.parse_join(invite) == ("413231713", None), wemeet.parse_join(invite))

    check("隔太远的（超过 24 小时）算 far，不定",
          (wemeet.parse_schedule("100小时后进会 413231713", now) or {}).get("kind") == "far",
          wemeet.parse_schedule("100小时后进会 413231713", now))
    for word in ["确认", "确定", "好", "ok", "OK", "行", "进", "进会", "可以"]:
        kind, _ = wemeet.read_confirm(word)
        check("「%s」算同意" % word, kind == "yes", kind)
    for word in ["取消", "不进", "算了", "不用", "别进"]:
        kind, _ = wemeet.read_confirm(word)
        check("「%s」算不进" % word, kind == "no", kind)
    for word in ["确认一下会议室在哪", "看看画面", "2", ""]:
        kind, _ = wemeet.read_confirm(word)
        check("「%s」不算答复" % word, kind is None, kind)
    check("「进会」是明确说法", wemeet.read_confirm("进会")[1] is True)
    check("光一个「确认」不算明确说法", wemeet.read_confirm("确认")[1] is False)
    for word in ["取消定时", "取消定时进会", "不定了", "取消预约"]:
        check("「%s」是取消定时" % word, wemeet.parse_cancel_timer(word) is True, word)
    check("「取消」不当成取消定时（那是点击那边的词）",
          wemeet.parse_cancel_timer("取消") is False)
    check("「别进了」是答复、不是取消定时",
          wemeet.parse_cancel_timer("别进了") is False)
    for word in ["/meetings", "有哪些定时", "定时列表", "看看定时", "待进的会"]:
        check("「%s」是看清单" % word, wemeet.parse_list_timer(word) is True, word)


def test_schedule_store(tmpdir):
    """落盘那一份：桥重启（18:00 定下 18:40 的会，中间关一次开关）不该让它消失。"""
    path = os.path.join(tmpdir, "meetings-store.json")
    s = wemeet.MeetingSchedule(path, max_pending=2)
    at = NOW_1838 + 120
    item, err = s.add(USER, "413231713", at, "18:40")
    check("记下一场", err == "" and item["code"] == "413231713", (item, err))
    check("落盘了", os.path.exists(path))
    check("同一场同一个点不重复记", s.add(USER, "413-231-713", at + 10, "18:40")[1] == "same")
    check("会议号不对的不记", s.add(USER, "123", at, "x")[1] == "会议号不对")
    s.add(USER, "987654321", at + 600, "18:50")
    check("满了就明说", s.add(USER, "555666777", at + 900, "19:00")[1] == "full")
    check("按时间排好", [x["label"] for x in s.all()] == ["18:40", "18:50"],
          [x["label"] for x in s.all()])
    check("按用户分得开", s.all(OTHER) == [], s.all(OTHER))

    again = wemeet.MeetingSchedule(path, max_pending=2)
    check("重启后还在", [x["code"] for x in again.all()] == ["413231713", "987654321"],
          again.all())
    due = again.due(at + 1)
    check("到点的取出来一场", len(due) == 1 and due[0]["code"] == "413231713", due)
    check("取出来时带着迟了多久", 0 <= due[0]["late"] <= 2, due[0]["late"])
    check("取出来就从表里删掉", [x["code"] for x in again.all()] == ["987654321"],
          again.all())
    check("同一场不会被取两次", again.due(at + 1) == [], again.due(at + 1))
    check("删掉那一下也落盘了",
          [x["code"] for x in wemeet.MeetingSchedule(path).all()] == ["987654321"])
    check("取消全部", len(again.cancel(USER)) == 1 and again.all() == [])

    bad = os.path.join(tmpdir, "meetings-bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write('{"pending": [{"code": "abc"}, 3, {"code": "413231713", "at": "x"},'
                 ' {"code": "413231713", "at": %f, "label": "18:40"}]}' % at)
    check("坏文件里只留能用的那条",
          [x["code"] for x in wemeet.MeetingSchedule(bad).all()] == ["413231713"],
          wemeet.MeetingSchedule(bad).all())
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{ 不是 json")
    check("整份读不动就当没有，不炸", wemeet.MeetingSchedule(bad).all() == [])


def timer_bridge(tmpdir, fake_claude, tag):
    """带定时的桥。刻意不走 config 里的 meeting.enable——那条会起一个真的计时线程，
    自测要的是自己掐着时间调 _timer_tick。"""
    b = make_bridge(tmpdir, fake_claude)
    opened = []
    b.meeting = wemeet.MeetingJoiner(opener=opened.append)
    b.schedule = wemeet.MeetingSchedule(os.path.join(tmpdir, "meetings-%s.json" % tag))
    return b, opened


def test_timer_chain(tmpdir, fake_claude):
    """桥这一层的定时进会：定上 → 到点先问 → 回「确认」才拉起。一次额度都不该花。"""
    b, opened = timer_bridge(tmpdir, fake_claude, "chain")

    # 这一组用「N 分钟后」而不是「六点四十」：钟点会随跑自测的时间变成过去式
    drain(b, [wecom_msg("/cc 二十分钟后进会 413231713", "t1")])
    t = texts()
    check("定上了会回一句确认", any("记下了" in x and "413-231-713" in x for x in t), t)
    check("说清到点会先问", any("确认" in x for x in t), t)
    check("说清开关得开着", any("开关" in x for x in t), t)
    check("定时那一刻绝不拉起", opened == [], opened)
    check("定时不花额度", not any("已处理" in x for x in t), t)
    saved = b.schedule.all(USER)
    check("表里真有这一场", len(saved) == 1 and saved[0]["code"] == "413231713", saved)

    drain(b, [wecom_msg("/meetings", "t2")])
    t = texts()
    check("清单里看得到", any("413-231-713" in x for x in t), t)

    at = saved[0]["at"]
    b._timer_tick(at + 1)
    t = texts()
    check("到点主动问一句", any("到点了" in x and "413-231-713" in x for x in t), t)
    check("问的时候还没拉起", opened == [], opened)
    check("问完从表里挪走了", b.schedule.all(USER) == [], b.schedule.all(USER))

    drain(b, [wecom_msg("确认", "t3")])
    t = texts()
    check("回「确认」才真拉起", len(opened) == 1 and "413231713" in opened[0], opened)
    check("拉起后回一句正在进", any("正在拉起腾讯会议" in x for x in t), t)
    check("确认不用带前缀", not any("已处理" in x for x in t), t)

    drain(b, [wecom_msg("确认", "t4")])
    t = texts()
    check("同一条问话不会被确认两次", len(opened) == 1, opened)
    check("多出来的「确认」不带前缀就当没说（不花额度）",
          t == [] and not any("已处理" in x for x in t), t)

    # 回「取消」：不进，也不该留下任何待办
    drain(b, [wecom_msg("/cc 二十分钟后进会 987654321", "t5")])
    at2 = b.schedule.all(USER)[0]["at"]
    b._timer_tick(at2 + 1)
    drain(b, [wecom_msg("取消", "t6")])
    t = texts()
    check("回「取消」就不进", len(opened) == 1, opened)
    check("回「取消」说一句不进了", any("不进了" in x for x in t), t)
    check("取消之后没有待确认的会", b._pending_meeting == {}, b._pending_meeting)


def test_timer_edges(tmpdir, fake_claude):
    """几条边界：等不到确认、桥当时没在跑、说了时间一直没给号、取消、功能关掉。"""
    b, opened = timer_bridge(tmpdir, fake_claude, "edge")

    # 等不到确认：到时限自己作废，并且主动说一句（不能装死）
    drain(b, [wecom_msg("/cc 十分钟后进会 413231713", "e1")])
    at = b.schedule.all(USER)[0]["at"]
    b._timer_tick(at + 1)
    FakeILink.sent = []
    b._timer_tick(at + 1 + b.meeting_confirm_seconds + 1)
    t = texts()
    check("等不到确认就作废", any("没等到确认" in x for x in t), t)
    check("作废时没拉起任何东西", opened == [], opened)
    drain(b, [wecom_msg("确认", "e2")])
    check("作废之后的「确认」不会补进会", opened == [], opened)

    # 到点的时候桥没在跑：晚太多就只说明一句，不再问
    b.schedule.add(USER, "987654321", NOW_1838, "18:38")
    FakeILink.sent = []
    b._timer_tick(NOW_1838 + b.meeting_late_seconds + 60)
    t = texts()
    check("错过太久的只说明、不问", any("开关没开着" in x for x in t), t)
    check("错过的那场绝不拉起", opened == [], opened)
    check("错过的也不留待确认", b._pending_meeting == {}, b._pending_meeting)

    # 刚过去一点点（在 late_minutes 里）还值得问一句
    b.schedule.add(USER, "987654321", NOW_1838, "18:38")
    FakeILink.sent = []
    b._timer_tick(NOW_1838 + 60)
    t = texts()
    check("刚过去一会儿还是问一句", any("到点了" in x for x in t), t)
    check("并且说清晚了多久", any("晚了" in x for x in t), t)
    b._pending_meeting.clear()

    # 说了时间没给号：下一条把号发来就算定上
    drain(b, [wecom_msg("/cc 十分钟后进腾讯会议，我把会议号发给你", "e3")])
    t = texts()
    check("差号时明说在等号", any("把会议号发过来" in x for x in t), t)
    check("等号时表里还没有这一场", b.schedule.all(USER) == [], b.schedule.all(USER))
    drain(b, [wecom_msg("/cc #腾讯会议：413-231-713", "e4")])
    t = texts()
    check("补上号就算定上", any("记下了" in x for x in t), t)
    check("补号那次也不拉起", opened == [], opened)
    check("补上的号进了表",
          [x["code"] for x in b.schedule.all(USER)] == ["413231713"], b.schedule.all(USER))
    drain(b, [wecom_msg("/cc 取消定时", "e5")])
    t = texts()
    check("取消定时清空表", b.schedule.all(USER) == [], b.schedule.all(USER))
    check("取消时说清取消了哪些", any("取消了" in x and "413-231-713" in x for x in t), t)
    drain(b, [wecom_msg("/cc 取消定时", "e6")])
    check("本来就没有时也直说", any("本来就没有" in x for x in texts()), texts())

    # 一直没给号，到点了也得作废（不能悄悄留着）
    drain(b, [wecom_msg("/cc 十分钟后进腾讯会议，我把会议号发给你", "e7")])
    when = b._await_code[USER]["at"]
    FakeILink.sent = []
    b._timer_tick(when + 1)
    check("到点还没给号就作废", any("一直没把会议号发我" in x for x in texts()), texts())
    check("作废后不再等号", USER not in b._await_code, b._await_code)

    # 满了：说清怎么腾位置
    for i in range(b.schedule.max_pending):
        b.schedule.add(USER, "41323171%d" % i, NOW_1838 + 3600 + i * 120, "20:0%d" % i)
    drain(b, [wecom_msg("/cc 两小时后进会 555666777", "e8")])
    check("定满了明说", any("最多同时定" in x for x in texts()), texts())
    b.schedule.cancel()

    # 主动通知发不出去（context_token 过期）：攒着，下条消息里补
    b2, opened2 = timer_bridge(tmpdir, fake_claude, "note")
    b2.schedule.add(USER, "413231713", NOW_1838, "18:38")
    b2._last_ctx.clear()
    b2._timer_tick(NOW_1838 + 1)
    check("发不出去时攒起来", b2._notes.get(USER), b2._notes)
    drain(b2, [wecom_msg("/cc 看下这个目录", "n1")])
    t = texts()
    check("下条消息里把通知补上", any("到点了" in x for x in t), t)
    check("补完就不再重复", USER not in b2._notes, b2._notes)

    # 功能关掉时：明说，不甩给 Claude 白花额度
    off, _ = timer_bridge(tmpdir, fake_claude, "off")
    off.schedule = None
    drain(off, [wecom_msg("/cc 十分钟后进会 413231713", "e9")])
    t = texts()
    check("timer_enable 关掉时明说", any("定时进会是关着的" in x for x in t), t)
    check("关掉时不花额度", not any("已处理" in x for x in t), t)
    check("关掉时不拉起", off.meeting is not None and texts() is not None)


def test_timer_thread(tmpdir, fake_claude):
    """真桥（meeting.enable=true）会起一个计时线程；起不来的话定时就是死的。"""
    cfg = dict(wechat_bridge.DEFAULTS)
    cfg.update({"base_url": BASE, "bot_token": "tok", "account_id": ACCOUNT,
                "allowed_user_ids": [USER], "workdir": tmpdir, "claude_bin": fake_claude,
                "app_launch": {"enable": False}, "media_control": {"enable": False},
                "screen_control": {"enable": False},
                "meeting": {"enable": True, "timer_enable": True}})
    b = wechat_bridge.Bridge(cfg)
    try:
        check("装上了定时表", b.schedule is not None)
        check("计时线程在跑", b._timer is not None and b._timer.is_alive())
        check("确认时限按配置来（默认 5 分钟）", b.meeting_confirm_seconds == 300,
              b.meeting_confirm_seconds)
        check("错过多久就不问了（默认 10 分钟）", b.meeting_late_seconds == 600,
              b.meeting_late_seconds)
    finally:
        b.schedule = None            # 让那个线程空转，别碰项目目录里真的 meetings.json
    # 进会关掉时不装进会表。注意**线程起没起要看两件事**（进会和提醒共用那一条心跳），
    # 所以这里把提醒也关掉才谈得上「不起线程」
    off = wechat_bridge.Bridge(dict(cfg, remind={"enable": False},
                                    meeting={"enable": True, "timer_enable": False}))
    check("timer_enable=false 时不装进会表、也没起线程",
          off.schedule is None and off._timer is None)
    only_remind = wechat_bridge.Bridge(dict(cfg, meeting={"enable": True,
                                                          "timer_enable": False}))
    check("只有提醒开着时，那条心跳照样要起（提醒不能哑掉）",
          only_remind.reminders is not None and only_remind._timer is not None)
    check("meetings.json 在不能发到微信的名单里",
          "meetings.json" in wechat_bridge._SEND_DENY_HERE)
