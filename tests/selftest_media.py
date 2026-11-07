# -*- coding: utf-8 -*-
"""播放控制、相对音量（发系统按键）、绝对音量（走系统混音器），以及屏幕亮度（走 WMI）。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。
"""

import app_actions
import brightness
import volume

from tests.selftest_base import (OTHER, check, drain, make_bridge, make_launcher,
                           texts, wecom_msg)


# ---------- 播放控制 ----------

def make_media(delay=6, vol_step=10, mixer=None):
    """假的按键器：只记下发了哪些键码、等了多久，不碰本机键盘。"""
    keys, waited = [], []
    mc = app_actions.MediaControl(delay=delay, sender=lambda vks: keys.append(list(vks)),
                                 sleeper=waited.append, spawn=lambda job: job(),
                                 vol_step=vol_step, mixer=mixer)
    return mc, keys, waited


def test_media_parse():
    P, N, V = "play", "next", "prev"
    cases = [
        ("打开波点音乐并开始播放", ("打开波点音乐", [P])),
        ("打开波点音乐并播放", ("打开波点音乐", [P])),
        ("打开波点音乐，然后下一首", ("打开波点音乐", [N])),
        ("打开波点音乐播放", ("打开波点音乐", [P])),
        ("下一首", ("", [N])),
        ("上一曲", ("", [V])),
        ("帮我暂停一下", ("帮我", ["pause"])),
        ("暂停播放", ("", ["pause"])),          # 别被「播放」抢走
        ("停止播放", ("", ["stop"])),
        ("放歌", ("", [P])),
    ]
    for text, want in cases:
        got = app_actions.split_tail(text)
        check("动作解析：%s" % text, got == want, got)

    for text in ("打开波点音乐", "看下这个目录", "播放列表里有几首歌", "继续", "开始",
                 "整理一下 D 盘的文件并把结果写成一份说明文档发到我微信上然后开始播放"):
        head, actions = app_actions.split_tail(text)
        check("不当动作看：%s" % text, (head, actions) == (text, []), (head, actions))

    check("整条消息就是个动作", app_actions.is_lead_only("帮我") and
          app_actions.is_lead_only(""), True)
    check("前半句有应用名就不算纯动作",
          not app_actions.is_lead_only("打开波点音乐") and
          not app_actions.is_lead_only("帮我看看怎么"), False)


def test_media_keys():
    mc, keys, _ = make_media()
    text, handled = mc.run(["play"])
    check("播放发的是 PLAY_PAUSE", keys == [[app_actions.VK_MEDIA_PLAY_PAUSE]], keys)
    check("回话里说清是开关键", handled and "播放" in text and "开关" in text, text)

    mc.run(["next"])
    check("下一首发的是 NEXT", keys[-1] == [app_actions.VK_MEDIA_NEXT], keys)
    mc.run(["prev"])
    check("上一首发的是 PREV", keys[-1] == [app_actions.VK_MEDIA_PREV], keys)
    mc.run(["stop"])
    check("停止发的是 STOP", keys[-1] == [app_actions.VK_MEDIA_STOP], keys)

    check("不认识的动作不按键", mc.run(["登月"]) == (None, False), mc.run(["登月"]))
    check("不认识的动作确实没发键", len(keys) == 4, keys)

    def boom(_):
        raise OSError("键盘失联")
    bad = app_actions.MediaControl(sender=boom)
    text, handled = bad.run(["play"])
    check("按键失败如实说", handled and "失败" in text and "键盘失联" in text, text)

    mc2, keys2, waited = make_media(delay=6)
    mc2.run_later(["play"])
    check("延迟按键先等再按", waited == [6.0] and keys2 == [[app_actions.VK_MEDIA_PLAY_PAUSE]],
          (waited, keys2))


def test_volume_parse():
    """音量的解析。宁可漏认不误认：认错了会真的把别人正在听的东西调没声。"""
    U, D, M, N = "vol_up", "vol_down", "mute", "unmute"
    cases = [
        ("大声点", ("", [U])),
        ("大声一点", ("", [U])),
        ("声音大点", ("", [U])),
        ("音量调大", ("", [U])),
        ("调大音量", ("", [U])),
        ("大点声", ("", [U])),
        ("大声", ("", [U])),
        ("小声点", ("", [D])),
        ("音量小一点", ("", [D])),
        ("降低音量", ("", [D])),
        ("静音", ("", [M])),
        ("帮我静音一下", ("帮我", [M])),
        ("别出声", ("", [M])),
        ("取消静音", ("", [N])),          # 别被「静音」抢走
        ("解除静音", ("", [N])),
        ("恢复声音", ("", [N])),
        ("打开波点音乐并大声点", ("打开波点音乐", [U])),
        ("打开波点音乐，然后静音", ("打开波点音乐", [M])),
        ("volume up", ("", [U])),
    ]
    for text, want in cases:
        got = app_actions.split_tail(text)
        check("音量解析：%s" % text, got == want, got)

    # 「大点」「小点」单独出现太可能在说字号/图片；「音量」「声音」是名词不是指令
    for text in ("大点", "小点", "音量", "声音", "把图片调大点", "字体调小点",
                 "音量这块的代码写得不好", "帮我看看怎么调音量",
                 "把这份报告的字号调大一点然后发到我微信上，另外音量"):
        head, actions = app_actions.split_tail(text)
        check("不当音量动作看：%s" % text, (head, actions) == (text, []), (head, actions))

    # 尾巴匹配上了但前半句不是应用名 → 后面那层会把它交回 Claude，不会按键
    head, actions = app_actions.split_tail("这个视频的声音大点")
    check("前半句不是应用名时不算纯动作",
          actions == [U] and not app_actions.is_lead_only(head), (head, actions))

    check("10% 换成 5 下键", app_actions.presses_for(10) == 5)
    check("一格就是 2%", app_actions.presses_for(2) == 1)
    check("小于一格也至少按一下", app_actions.presses_for(1) == 1)
    check("100% 是 50 下", app_actions.presses_for(100) == 50)
    check("步长给了垃圾值就用默认的 10%", app_actions.presses_for("abc") == 5)


def test_volume_keys():
    mc, keys, _ = make_media()
    text, handled = mc.run(["vol_up"])
    check("调大音量发的是 VOLUME_UP", keys == [[app_actions.VK_VOLUME_UP] * 5], keys)
    check("按了 5 下（10% ÷ 每下 2%）", len(keys[-1]) == 5, keys[-1])
    check("回话里报了幅度", handled and "5 下" in text and "10%" in text, text)
    check("回话里说清读不到当前音量", "读不到" in text, text)

    mc.run(["vol_down"])
    check("调小音量发的是 VOLUME_DOWN", keys[-1] == [app_actions.VK_VOLUME_DOWN] * 5, keys[-1])

    text, _ = mc.run(["mute"])
    check("静音发的是 VOLUME_MUTE 一下", keys[-1] == [app_actions.VK_VOLUME_MUTE], keys[-1])
    check("静音必须说清是开关键，不能装成两个动作", "开关" in text, text)
    text, _ = mc.run(["unmute"])
    check("取消静音发的是同一个键", keys[-1] == [app_actions.VK_VOLUME_MUTE], keys[-1])
    check("取消静音也说清是开关键", "开关" in text, text)

    big, keys2, _ = make_media(vol_step=20)
    text, _ = big.run(["vol_up"])
    check("步长 20% 就按 10 下", keys2[-1] == [app_actions.VK_VOLUME_UP] * 10, len(keys2[-1]))
    check("回话报 20%", "10 下" in text and "20%" in text, text)

    odd, keys3, _ = make_media(vol_step=11)
    text, _ = odd.run(["vol_up"])
    check("11% 凑不出来，按 6 下", len(keys3[-1]) == 6, len(keys3[-1]))
    check("回话报按键换算后的真实幅度 12%，不报 11%",
          "12%" in text and "11%" not in text, text)

    tiny, keys4, _ = make_media(vol_step=0)
    check("步长填 0 当没填，用默认 10%（步长 0 没有意义）",
          len(tiny._expand(["vol_up"])) == 5, len(tiny._expand(["vol_up"])))
    neg, _, _ = make_media(vol_step=-20)
    check("步长填负数按幅度看，方向由动作定不由符号定",
          len(neg._expand(["vol_up"])) == 10, len(neg._expand(["vol_up"])))
    huge, _, _ = make_media(vol_step=999)
    check("步长封顶 100%", len(huge._expand(["vol_down"])) == 50,
          len(huge._expand(["vol_down"])))


def test_volume_chain(tmpdir, fake_claude):
    """桥这一层：音量走的是和播放键同一条快速通道（要带前缀、不花额度）。"""
    b = make_bridge(tmpdir, fake_claude)
    lc, desktop, opened, killed, _ = make_launcher(tmpdir)
    b.launcher = lc
    b.media, keys, waited = make_media(delay=6)

    drain(b, [wecom_msg("/cc 大声点", "v1")])
    t = texts()
    check("光说「大声点」就调音量", keys == [[app_actions.VK_VOLUME_UP] * 5], keys)
    check("调音量不经过 Claude", not any("已处理" in x for x in t), t)
    check("调音量不会去开程序", not opened, opened)

    drain(b, [wecom_msg("/cc 静音", "v2")])
    check("静音也走快速通道", keys[-1] == [app_actions.VK_VOLUME_MUTE], keys[-1])

    drain(b, [wecom_msg("/cc 打开波点音乐并大声点", "v3")])
    t = texts()
    check("开完应用顺手调音量", keys[-1] == [app_actions.VK_VOLUME_UP] * 5, keys[-1])
    check("等了几秒才按", waited == [6.0], waited)
    check("回复里说了开了什么、按了什么",
          any("已请求打开 波点音乐" in x and "音量" in x for x in t), t)

    n = len(keys)
    drain(b, [wecom_msg("大声点", "v4")])
    check("不带前缀不会误调音量", texts() == [] and len(keys) == n, (texts(), len(keys)))

    drain(b, [wecom_msg("/cc 帮我看看怎么调音量", "v5")])
    check("问「怎么调音量」是问题不是指令，交给 Claude",
          any("已处理" in x for x in texts()) and len(keys) == n, len(keys))

    drain(b, [wecom_msg("/cc 把图片调大点", "v6")])
    check("「调大点」不是音量，交给 Claude",
          any("已处理" in x for x in texts()) and len(keys) == n, len(keys))

    off = make_bridge(tmpdir, fake_claude)
    off.launcher = lc
    drain(off, [wecom_msg("/cc 大声点", "v7")])
    check("media_control.enable=false 时音量也交给 Claude",
          any("已处理" in x for x in texts()), texts())


# ---------- 绝对音量（volume.py 的混音器） ----------


class FakeMixer(object):
    """假混音器：不起 PowerShell，只按脚本演。`boom=True` 演「这台机器上跑不起来」。

    真机上那条路要 1~3 秒编 C#，自测里绝不能真跑——所以 VolumeMixer 收 runner，
    这里连 VolumeMixer 都不用，直接照着 app_actions 认的四个方法名做一个。
    """

    def __init__(self, vol=40, muted=False, boom=False):
        self.vol, self.muted, self.boom, self.calls = vol, muted, boom, []

    def _guard(self, what, arg=None):
        self.calls.append((what, arg))
        if self.boom:
            raise RuntimeError("COM 不给（假的）")

    def nudge_text(self, delta):
        self._guard("nudge", delta)
        before, self.vol = self.vol, max(0, min(100, self.vol + delta))
        return volume.say_nudge({"before": before, "volume": self.vol,
                                 "beforemuted": self.muted, "muted": self.muted})

    def mute_text(self, on):
        self._guard("mute", on)
        before, self.muted = self.muted, bool(on)
        return volume.say_mute({"before": self.vol, "volume": self.vol,
                                "beforemuted": before, "muted": self.muted}, bool(on))

    def set_text(self, percent):
        self._guard("set", percent)
        before, self.vol = self.vol, max(0, min(100, int(percent)))
        return volume.say_set({"before": before, "volume": self.vol,
                               "beforemuted": self.muted, "muted": self.muted}, percent)

    def query_text(self):
        self._guard("get")
        return volume.say_query({"before": self.vol, "volume": self.vol,
                                 "beforemuted": self.muted, "muted": self.muted})


def test_abs_volume_parse():
    """「调到多少」「现在多少」的解析。**认不出要交给 Claude，绝不瞎猜。**"""
    yes = [("音量调到30%", 30), ("把音量设成 55", 55), ("音量 30", 30), ("音量30", 30),
           ("声音调到一半", 50), ("音量一半", 50), ("把音量调到百分之五十", 50),
           ("音量最大", 100), ("音量拉满", 100), ("声音开到最大", 100),
           ("音量最小", 0), ("把音量调到 0", 0), ("音量100%", 100),
           ("音量：30", 30), ("音量=45", 45), ("帮我把音量设成 20 谢谢", 20)]
    for text, want in yes:
        got = volume.parse_set(text)
        check("音量设成：%s → %s" % (text, want), got == want, got)

    no = ["音量调到 300",        # 不像百分比
          "音量从30调到50",      # 两个数字，宁可漏认
          "30分钟后把音量调好",   # 那个 30 在说时间
          "音量调到30 分钟",
          "调到30%",            # 没提音量，可能在说亮度、缩放
          "亮度调到30%",
          "把图片调大点",
          "30",
          "音量大点",            # 相对调整，归 app_actions 的词表
          "音量多少",
          "http://x 音量30",     # 带链接的一律不当指令
          "帮我把 D:\\a.md 里的音量 30 改一下"]
    for text in no:
        got = volume.parse_set(text)
        check("不当设音量看：%s" % text, got is None, got)

    for text in ("音量多少", "现在音量多大", "音量", "声音", "vol", "音量现在是多少",
                 "帮我看看音量是多少", "音量状态"):
        check("音量问句：%s" % text, volume.parse_query(text) is True,
              volume.parse_query(text))
    for text in ("音量调到30", "多少", "音量最大", "现在几点了", "帮我看看怎么调音量"):
        check("不当问句看：%s" % text, volume.parse_query(text) is False,
              volume.parse_query(text))


def test_abs_volume_script():
    """脚本正文的硬约束：**纯 ASCII、不带单引号、动作名白名单**。"""
    sc = volume.build_script("set", 30)
    check("脚本里一个非 ASCII 都没有", all(ord(c) < 128 for c in sc),
          [c for c in sc if ord(c) >= 128][:5])
    check("C# 里没有单引号（会提前收掉 here-string）", "'" not in volume._CSHARP)
    check("动作和数字都进去了", "$act='set'" in sc and "$arg=30" in sc)
    for bad in ("rm", "get; whoami", "", "GET"):
        try:
            volume.build_script(bad, 0)
            check("动作白名单挡住 %r" % bad, False, "没挡住")
        except ValueError:
            check("动作白名单挡住 %r" % bad, True)
    check("非整数参数会被拦", isinstance(volume.build_script("set", 30.7), str))
    check("小数被截成整数", "$arg=30" in volume.build_script("set", 30.7))
    body = volume._CSHARP.split("interface IAudioEndpointVolume {", 1)[1].split("}", 1)[0]
    names = [ln for ln in body.splitlines() if ln.strip().startswith("int ")]
    check("IAudioEndpointVolume 的 18 个方法一个都没少", len(names) == 18, len(names))
    order = volume._CSHARP.find("SetMasterVolumeLevelScalar")
    check("SetMasterVolumeLevelScalar 在 GetMasterVolumeLevelScalar 前面（vtable 顺序）",
          0 < order < volume._CSHARP.find("GetMasterVolumeLevelScalar"))


def test_abs_volume_mixer():
    """VolumeMixer 本身：runner 换成假的，一行 PowerShell 都不跑。"""
    seen = []

    def runner(action, arg=0):
        seen.append((action, arg))
        return {"ok": True, "before": 40, "beforemuted": False,
                "volume": 30 if action == "set" else 40, "muted": action == "mute"}

    m = volume.VolumeMixer(runner=runner)
    check("读当前值", m.get()["volume"] == 40, m.get())
    check("设到 30", m.set(30)["volume"] == 30 and seen[-1] == ("set", 30), seen[-1])
    check("设值封在 0~100", m.set(555) and seen[-1] == ("set", 100), seen[-1])
    check("设负值也封住", m.set(-5) and seen[-1] == ("set", 0), seen[-1])
    check("相对调整带符号", m.nudge(-10) and seen[-1] == ("nudge", -10), seen[-1])
    check("静音传 1、取消传 0",
          (m.mute(True) and seen[-1] == ("mute", 1)) and
          (m.mute(False) and seen[-1] == ("mute", 0)), seen[-1])

    def bad_ok(action, arg=0):
        return {"ok": False, "error": "Add-Type 编不过"}

    for name, r in (("ok=false 要抛异常", bad_ok),
                    ("音量值不像话要抛异常",
                     lambda a, g=0: {"ok": True, "volume": 300}),
                    ("返回的不是字典要抛异常", lambda a, g=0: "??")):
        try:
            volume.VolumeMixer(runner=r).get()
            check(name, False, "没抛")
        except Exception:
            check(name, True)

    # 回话：这一层唯一比按键强的地方就是能说出数字、能说出「本来就没静音」
    r = {"before": 40, "volume": 50, "beforemuted": False, "muted": False}
    check("相对调整回话带前后两个值", volume.say_nudge(r) == "✅ 系统音量：40% → 50%。",
          volume.say_nudge(r))
    edge = {"before": 100, "volume": 100, "beforemuted": False, "muted": False}
    check("到顶了就直说", "最大" in volume.say_nudge(edge), volume.say_nudge(edge))
    zero = {"before": 0, "volume": 0, "beforemuted": False, "muted": False}
    check("到底了也直说", "最小" in volume.say_nudge(zero), volume.say_nudge(zero))
    mutedr = {"before": 40, "volume": 50, "beforemuted": True, "muted": True}
    check("静音时调大要提醒听不见", "静音" in volume.say_nudge(mutedr),
          volume.say_nudge(mutedr))
    check("系统没落在要的值上要说出来",
          "实际落在" in volume.say_set({"before": 40, "volume": 47, "muted": False}, 50),
          volume.say_set({"before": 40, "volume": 47, "muted": False}, 50))
    check("落在要的值上就不啰嗦",
          "实际落在" not in volume.say_set({"before": 40, "volume": 50, "muted": False}, 50))
    on = {"before": 40, "volume": 40, "beforemuted": False, "muted": True}
    check("静音回话带音量值", "40%" in volume.say_mute(on, True), volume.say_mute(on, True))
    already = {"before": 40, "volume": 40, "beforemuted": True, "muted": True}
    check("本来就静音要说清没变", "本来就是静音" in volume.say_mute(already, True),
          volume.say_mute(already, True))
    off = {"before": 40, "volume": 40, "beforemuted": True, "muted": False}
    check("取消静音说得出方向", "已取消静音" in volume.say_mute(off, False),
          volume.say_mute(off, False))
    noop = {"before": 40, "volume": 40, "beforemuted": False, "muted": False}
    check("本来就没静音——按键那条路说不出这句",
          "本来就没静音" in volume.say_mute(noop, False), volume.say_mute(noop, False))
    check("问句回话就是一个数", volume.say_query(noop) == "✅ 系统音量现在 40%。",
          volume.say_query(noop))


def test_abs_volume_fallback():
    """**混音器挂了必须退回按键**——这一层是加分项，坏了不能让功能跟着坏。"""
    good = FakeMixer(vol=40)
    mc, keys, _ = make_media(mixer=good)
    text, hit = mc.run(["vol_up"])
    check("有混音器时不按键", keys == [] and hit, (keys, text))
    check("有混音器时回话带具体数字", "40% → 50%" in text, text)
    text, _ = mc.run(["unmute"])
    check("有混音器时分得清「本来就没静音」", "本来就没静音" in text, text)
    check("有混音器时不再提「这是个开关键」", "开关" not in text, text)

    boom = FakeMixer(boom=True)
    mc2, keys2, _ = make_media(mixer=boom)
    text, hit = mc2.run(["vol_up"])
    check("混音器挂了退回按键", keys2 == [[app_actions.VK_VOLUME_UP] * 5] and hit, keys2)
    check("退回按键后回话里不编数字", "→" not in text and "已按 5 下" in text, text)
    text, _ = mc2.run(["mute"])
    check("静音挂了也退回按键", keys2[-1] == [app_actions.VK_VOLUME_MUTE], keys2[-1])
    check("退回按键就老实说这是个开关", "开关" in text, text)

    # 绝对音量没有按键可退：**宁可说做不到，也不硬按**
    text, hit = mc2.set_volume(30)
    check("设绝对音量失败要明说、不硬按", hit and "读不到系统混音器" in text
          and len(keys2) == 2, (text, len(keys2)))
    text, hit = mc2.query_volume()
    check("读音量失败也明说", hit and "读不到当前音量" in text, text)
    plain, keys3, _ = make_media()
    check("压根没混音器时绝对音量交给 Claude",
          plain.set_volume(30) == (None, False) and plain.query_volume() == (None, False))
    check("没混音器时相对调整照旧按键", plain.run(["vol_up"])[1] and keys3, keys3)


def test_abs_volume_chain(tmpdir, fake_claude):
    """桥这一层：「音量调到 30%」「音量多少」走快速通道，不花额度。"""
    fm = FakeMixer(vol=40)
    b = make_bridge(tmpdir, fake_claude)
    lc, desktop, opened, killed, _ = make_launcher(tmpdir)
    b.launcher = lc
    b.media, keys, _ = make_media(delay=6, mixer=fm)

    drain(b, [wecom_msg("/cc 音量调到30%", "a1")])
    t = texts()
    check("「音量调到30%」走混音器", fm.calls[-1] == ("set", 30), fm.calls)
    check("设绝对音量不按键、不花额度",
          keys == [] and not any("已处理" in x for x in t), (keys, t))
    check("回话里有前后两个值", any("40% → 30%" in x for x in t), t)
    check("设音量不会去开程序", not opened, opened)

    drain(b, [wecom_msg("/cc 音量多少", "a2")])
    check("「音量多少」走混音器", fm.calls[-1] == ("get", None), fm.calls[-1])
    check("问音量回的是一个数", any("30%" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 大声点", "a3")])
    check("相对调整也用上了混音器（有的话）", fm.calls[-1] == ("nudge", 10), fm.calls[-1])
    check("有混音器时相对调整也不按键", keys == [], keys)

    n = len(fm.calls)
    drain(b, [wecom_msg("音量调到30%", "a4")])
    check("不带前缀不会改音量", texts() == [] and len(fm.calls) == n, len(fm.calls))

    drain(b, [wecom_msg("/cc 帮我看看怎么调音量", "a5")])
    check("问「怎么调音量」照旧交给 Claude",
          any("已处理" in x for x in texts()) and len(fm.calls) == n, len(fm.calls))

    drain(b, [wecom_msg("/cc 30分钟后把音量调好", "a6")])
    check("「30 分钟后」里的 30 不是音量值",
          any("已处理" in x for x in texts()) and len(fm.calls) == n, len(fm.calls))

    b2 = make_bridge(tmpdir, fake_claude)
    b2.launcher = lc
    b2.media, keys2, _ = make_media(delay=6, mixer=FakeMixer(boom=True))
    drain(b2, [wecom_msg("/cc 音量调到30%", "a7")])
    check("混音器挂了时设音量明说做不到，不落给 Claude",
          any("读不到系统混音器" in x for x in texts()) and keys2 == [], keys2)
    drain(b2, [wecom_msg("/cc 大声点", "a8")])
    check("混音器挂了时相对调整照旧退回按键",
          keys2 == [[app_actions.VK_VOLUME_UP] * 5], keys2)

    b3 = make_bridge(tmpdir, fake_claude)
    b3.launcher = lc
    b3.media, keys3, _ = make_media(delay=6)          # 没混音器
    drain(b3, [wecom_msg("/cc 音量调到30%", "a9")])
    check("没混音器时「音量调到30%」交给 Claude",
          any("已处理" in x for x in texts()) and keys3 == [], keys3)

    off = make_bridge(tmpdir, fake_claude)            # media_control.enable=false
    off.launcher = lc
    drain(off, [wecom_msg("/cc 音量多少", "a10")])
    check("音量功能关掉时问音量也交给 Claude",
          any("已处理" in x for x in texts()), texts())


def test_media_chain(tmpdir, fake_claude):
    """桥这一层：开完应用顺手按键，认不出的照旧交给 Claude。"""
    b = make_bridge(tmpdir, fake_claude)
    lc, desktop, opened, killed, _ = make_launcher(tmpdir)
    b.launcher = lc
    b.media, keys, waited = make_media(delay=6)

    drain(b, [wecom_msg("/cc 打开波点音乐并开始播放", "b1")])
    t = texts()
    check("开完应用顺手按播放", keys == [[app_actions.VK_MEDIA_PLAY_PAUSE]], keys)
    check("等了几秒才按（应用要时间起来）", waited == [6.0], waited)
    check("回复里说了开了什么、按了什么",
          any("已请求打开 波点音乐" in x and "播放" in x for x in t), t)
    check("这一整套不经过 Claude", not any("已处理" in x for x in t), t)
    check("确实启动了程序", len(opened) == 1, opened)

    drain(b, [wecom_msg("/cc 下一首", "b2")])
    t = texts()
    check("光说动作也认", keys[-1] == [app_actions.VK_MEDIA_NEXT], keys)
    check("光说动作不会去开程序", len(opened) == 1, opened)
    check("光说动作不经过 Claude", not any("已处理" in x for x in t), t)

    drain(b, [wecom_msg("/cc 帮我看看怎么开始播放", "b3")])
    t = texts()
    check("前半句不像应用名就交给 Claude", any("已处理" in x for x in t), t)
    check("交给 Claude 时没乱按键", len(keys) == 2, keys)

    drain(b, [wecom_msg("/cc 打开网易云音乐并播放", "b4")])
    t = texts()
    check("桌面上没这个应用，整条交给 Claude", any("已处理" in x for x in t), t)
    check("没开成也就不按键", len(keys) == 2, keys)

    drain(b, [wecom_msg("下一首", "b5")])
    check("不带前缀不会误按键", texts() == [] and len(keys) == 2, (texts(), keys))

    drain(b, [wecom_msg("/cc 下一首", "b6", user=OTHER)])
    check("非白名单用户按不了键", texts() == [] and len(keys) == 2, (texts(), keys))

    off = make_bridge(tmpdir, fake_claude)
    off.launcher = lc
    check("media_control.enable=false 时不装播放控制", off.media is None)
    drain(off, [wecom_msg("/cc 下一首", "b7")])
    check("关掉后「下一首」交给 Claude", any("已处理" in x for x in texts()), texts())
    drain(off, [wecom_msg("/cc 打开波点音乐", "b8")])
    check("关掉播放控制不影响开应用",
          any("已请求打开 波点音乐" in x for x in texts()), texts())


# ---------- 屏幕亮度（走 WMI 的背光）----------

class FakeBright(object):
    """假 WMI：亮度那条路要起一次 PowerShell，自测里**一次都不许真跑**。

    真跑的话会改这台机器屏幕的亮度（真机上 2026-09-27 量过：读 90 → 写 88 → 读 88）。
    这里连 `_run_powershell` 都不碰，直接照着 `BrightnessControl` 认的签名做。
    `stuck=True` 演「设了但读不回新值」（屏幕没接住），用来验回话里那句「没动」。
    """

    def __init__(self, level=90, boom=False, monitors=1, stuck=False):
        self.level, self.boom, self.monitors, self.stuck = (level, boom, monitors,
                                                            stuck)
        self.calls = []

    def __call__(self, action, arg=0, lo=0, hi=100):
        self.calls.append((action, int(arg), int(lo), int(hi)))
        if self.boom:
            raise RuntimeError("WMI 不给（假的）")
        before = self.level
        if action == "set" and not self.stuck:
            self.level = max(int(lo), min(int(hi), int(arg)))
        return {"ok": True, "before": before, "after": self.level,
                "monitors": self.monitors, "methods": self.monitors,
                "set": 1 if action == "set" else 0}


def make_bright(level=90, step=10, **kw):
    """造一个只跟 FakeBright 说话的 BrightnessControl，回 (它, 假 WMI)。"""
    fb = FakeBright(level=level, **kw)
    return brightness.BrightnessControl(runner=fb, step=step), fb


def test_brightness_parse():
    """亮度：认什么、不认什么。**和音量的词表一个字都不能重叠。**"""
    for text, want in (("亮度调到50%", 50), ("亮度调到50", 50), ("把亮度调到 80%", 80),
                       ("屏幕亮度调到 30%", 30), ("亮度 50", 50), ("亮度一半", 50),
                       ("亮度拉满", 100), ("亮度最亮", 100), ("亮度最高", 100),
                       ("亮度最低", 5), ("亮度最暗", 5), ("让亮度到 70", 70),
                       ("帮我调亮度到 60%", 60)):
        check("认得出「%s」→ %s" % (text, want),
              brightness.parse_set(text) == want, brightness.parse_set(text))
    # ---- 不认的：说别的事、缺锚点、藏着时间的 ----
    for text in ("音量调到30%", "声音大一点", "调到50%", "亮度调到 30 分钟后",
                 "亮度从 30 调到 50", "亮度300", "看一下屏幕亮度设置在哪",
                 "把图片亮度调大点", "30分钟后把亮度调好", "亮度是多少",
                 "屏幕太亮了", "亮度" + "啊" * 40):
        check("不认成设亮度：%s" % text[:14], brightness.parse_set(text) is None,
              brightness.parse_set(text))
    check("音量那个解析器绝不抢「亮度调到50%」", volume.parse_set("亮度调到50%") is None)
    check("亮度这个解析器绝不抢「音量调到30%」",
          brightness.parse_set("音量调到30%") is None)

    for text in ("亮度多少", "屏幕亮度多少", "亮度", "屏幕亮度", "显示屏亮度",
                 "现在亮度是多少", "亮度多大", "屏幕多亮", "屏幕亮不亮"):
        check("认得出亮度问句：%s" % text, brightness.parse_query(text) is True,
              brightness.parse_query(text))
    # 「屏幕」两个字什么都指不了：分辨率、截图、锁屏状态全带着它
    for text in ("屏幕", "屏幕状态", "屏幕锁了吗", "屏幕截图发我",
                 "屏幕分辨率多少", "亮度调到50%", "屏幕亮一点", "音量多少"):
        check("不算亮度问句：%s" % text, brightness.parse_query(text) is False,
              brightness.parse_query(text))

    for text, want in (("屏幕亮一点", 1), ("屏幕太暗了", 1), ("亮度调亮", 1),
                       ("屏幕有点暗", 1), ("屏幕看不清", 1),
                       ("屏幕暗一点", -1), ("屏幕太亮了", -1), ("屏幕刺眼", -1),
                       ("屏幕调暗", -1), ("亮度调暗一些", -1)):
        check("认得出方向「%s」→ %s" % (text, want),
              brightness.parse_nudge(text) == want, brightness.parse_nudge(text))
    for text in ("太亮了", "刺眼", "亮点是什么", "屏幕亮点是什么", "暗点在哪",
                 "屏幕分辨率", "亮度调到50%", "音量小一点"):
        check("认不出方向（交给 Claude）：%s" % text,
              brightness.parse_nudge(text) == 0, brightness.parse_nudge(text))


def test_brightness_script():
    """拼给 PowerShell 的那段脚本：占位符都填上了、中文进不来、动作白名单卡死。"""
    s = brightness.build_script("get")
    check("脚本里没有没填上的占位符", "__" not in s, s[:120])
    check("脚本里一个中文都没有",
          not any(u"\u4e00" <= c <= u"\u9fff" for c in s), s[:120])
    check("脚本里用的是 WMI 那两个类",
          "WmiMonitorBrightness" in s and "WmiMonitorBrightnessMethods" in s)
    check("括号配对（PowerShell 少一个括号就整段不跑）",
          s.count("{") == s.count("}"), (s.count("{"), s.count("}")))
    s2 = brightness.build_script("set", 42, 5, 100)
    check("参数真进了脚本", "$arg=42" in s2 and "$min=5" in s2 and "$max=100" in s2,
          s2[:160])
    try:
        brightness.build_script("rm -rf /")
        ok = False
    except ValueError:
        ok = True
    check("动作白名单之外一律拒绝（用户原话永远进不了脚本正文）", ok)


def test_brightness_facade():
    """门面：读数、设值、相对调整，以及**下限和「读回来才算数」**。"""
    bc, fb = make_bright(level=90)
    text, handled = bc.query_text()
    check("问亮度回话了", handled and "90%" in text, text)
    check("读数只发一次 get", fb.calls == [("get", 0, 5, 100)], fb.calls)

    text, handled = bc.set_text(50)
    check("设 50 回了前后两个值", handled and "90% → 50%" in text, text)
    check("设的时候把下限上限一起送进去了", fb.calls[-1] == ("set", 50, 5, 100),
          fb.calls[-1])

    bc0, _ = make_bright(level=90)          # 换一个干净的：上面那条已经把它改到 50 了
    text, handled = bc0.set_text(0)
    check("★说「调到 0%」也不会真的给 0（人还在手机那头，屏幕全黑就麻烦了）",
          "90% → 5%" in text, text)
    check("回话里写明「你说的是 0%，落在 5%」", "你说的是 0%" in text, text)

    text, handled = bc.set_text(300)
    check("超上限夹回 100", "→ 100%" in text, text)

    bc2, fb2 = make_bright(level=90, step=10)
    text, _ = bc2.nudge_text(1)
    check("「亮一点」一次调 10%", "90% → 100%" in text, text)
    check("相对调整是「先读再设」，两次调用",
          [c[0] for c in fb2.calls] == ["get", "set"], fb2.calls)
    check("设的值是读回来那个加 10（不是假设的 90）",
          fb2.calls[-1][1] == 100, fb2.calls[-1])

    bc3, _ = make_bright(level=100)
    text, _ = bc3.nudge_text(1)
    check("到顶了直说，不假装又调高了", "100%（最亮）" in text, text)
    bc4, _ = make_bright(level=5)
    text, _ = bc4.nudge_text(-1)
    check("到底了直说，并说明是下限在拦", "下限" in text, text)

    bc5, _ = make_bright(level=50, stuck=True)
    text, _ = bc5.nudge_text(1)
    check("设了但屏幕没接住时如实说「没动」", "没动" in text, text)

    bc6, _ = make_bright(boom=True)
    text, handled = bc6.query_text()
    check("WMI 挂了时照实说、不假装读到数", handled and "读不到" in text, text)
    text, handled = bc6.set_text(50)
    check("WMI 挂了时设值也照实说", handled and "没调成" in text, text)

    check("不给 0 这件事是写死在模块里的",
          brightness.MIN_BRIGHTNESS >= 1 and brightness.MAX_BRIGHTNESS == 100)


def test_brightness_chain(tmpdir, fake_claude):
    """桥这一层：「亮度调到 50%」「亮度多少」「屏幕亮一点」走快速通道，不花额度。"""
    b = make_bridge(tmpdir, fake_claude)
    check("自测默认不装亮度（免得真去改屏幕）", b.brightness is None, b.brightness)
    drain(b, [wecom_msg("/cc 亮度调到50%", "br0")])
    check("没装的时候照旧交给 Claude", any("已处理" in x for x in texts()), texts())

    lc, desktop, opened, killed, _ = make_launcher(tmpdir)
    b.launcher = lc
    b.brightness, fb = make_bright(level=90)

    drain(b, [wecom_msg("/cc 亮度调到50%", "br1")])
    t = texts()
    check("「亮度调到50%」走亮度通道", fb.calls[-1] == ("set", 50, 5, 100), fb.calls)
    check("回话里有前后两个值", any("90% → 50%" in x for x in t), t)
    check("不花额度", not any("已处理" in x for x in t), t)
    check("调亮度没去动桌面上的应用", not opened and not killed, (opened, killed))

    drain(b, [wecom_msg("/cc 亮度多少", "br2")])
    check("「亮度多少」走亮度通道",
          any("50%" in x for x in texts()) and fb.calls[-1][0] == "get",
          fb.calls[-1])

    drain(b, [wecom_msg("/cc 屏幕亮一点", "br3")])
    check("「屏幕亮一点」也走这条",
          any("50% → 60%" in x for x in texts()), texts())

    # 不带前缀：一句都不许改
    n = len(fb.calls)
    drain(b, [wecom_msg("亮度调到10%", "br4")])
    check("不带前缀不会改亮度", texts() == [] and len(fb.calls) == n, len(fb.calls))

    # 这些必须落回 Claude：音量归音量、屏幕的别的事归别人、说别的事的也别碰
    for text, mid in (("音量调到30%", "br5"), ("屏幕分辨率多少", "br6"),
                      ("屏幕截图发我", "br7"), ("亮点是什么", "br8")):
        before = len(fb.calls)
        drain(b, [wecom_msg("/cc " + text, mid)])
        check("「%s」不归亮度管" % text,
              len(fb.calls) == before, fb.calls[before:])

    # 关掉之后直说做不到（不装 → 交给 Claude，和音量一致）
    off = make_bridge(tmpdir, fake_claude)
    check("brightness.enable=false 时不装亮度", off.brightness is None)
    drain(off, [wecom_msg("/cc 亮度调到50%", "br9")])
    check("关掉后交给 Claude", any("已处理" in x for x in texts()), texts())
