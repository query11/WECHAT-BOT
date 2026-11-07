# -*- coding: utf-8 -*-
"""关机、锁屏：解析、门面、以及在桥里走哪条通道。

这一组和别的自测有个不一样的地方：**跑错了会真的把这台机器关掉。** 所以规矩是硬的
——本文件里没有一个 PowerControl 是拿默认 runner 造的，每一个都塞了 FakeMachine，
真的 shutdown.exe / LockWorkStation 一次都不会跑。selftest_base.make_bridge 那边也
默认 power_control.enable=False，所以别的用例里哪句话被误判成「关机」都不会出事。

解锁屏幕 2026-09-03 整块砍掉了（理由见 power.py 开头），于是这里多了一批**守卫**：
「解锁」「把屏幕解锁」「unlock」这些说法一句都不许被误判成锁屏或关机——它们只该落回
Claude。删功能最容易出的事故就是「少了一个专门让开的判断，于是含着「锁」字的句子被
当成锁屏」，所以那批断言留在 test_power_parse 和 test_power_chain 里，别删。

钟点式定时关机（同一天 2026-09-03 加的）单独一组 test_power_schedule，它盯的是另一类
事故：**「三点四十分」「十点二十分」里都藏着「十分」「二十分」**，谁把 parse_delay 挪到
wemeet 前面，说 15:40 的人就会在 10 分钟后被强制关机。那一组传死了 `now`（正午），
所以不管几点跑都是同一个答案；桥那条链路上只用相对时长，理由见 test_power_chain。
"""

import logging
import time

import power
import wechat_bridge
import wemeet

from tests.selftest_base import check, drain, make_bridge, make_launcher, texts, wecom_msg


class FakeMachine(object):
    """替掉 power.default_runner：所有 Windows 调用都落这儿，一个都不出门。"""

    def __init__(self, locked=True, aborted=True, states=None):
        self.locked, self.aborted = locked, aborted
        self.calls, self.delays = [], []
        # 睡眠能力由用例给（`states`）；真机器上这个值是 `powercfg /a` 读出来的。
        # 这里给它一个「都能用」的默认，真去问 powercfg 的只有 `test_power_sleep` 里
        # 那条纯解析用例（喂死文本，不跑命令）。
        self.states = {"sleep": True, "hibernate": True} if states is None else states
        self.suspends = []          # 记下「谁、等了多久」——别真睡

    def __call__(self, action, arg=0):
        self.calls.append(action)
        if action == "status":
            return {"ok": True, "locked": self.locked}
        if action == "lock":
            self.locked = True
            return {"ok": True, "locked": True}
        if action == "shutdown":
            self.delays.append(int(arg))
            return {"ok": True, "delay": int(arg), "armed": True}
        if action == "cancel":
            return {"ok": True, "aborted": self.aborted}
        if action == "states":
            return dict({"ok": True}, **self.states)
        if action in ("sleep", "hibernate"):
            # **不真睡**：真睡这一行是 power.sleep_now / hibernate_now，而它俩只有在
            # `_suspend_later` 起的那个守护线程里才会被调到——自测里压根不起那个线程。
            self.suspends.append((action, int(arg)))
            return {"ok": True, "scheduled": True}
        raise ValueError("不认识的动作：%r" % (action,))


def make_pc(opts=None, **machine_kw):
    """造一个只跟 FakeMachine 说话的 PowerControl，回 (它, 假机器)。

    machine_kw 给假机器（locked / aborted），opts 给 PowerControl（allow_shutdown /
    shutdown_delay）。名字故意不叫 make_power——那是 wechat_bridge 里的工厂，
    本文件后面要单独测它。
    """
    mach = FakeMachine(**machine_kw)
    return power.PowerControl(runner=mach, **dict(opts or {})), mach


def test_power_parse():
    """认得出「关机」，更要认不出「关闭电脑上的微信」。后者错一次就是替人关机。"""
    for text in ("锁屏", "锁一下", "把电脑锁上", "帮我锁定屏幕", "/lock", "/锁屏", "lock"):
        check("认得出锁屏：%s" % text, power.parse_lock(text) is True)
    for text in ("电脑锁了吗", "锁了没", "锁没锁", "屏幕状态", "/locked"):
        check("认得出问状态：%s" % text, power.parse_status(text) is True)
    for text in ("取消关机", "先别关机", "不要关机", "撤销关机", "/取消关机"):
        check("认得出取消关机：%s" % text, power.parse_cancel_shutdown(text) is True)

    # 两条容易互相抢的：都含着对方的字，谁先谁后是定死的
    check("「电脑锁了吗」不算锁屏", power.parse_lock("电脑锁了吗") is False)
    check("「取消关机」不算关机", power.parse_shutdown("取消关机") is None)

    # 删掉解锁之后留下的守卫：这些说法一句都不许变成动作，只能落回 Claude。
    # 挡它们的不是什么专门的判断，而是 parse_lock 里「摘掉关键词后剩下的必须是空的」
    # ——「解锁屏幕」剩个「解」、「unlock」剩个「un」。那条规矩要是被谁放松了，
    # 这一批会立刻红，而真机上的后果是「说解锁结果屏幕锁了」。
    for text in ("解锁", "解锁屏幕", "把屏幕解锁", "解锁电脑", "电脑解锁", "解锁一下",
                 "帮我解锁", "唤醒电脑", "登录进去", "unlock", "/unlock", "/解锁"):
        check("「%s」既不算锁屏也不算关机" % text,
              power.parse_lock(text) is False and power.parse_shutdown(text) is None,
              (power.parse_lock(text), power.parse_shutdown(text)))
    check("解锁的解析函数确实没了（别照着老注释加回来）",
          not hasattr(power, "parse_unlock"))
    check("斜杠指令表里也没有解锁",
          not any("unlock" in k or "解锁" in k for k in power._SLASH), power._SLASH)

    check("关机：不说时间就是 None", power.parse_shutdown("关机") == {"delay": None})
    check("「关闭电脑」也是关机", power.parse_shutdown("关闭电脑") == {"delay": None})
    check("/shutdown 也是关机", power.parse_shutdown("/shutdown") == {"delay": None})
    # 只比 delay 一个键：带「后」的那几句现在走 wemeet，会多带一个 label（回话里
    # 「已安排 13:30 关机」的那个钟点），拿整个字典相等去比的话它们会全红。
    for text, sec in (("十分钟后关机", 600), ("30分钟后关机", 1800), ("半小时后关机", 1800),
                      ("两小时后关机", 7200), ("定时关机 20 分钟", 1200)):
        got = power.parse_shutdown(text)
        check("关机带时间：%s → %s 秒" % (text, sec),
              bool(got) and got.get("delay") == sec and "problem" not in got, got)

    # 下面每一条错判都是事故，不是小瑕疵
    for text in ("关闭电脑上的微信", "怎么关机", "关机快捷键是什么", "为什么关机这么慢",
                 "把 D:\\a.py 里的关机代码删掉", "关机脚本发给我",
                 "今天太晚了我准备关机睡觉了明天再说"):
        check("不该当成关机：%s" % text, power.parse_shutdown(text) is None,
              power.parse_shutdown(text))
    for text in ("怎么锁屏", "锁屏快捷键是什么", "blocked", "block this", "解锁密码是多少",
                 "屏保和锁屏有什么区别"):
        check("不该当成锁屏：%s" % text, power.parse_lock(text) is False,
              power.parse_lock(text))

    for text, sec in (("10 分钟", 600), ("半小时", 1800), ("两小时", 7200), ("45秒", 45)):
        check("时长解析：%s → %s" % (text, sec), power.parse_delay(text) == sec,
              power.parse_delay(text))
    check("0 秒不算时长", power.parse_delay("0 秒") is None)
    for sec, want in ((30, "30 秒"), (60, "1 分钟"), (90, "1 分 30 秒"),
                      (3600, "1 小时"), (5400, "1 小时 30 分"), (86400, "24 小时")):
        check("说人话的时长：%d → %s" % (sec, want), power._dur(sec) == want,
              power._dur(sec))


def test_power_status_lock():
    """查状态和锁屏。锁屏是单纯一句 LockWorkStation，回话得说清「只能自己去开」。"""
    pc, mach = make_pc(locked=True)
    text, handled = pc.status()
    check("锁着时说锁着", handled and "锁着的" in text, text)
    check("锁着时不再提「说解锁」（那条路没了，提了就是空头承诺）",
          "解锁" not in text, text)
    pc, mach = make_pc(locked=False)
    text, _ = pc.status()
    check("没锁时说没锁", "没锁" in text, text)

    pc, mach = make_pc(locked=False)
    text, handled = pc.lock()
    check("锁屏成了，且带统一的「✅」状态记号",
          handled and text.startswith("✅ 屏幕已锁。"), text)
    check("锁屏会说清得自己到电脑前开", "自己到电脑前" in text, text)
    check("锁屏只做这一件事，不再顺手切登录方式", mach.calls == ["lock"], mach.calls)
    check("锁上之后状态也跟着变", mach.locked is True)

    def boom(action, arg=0):
        raise OSError("LockWorkStation 失败，GetLastError=5")

    lg = logging.getLogger("bridge.power")
    prev = lg.level
    lg.setLevel(logging.CRITICAL)      # 下面这次失败是预期的，别把堆栈刷出来
    try:
        text, handled = power.PowerControl(runner=boom).lock()
    finally:
        lg.setLevel(prev)
    check("锁屏失败也要回一句，不许把异常抛到消息循环上",
          handled and "锁屏没成" in text, text)


def test_power_shutdown():
    """关机是这套里唯一不可逆的动作：秒数怎么夹、取消怎么答，都得对得上。"""
    pc, mach = make_pc()
    text, handled = pc.shutdown(None)
    check("不说时间就用默认的 60 秒", handled and mach.delays == [60], mach.delays)
    check("回话里写的是「1 分钟」不是「60 秒」", "已安排 1 分钟后关机" in text, text)
    check("回话里教了怎么反悔", "取消关机" in text, text)

    pc, mach = make_pc()
    pc.shutdown(600)
    check("说了时间就按说的来", mach.delays == [600], mach.delays)
    pc.shutdown(1)
    check("再急也留 %d 秒（回话得发得出去）" % power.MIN_DELAY,
          mach.delays[-1] == power.MIN_DELAY, mach.delays)
    pc.shutdown(999999)
    check("再久也不超过我们自己定的 24 小时上限（shutdown.exe 自己能吃到 10 年，别搞混）",
          mach.delays[-1] == power.MAX_DELAY, mach.delays)

    pc, mach = make_pc(opts={"shutdown_delay": 3})
    check("配置里的默认值同样被夹住", pc.shutdown_delay == power.MIN_DELAY,
          pc.shutdown_delay)

    pc, mach = make_pc(opts={"allow_shutdown": False})
    text, handled = pc.shutdown(None)
    check("关掉之后不关机，但要回一句", handled and "关掉了" in text, text)
    check("关掉之后一次都没碰 shutdown.exe", not mach.delays, mach.calls)

    pc, mach = make_pc(aborted=True)
    text, handled = pc.cancel_shutdown()
    check("拦住了就说拦住了", handled and text == "✅ 关机已取消。", text)
    pc, mach = make_pc(aborted=False)
    text, _ = pc.cancel_shutdown()
    check("本来没安排也不当错报", "本来就没安排" in text, text)


def _at(h, m=0):
    """今天 h:m 的时间戳。算法照抄 `wemeet._today_at`（午夜起加秒数），这样期望值
    是算出来的而不是写死的 36000——换个时区、赶上夏令时也不会假红。"""
    t = time.localtime()
    base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return base + h * 3600 + m * 60


def test_power_schedule():
    """钟点式定时关机（2026-09-03 加的）。**这一组盯的是「几点」不许被听成「几分钟」。**

    钟点解析借的是 wemeet 那套，所以这里传死 `now`（正午），不然用例会跟着当前时间飘。
    """
    noon = _at(12)

    for text, h, mi in (("晚上十点关机", 22, 0), ("22点关机", 22, 0),
                        ("晚上十点半关机", 22, 30), ("十点二十分关机", 22, 20),
                        ("三点四十分关机", 15, 40), ("下午三点关机", 15, 0),
                        ("23:30 关机", 23, 30)):
        got = power.parse_shutdown(text, noon)
        want = {"delay": int(round(_at(h, mi) - noon)), "label": "%02d:%02d" % (h, mi)}
        check("钟点关机：%s → %s" % (text, want["label"]), got == want, got)

    # 这两句是这一组存在的理由：句子里都藏着「十分」「二十分」，`_CN_DELAY_RE` 会把它们
    # 当成 10 / 20 分钟。真出了这个错，说 15:40 的人会在 10 分钟后被强制关机——差两个
    # 多钟头，而关机不可逆。所以顺序是 wemeet 先、parse_delay 后，见 power._take_time。
    for text, bad in (("三点四十分关机", 600), ("十点二十分关机", 1200)):
        got = power.parse_shutdown(text, noon)
        check("「%s」不是 %d 秒后关机" % (text, bad), (got or {}).get("delay") != bad, got)

    # 「一个半小时后」以前被截成「半小时后」当 1800 秒，wemeet 的 _REL_HALF_RE 认得对
    got = power.parse_shutdown("一个半小时后关机", noon)
    check("「一个半小时后关机」= 5400 秒（不是 1800）",
          (got or {}).get("delay") == 5400, got)

    # 相对时长不依赖墙上时钟：不传 now 也该是同一个数
    check("不传 now，相对时长照样准",
          (power.parse_shutdown("十分钟后关机") or {}).get("delay") == 600)

    # 「上午十点」「早上八点」这种带了上下午的只剩一个候选，正午一看就是过去了。
    # 光说「9点」不算——它还有 21:00 那个候选，那是今天没到的点，会照常定上。
    for text, label in (("上午十点关机", "10:00"), ("早上八点关机", "08:00"),
                        ("凌晨三点关机", "03:00")):
        got = power.parse_shutdown(text, noon)
        check("今天已经过去的点不定、也不顺延：%s" % text,
              got == {"problem": "past", "label": label}, got)
    check("没说上下午的按晚的那个算（「9点」在正午还没到，照常定）",
          power.parse_shutdown("9点关机", noon)
          == {"delay": int(round(_at(21) - noon)), "label": "21:00"},
          power.parse_shutdown("9点关机", noon))
    check("超过 24 小时不接", power.parse_shutdown("30小时后关机", noon)
          == {"problem": "far", "label": wemeet.stamp_label(noon + 108000, noon)},
          power.parse_shutdown("30小时后关机", noon))
    for text in ("定时关机", "自动关机", "预约关机"):
        check("光说「%s」不给时间：问一句，别按默认 60 秒关" % text,
              power.parse_shutdown(text, noon) == {"problem": "notime"},
              power.parse_shutdown(text, noon))

    # 带日子的一律交给 Claude：口径就是当天，顺延过的时刻到点你早忘了
    for text in ("明天十点关机", "明天早上八点关机", "周一十点关机", "下周关机",
                 "几点关机", "关机定在几点了", "十点前关机"):
        check("不该当成定时关机：%s" % text,
              power.parse_shutdown(text, noon) is None,
              power.parse_shutdown(text, noon))

    _schedule_replies()


def _schedule_replies():
    """plan 的四种形状在门面这一层各回什么话。三种 problem 一次都不许碰 shutdown.exe。"""
    pc, mach = make_pc()
    text, handled = pc.shutdown(36000, None, "22:00")
    check("钟点关机的回话里报出理解成了几点（说「十点」的人只能靠这句纠错）",
          handled and "22:00" in text, text)
    check("钟点关机的回话里说明到点是强制关", "强制关" in text, text)
    check("钟点关机的回话里也教了怎么反悔", "取消关机" in text, text)
    check("钟点关机真安排上了", mach.delays == [36000], mach.delays)

    for problem, label, want in (("past", "08:00", "已经过去"),
                                 ("far", None, "24 小时"),
                                 ("notime", None, "得带上时间")):
        pc, mach = make_pc()
        text, handled = pc.shutdown(None, problem, label)
        check("%s：回一句话，但算处理了" % problem, handled and want in text, text)
        check("%s：一次都没碰 shutdown.exe" % problem, not mach.delays, mach.calls)
    pc, mach = make_pc()
    text, _ = pc.shutdown(None, "past", "08:00")
    check("past 的回话里报出的是哪个点", "08:00" in text, text)
    check("past 的回话里说清了不会顺延到明天", "不会顺延" in text, text)

    pc, mach = make_pc(opts={"allow_shutdown": False})
    text, handled = pc.shutdown(None, "notime", None)
    check("allow_shutdown=False 比 problem 先判（关掉了就只说这一件事）",
          handled and "关掉了" in text, text)


def test_power_factory():
    """config.json 里那几个键真的落到 PowerControl 身上了。"""
    check("关掉就不装", wechat_bridge.make_power({"power_control": {"enable": False}})
          is None)
    pc = wechat_bridge.make_power({})
    check("默认就装上", isinstance(pc, power.PowerControl), pc)
    check("默认留 %d 秒反悔窗口" % power.DEFAULT_DELAY,
          pc.shutdown_delay == power.DEFAULT_DELAY, pc.shutdown_delay)
    check("默认允许关机", pc.allow_shutdown is True)
    # 解锁那几个键（allow_unlock / prefer_pin_on_lock / unlock_wait_seconds）已经
    # 不认了。老 config.json 里还留着它们的人不该因此起不来——多余的键一律忽略。
    pc = wechat_bridge.make_power({"power_control": {
        "allow_unlock": True, "prefer_pin_on_lock": True, "unlock_wait_seconds": 25}})
    check("老配置里剩下的解锁键不影响启动", isinstance(pc, power.PowerControl), pc)
    check("PowerControl 上不再有解锁那几个属性",
          not any(hasattr(pc, a) for a in
                  ("allow_unlock", "prefer_pin_on_lock", "unlock_wait", "state_root")))

    pc = wechat_bridge.make_power({"power_control": {
        "allow_shutdown": False, "shutdown_delay_seconds": 300}})
    check("allow_shutdown 传下去了", pc.allow_shutdown is False)
    check("默认关机秒数传下去了", pc.shutdown_delay == 300, pc.shutdown_delay)
    check("配置里填了荒唐的秒数也夹得住",
          wechat_bridge.make_power({"power_control": {"shutdown_delay_seconds": 1}}
                                   ).shutdown_delay == power.MIN_DELAY)
    lg = logging.getLogger("bridge")
    prev = lg.level
    lg.setLevel(logging.CRITICAL)      # 下面这条配置故意是坏的，别把预期中的堆栈刷出来
    try:
        broken = wechat_bridge.make_power(
            {"power_control": {"shutdown_delay_seconds": "不是数"}})
    finally:
        lg.setLevel(prev)
    check("配置里填了坏值就整块不装（不许半灵半不灵）", broken is None, broken)


def test_power_chain(tmpdir, fake_claude):
    """整条链路：微信一句话 → 电源通道 → 回话，中间不该有 Claude。"""
    b = make_bridge(tmpdir, fake_claude)
    check("自测默认不装电源通道", b.power is None, b.power)
    drain(b, [wecom_msg("/cc 锁屏", "pw0")])
    check("没装的时候「锁屏」照旧交给 Claude", any("已处理" in x for x in texts()),
          texts())

    pc, mach = make_pc(locked=False)
    b.power = pc
    drain(b, [wecom_msg("/cc 锁屏", "pw1")])
    check("锁屏回了话", any("屏幕已锁" in x for x in texts()), texts())
    check("锁屏没花额度", not any("已处理" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 电脑锁了吗", "pw2")])
    check("问状态回的是状态", any("锁着的" in x for x in texts()), texts())
    check("问状态没被当成锁屏", mach.calls[-1] == "status", mach.calls)

    # 解锁功能没了，这句该原样落给 Claude——**尤其不能变成又锁一次屏**
    before = list(mach.calls)
    drain(b, [wecom_msg("/cc 解锁", "pw3")])
    check("「解锁」交给 Claude（这个功能砍了）", any("已处理" in x for x in texts()),
          texts())
    check("「解锁」一次都没碰电源", mach.calls == before, mach.calls)

    drain(b, [wecom_msg("/cc 关机", "pw4")])
    check("关机安排上了", any("1 分钟后关机" in x for x in texts()), texts())
    check("关机用的是默认秒数", mach.delays == [60], mach.delays)

    drain(b, [wecom_msg("/cc 取消关机", "pw5")])
    check("取消关机回了话", any("关机已取消" in x for x in texts()), texts())
    check("「取消关机」没被当成关机（含着同样两个字）",
          mach.calls[-1] == "cancel" and mach.delays == [60], mach.calls)

    # 这两句必须落回 Claude：一句在说别的事，一句是在问办法
    for text, mid in (("关闭电脑上的微信", "pw6"), ("怎么关机", "pw7")):
        before = list(mach.calls)
        drain(b, [wecom_msg("/cc " + text, mid)])
        check("「%s」交给 Claude" % text, any("已处理" in x for x in texts()), texts())
        check("「%s」一次都没碰电源" % text, mach.calls == before, mach.calls)

    # 电源通道排在开关应用前面：不然「关闭电脑」会去桌面上找一个叫「电脑」的图标
    lc, _, opened, killed, _ = make_launcher(tmpdir)
    b.launcher = lc
    drain(b, [wecom_msg("/cc 关闭电脑", "pw8")])
    check("「关闭电脑」是关机，不是开关应用",
          any("后关机" in x for x in texts()) and not killed, (texts(), killed))
    drain(b, [wecom_msg("/cc 关闭微信", "pw9")])
    check("「关闭微信」照旧走开关应用", killed and "WeChat.exe" in killed[-1], killed)

    # 定时关机的三种回话也得能穿过桥这一层（bridge_power 要把 label / problem 一起传
    # 下去，漏了的话 past/far/notime 会变成「按默认 60 秒关机」——那是真关）。
    # 这里只用相对时长：钟点的结果跟着墙上时钟走，跑在半夜就成了 past，会假红。
    before = list(mach.delays)
    drain(b, [wecom_msg("/cc 一个半小时后关机", "pw10")])
    check("定时关机穿过桥了", mach.delays == before + [5400], mach.delays)
    check("回话里报了还剩多久", any("还有 1 小时 30 分" in x for x in texts()), texts())
    check("回话里报了到点是几点（label 传到桥这一层了）",
          any("已安排 " in x and " 关机（还有" in x for x in texts()), texts())

    for text, mid, want in (("30小时后关机", "pw11", "太远"),
                            ("定时关机", "pw12", "得带上时间")):
        before = list(mach.delays)
        drain(b, [wecom_msg("/cc " + text, mid)])
        check("「%s」回一句就完（没花额度）" % text,
              any(want in x for x in texts()) and not any("已处理" in x for x in texts()),
              texts())
        check("「%s」一次都没安排关机" % text, mach.delays == before, mach.delays)



# ---------- 睡眠 / 休眠 ----------

def test_power_sleep():
    """睡眠/休眠：认什么、不认什么，以及「先问能力再动手、动手要延后」。"""
    # ---- 该认的。睡眠和休眠必须分得开（一个秒醒、一个掉电也还在）----
    for text, want in (("睡眠", "sleep"), ("睡一觉", "sleep"), ("睡个觉", "sleep"),
                       ("睡眠模式", "sleep"), ("待机", "sleep"), ("sleep", "sleep"),
                       ("suspend", "sleep"),
                       ("让电脑睡眠", "sleep"), ("叫电脑睡眠", "sleep"),
                       ("把电脑睡眠", "sleep"), ("电脑睡一觉", "sleep"),
                       ("/sleep", "sleep"), ("/睡眠", "sleep"), ("/待机", "sleep"),
                       ("休眠", "hibernate"), ("休眠模式", "hibernate"),
                       ("电脑休眠", "hibernate"), ("hibernate", "hibernate"),
                       ("/hibernate", "hibernate"), ("/休眠", "hibernate")):
        check("认得出「%s」→ %s" % (text, want), power.parse_sleep(text) == want,
              power.parse_sleep(text))
    check("「休眠」不会被「睡眠」抢走",
          power.parse_sleep("休眠") == "hibernate"
          and power.parse_sleep("睡眠") == "sleep")

    # ---- 不该认的。**「我要睡了」那句说的是人自己**；误认的后果是
    #      「说一句话电脑就睡了，而我在手机那头叫不醒它」。----
    for text in ("我要睡了", "早点睡", "睡吧", "电脑睡了吗", "睡了没",
                 "休眠和睡眠有什么区别", "睡眠模式怎么设置", "睡眠对健康的影响",
                 "怎么让电脑睡眠", "帮我看看睡眠设置在哪", "别睡眠", "不用睡眠",
                 "解锁", "解码", "关机", "锁屏", "电脑锁了吗", "睡眠" * 20):
        check("「%s」不当电源指令" % text[:12], power.parse_sleep(text) is None,
              power.parse_sleep(text))

    # ---- sleep_states：真机（2026-09-27）的 `powercfg /a` 原文，照抄 ----
    real = ("此系统上有以下睡眠状态:\n"
            "    待机 (S0 低电量待机) 网络已断开连接\n"
            "    休眠\n"
            "\n"
            "此系统上没有以下睡眠状态:\n"
            "    待机 (S1)\n\t系统固件不支持此待机状态。\n"
            "    待机 (S3)\n\t系统固件不支持此待机状态。\n"
            "    混合睡眠\n\t待机(S3)不可用。\n"
            "    快速启动\n\t当前系统策略禁用此动作。\n"
            "    待机(S0 低电量待机) 连接的网络\n\t策略禁用了备用连接。\n")
    check("真机输出：待机和休眠都读成可用",
          power.sleep_states(real) == {"sleep": True, "hibernate": True},
          power.sleep_states(real))
    # 关键点：**「不可用」那一段里也有「待机」**（上面那个「连接的网络」就是），
    # 全文搜关键字会把被策略禁掉的状态读成可用。这条断言就是钉住「按两段切」。
    check("不可用那一段里的「待机」不会把结果带偏",
          power.sleep_states("此系统上没有以下睡眠状态:\n    待机 (S3)\n    休眠\n")
          == {"sleep": False, "hibernate": False})
    check("英文系统的输出也认",
          power.sleep_states(
              "The following sleep states are available on this system:\n"
              "    Standby (S3)\n\n"
              "The following sleep states are not available on this system:\n"
              "    Hibernate\n") == {"sleep": True, "hibernate": False})
    check("空输出当成都不能用（宁说说不了，也别假装能睡）",
          power.sleep_states("") == {"sleep": False, "hibernate": False})

    # ---- 门面：先问能力，再决定说不说那句「好」----
    pc, mach = make_pc(locked=False)
    text, handled = pc.sleep("sleep")
    check("睡眠回了话", handled and "睡眠" in text, text)
    check("回话里说要怎么唤醒（手机上叫不醒它）", "唤醒" in text, text)
    check("动手之前先问了 powercfg /a", mach.calls[0] == "states", mach.calls)
    check("真正睡是延后 %d 秒的（回话得先发出去）" % power.SLEEP_GRACE,
          mach.suspends == [("sleep", power.SLEEP_GRACE)], mach.suspends)
    text, _ = pc.sleep("hibernate")
    check("休眠也是延后的",
          mach.suspends[-1] == ("hibernate", power.SLEEP_GRACE), mach.suspends)
    check("休眠的回话也说唤醒", "唤醒" in text, text)

    pc2, _ = make_pc(locked=False, states={"sleep": False, "hibernate": False})
    text, handled = pc2.sleep("sleep")
    check("没有待机时直说，不假装睡了", handled and "没有可用的睡眠状态" in text, text)
    check("一次都没真的去睡", pc2.runner.suspends == [], pc2.runner.suspends)
    text, handled = pc2.sleep("hibernate")
    check("休眠不可用时直说，并告诉怎么开",
          handled and "powercfg /hibernate on" in text, text)

    pc3, _ = make_pc(opts={"allow_sleep": False})
    text, handled = pc3.sleep("sleep")
    check("allow_sleep=false 时直说关着", handled and "allow_sleep" in text, text)
    check("关着时一个电源动作都没碰", pc3.runner.calls == [], pc3.runner.calls)

    def boom(action, arg=0):
        raise RuntimeError("powercfg 跑不起来")
    text, handled = power.PowerControl(runner=boom).sleep("sleep")
    check("问不出能力时照实说", handled and "看不出" in text, text)

    def flaky(action, arg=0):
        if action == "states":
            return {"ok": True, "sleep": True, "hibernate": True}
        return {"ok": False, "error": "SetSuspendState 失败"}
    text, handled = power.PowerControl(runner=flaky).sleep("sleep")
    check("排不上去时照实说", handled and "没安排上" in text, text)

    # **这条最要紧**：默认 runner 里那个 `_suspend_later` 会起一个守护线程，
    # 8 秒后真去 `SetSuspendState`。自测里一个 PowerControl 都不拿默认 runner 造
    # （`make_pc` 全塞 FakeMachine），所以这一觉在自测里永远睡不下去。
    check("自测里没人拿默认 runner 造电源对象",
          power.PowerControl().runner is power.default_runner)


def test_sleep_chain(tmpdir, fake_claude):
    """桥这一层：「睡眠」「休眠」走电源通道，而且不跟关机/锁屏/开应用抢。"""
    b = make_bridge(tmpdir, fake_claude)
    check("自测默认不装电源通道", b.power is None, b.power)
    drain(b, [wecom_msg("/cc 睡眠", "sl0")])
    check("没装的时候「睡眠」照旧交给 Claude",
          any("已处理" in x for x in texts()), texts())

    pc, mach = make_pc(locked=False)
    b.power = pc
    lc, desktop, opened, killed, _ = make_launcher(tmpdir)
    b.launcher = lc

    drain(b, [wecom_msg("/cc 睡眠", "sl1")])
    t = texts()
    check("「睡眠」走了电源通道", mach.suspends == [("sleep", power.SLEEP_GRACE)],
          mach.suspends)
    check("「睡眠」没花额度", not any("已处理" in x for x in t), t)
    check("回话里告诉你怎么唤醒", any("唤醒" in x for x in t), t)
    check("睡眠没去动桌面上的应用", not opened and not killed, (opened, killed))

    drain(b, [wecom_msg("/cc 休眠", "sl2")])
    check("「休眠」走的是 hibernate",
          mach.suspends[-1] == ("hibernate", power.SLEEP_GRACE), mach.suspends)

    before = list(mach.suspends)
    for text, mid in (("我要睡了", "sl3"), ("休眠和睡眠有什么区别", "sl4"),
                      ("电脑睡了吗", "sl5"), ("怎么让电脑睡眠", "sl6")):
        drain(b, [wecom_msg("/cc " + text, mid)])
        check("「%s」交给 Claude" % text, any("已处理" in x for x in texts()), texts())
    check("这四句一次都没真睡（宁可漏认）", mach.suspends == before, mach.suspends)

    # 三个动作各归各的，一个都没被邻居抢走
    drain(b, [wecom_msg("/cc 关机", "sl7")])
    check("「关机」还是关机（没被睡眠抢走）", mach.delays == [60], mach.delays)
    drain(b, [wecom_msg("/cc 锁屏", "sl8")])
    check("「锁屏」还是锁屏", mach.calls[-1] == "lock", mach.calls)

    b2 = make_bridge(tmpdir, fake_claude)
    b2.power = power.PowerControl(runner=mach, allow_sleep=False)
    drain(b2, [wecom_msg("/cc 睡眠", "sl9")])
    check("allow_sleep=false 时直说被关掉了",
          any("allow_sleep" in x for x in texts()), texts())
