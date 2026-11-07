# -*- coding: utf-8 -*-
"""看画面与点鼠标：控件树、截图、待确认、看图兜底、DPI、桌面、自绘控件。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。
"""

import json
import os

import screen_control

from tests.selftest_base import USER, check, drain, make_bridge, texts, wecom_msg


def make_fake_screen(tmpdir, elements=None, title="波点音乐", shot_size=(400, 300),
                     confirm_seconds=60, vision_fallback=False, scale=1.0):
    """假的看画面/点鼠标：窗口、控件树、截图、点击全在手里，Linux 上也能跑。"""
    state = {
        "win": {"hwnd": 11, "title": title, "left": 100, "top": 50,
                "width": 800, "height": 600, "minimized": False},
        "els": [{"name": "播放全部", "type": "ControlType.Button",
                 "x": 200, "y": 300, "w": 80, "h": 40, "enabled": True},
                {"name": "开始播放", "type": "ControlType.Button",
                 "x": 400, "y": 300, "w": 100, "h": 40, "enabled": True},
                {"name": "", "type": "ControlType.Button",
                 "x": 0, "y": 0, "w": 10, "h": 10, "enabled": True},
                {"name": "看不见的", "type": "ControlType.Button",
                 "x": 5, "y": 5, "w": 0, "h": 0, "enabled": True}]
        if elements is None else list(elements),
        "clicks": [], "shots": [], "probes": [0], "scale": float(scale),
        # 假的「现在开着哪些窗口」：Program Manager 就是桌面，应该被当成壳子过滤掉
        "wins": [{"hwnd": 99, "title": "Program Manager", "left": 0, "top": 0,
                  "width": 1920, "height": 1080, "minimized": False},
                 {"hwnd": 11, "title": title, "left": 100, "top": 50,
                  "width": 800, "height": 600, "minimized": False},
                 {"hwnd": 12, "title": "微信", "left": 0, "top": 0,
                  "width": 400, "height": 300, "minimized": False}],
        "activated": [], "activate_ok": True, "raw": None}

    def window_fn():
        return dict(state["win"]) if state["win"] else None

    def prober(hwnd, cap=300):
        state["probes"][0] += 1
        # raw 有值时假装是新版探测器（带诊断信息的 dict）；默认还是老的裸列表，
        # 好让「注入的探测器只给列表」这条兼容路径也一直被走到
        if state["raw"] is not None:
            return json.loads(json.dumps(state["raw"]))
        return list(state["els"])

    def shooter(path, left, top, w, h):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake")
        state["shots"].append((path, left, top, w, h))
        return shot_size

    def clicker(x, y, double=False, button="left"):
        state["clicks"].append((x, y, bool(double)))

    def windows_fn():
        return [dict(w) for w in state["wins"]]

    def activator(hwnd):
        state["activated"].append(int(hwnd))
        if state["activate_ok"]:
            for w in state["wins"]:
                if w["hwnd"] == hwnd:
                    state["win"] = dict(w, minimized=False)

    sc = screen_control.ScreenControl(
        shots_dir=os.path.join(tmpdir, "shots"), confirm_seconds=confirm_seconds,
        vision_fallback=vision_fallback, keep_shots=1,
        window_fn=window_fn, prober=prober, shooter=shooter, clicker=clicker,
        dpi_fn=lambda: state["scale"], windows_fn=windows_fn, activator=activator)
    return sc, state


def test_screen_parse():
    p = screen_control.parse_click
    check("认出「点一下 X」", p("点一下开始播放") == ("开始播放", False))
    check("认出「双击 X」", p("双击 波点音乐") == ("波点音乐", True))
    check("「帮我点确定」也认", p("帮我点确定") == ("确定", False))
    check("光一个「点」不算指令", p("点") == (None, False))
    check("带路径的不当点击看", p("点一下 D:\\a\\b.md") == (None, False))
    check("句子里还有别的事就不拦", p("看看怎么点这个按钮然后再说") == (None, False))
    check("名字太长不当点击看",
          p("点一下那个特别长的名字一二三四五六七八九十十一十二十三") == (None, False))
    r = screen_control.read_reply
    check("「确认」= 确认", r("确认") == ("yes", None))
    check("「算了」= 取消", r("算了") == ("no", None))
    check("全角数字也认", r("２") == ("pick", 1))
    check("说别的事不算确认", r("看看今天的日志") == (None, None), r("看看今天的日志"))


def test_screen_tree(tmpdir):
    sc, state = make_fake_screen(tmpdir)
    els = sc.elements(11)
    check("没名字和零尺寸的控件被丢掉", len(els) == 2, [e["name"] for e in els])
    check("点的是矩形中心", (els[1]["x"], els[1]["y"]) == (450, 320),
          (els[1]["x"], els[1]["y"]))
    check("控件类型说成中文", els[0]["type"] == "按钮", els[0]["type"])
    check("精确名字只中一个", [e["name"] for e in sc.find("开始播放", els)] == ["开始播放"])
    check("找不到就返回空", sc.find("随便什么", els) == [])
    d = sc.describe(state["win"], els)
    check("/see 里有窗口名和控件名", "波点音乐" in d and "开始播放" in d, d)
    check("控件树空的时候提示可以看图", "自己画界面" in sc.describe(state["win"], []),
          sc.describe(state["win"], []))
    mini = dict(state["win"], minimized=True)
    check("最小化的窗口直说看不到", "最小化" in sc.describe(mini, els))
    check("终端类窗口一律拒点", sc.refuse_reason("Windows PowerShell") is not None)
    check("普通窗口不拒", sc.refuse_reason("波点音乐") is None)
    check("图片坐标按窗口矩形换算",
          sc.to_screen(state["win"], 400, 300, 200, 150) == (500, 350),
          sc.to_screen(state["win"], 400, 300, 200, 150))
    check("越界坐标被夹回窗口里",
          sc.to_screen(state["win"], 400, 300, 9999, 9999) == (899, 649),
          sc.to_screen(state["win"], 400, 300, 9999, 9999))
    p1, _, _ = sc.shoot(state["win"])
    p2, _, _ = sc.shoot(state["win"])
    left = [f for f in os.listdir(sc.shots_dir) if f.endswith(".png")]
    check("截图只留最近一张", len(left) == 1 and os.path.basename(p2) in left, left)
    check("截的是窗口那一块，不是整屏",
          state["shots"][-1][1:] == (100, 50, 800, 600), state["shots"][-1])
    check("旧截图被删掉", not os.path.exists(p1) or p1 == p2)


def screen_bridge(tmpdir, fake_claude, **kw):
    """带假 screen 的桥。用例只需要关心控件树和点击记录。"""
    b = make_bridge(tmpdir, fake_claude)
    b.screen, state = make_fake_screen(tmpdir, **kw)
    b._pending = {}
    return b, state


def test_screen_confirm(tmpdir, fake_claude):
    b, st = screen_bridge(tmpdir, fake_claude)

    drain(b, [wecom_msg("/see", "s1")])
    check("/see 不需要前缀也能用", any("波点音乐" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 点一下开始播放", "s2")])
    check("先说打算点哪儿，不动手",
          any("打算点" in x and "开始播放" in x for x in texts()) and st["clicks"] == [],
          texts())
    check("确认之前不点", st["clicks"] == [], st["clicks"])
    drain(b, [wecom_msg("确认", "s3")])
    check("回「确认」才真点，坐标是控件中心", st["clicks"] == [(450, 320, False)],
          st["clicks"])
    check("点完待确认状态就清了", not b._pending, b._pending)

    st["clicks"] = []
    drain(b, [wecom_msg("/cc 点一下开始播放", "s4")])
    drain(b, [wecom_msg("取消", "s5")])
    check("回「取消」不点", st["clicks"] == [] and not b._pending, st["clicks"])

    st["clicks"] = []
    drain(b, [wecom_msg("/cc 点一下开始播放", "s6")])
    b._pending[USER]["at"] -= 9999
    drain(b, [wecom_msg("确认", "s7")])
    check("超过时限就作废，不点", st["clicks"] == [], st["clicks"])
    check("作废时说清了原因", any("作废" in x for x in texts()), texts())

    st["clicks"] = []
    drain(b, [wecom_msg("/cc 点一下开始播放", "s8")])
    drain(b, [wecom_msg("/cc 现在几点", "s9")])
    check("中间说了别的，那次点击作废", not b._pending and st["clicks"] == [],
          (b._pending, st["clicks"]))
    check("说别的那条照旧交给 Claude", any("已处理" in x for x in texts()), texts())
    drain(b, [wecom_msg("确认", "s10")])
    check("作废之后再说「确认」也不点", st["clicks"] == [], st["clicks"])


def test_screen_ambiguous(tmpdir, fake_claude):
    b, st = screen_bridge(tmpdir, fake_claude)
    # 两个都以「播放」开头，一句「点一下播放」就该问清楚点哪个，不许自己挑
    st["els"] = [{"name": "播放全部", "type": "ControlType.Button",
                  "x": 200, "y": 300, "w": 80, "h": 40, "enabled": True},
                 {"name": "播放列表", "type": "ControlType.ListItem",
                  "x": 400, "y": 300, "w": 100, "h": 40, "enabled": True}]
    drain(b, [wecom_msg("/cc 点一下播放", "a2")])
    check("有几个像的就摆出来问", any("有 2 个" in x for x in texts()), texts())
    check("歧义时不点", st["clicks"] == [], st["clicks"])
    drain(b, [wecom_msg("3", "a3")])
    check("数字超出范围时提醒，不点",
          st["clicks"] == [] and any("回 1 到 2" in x for x in texts()), texts())
    check("提醒之后还等着确认", USER in b._pending, b._pending)
    drain(b, [wecom_msg("2", "a4")])
    check("回数字就点那一个", st["clicks"] == [(450, 320, False)], st["clicks"])

    st["clicks"] = []
    drain(b, [wecom_msg("/cc 点一下播放", "a5")])
    drain(b, [wecom_msg("确认", "a6")])
    check("歧义时光说「确认」不点，要数字",
          st["clicks"] == [] and any("回数字" in x for x in texts()), texts())


def test_screen_refuse(tmpdir, fake_claude):
    b, st = screen_bridge(tmpdir, fake_claude)

    st["win"]["title"] = "Windows PowerShell"
    drain(b, [wecom_msg("/cc 点一下开始播放", "r1")])
    check("终端窗口上直接拒绝，不进待确认",
          not b._pending and any("我不点" in x for x in texts()), texts())
    st["win"]["title"] = "波点音乐"

    st["win"]["minimized"] = True
    drain(b, [wecom_msg("/cc 点一下开始播放", "r2")])
    check("最小化就先让你切到前面",
          not b._pending and any("最小化" in x for x in texts()), texts())
    st["win"]["minimized"] = False

    drain(b, [wecom_msg("/cc 点一下开始播放", "r3")])
    st["win"]["hwnd"] = 22
    st["win"]["title"] = "微信"
    drain(b, [wecom_msg("确认", "r4")])
    check("确认时窗口换了就不点",
          st["clicks"] == [] and any("不是刚才那个" in x for x in texts()), texts())
    st["win"].update({"hwnd": 11, "title": "波点音乐"})

    drain(b, [wecom_msg("/cc 点一下开始播放", "r5")])
    st["win"]["left"] += 60                      # 窗口被挪了，绝对坐标就不对了
    drain(b, [wecom_msg("确认", "r6")])
    check("窗口被挪过位置也不点",
          st["clicks"] == [] and any("挪过位置" in x for x in texts()), texts())
    st["win"]["left"] -= 60

    st["els"] = []
    drain(b, [wecom_msg("/cc 点一下开始播放", "r7")])
    check("控件树里没有、又没开看图兜底时，只给建议不乱点",
          st["clicks"] == [] and any("/see" in x for x in texts()), texts())
    check("这种情况也不留待确认状态", not b._pending, b._pending)

    st["win"] = None
    drain(b, [wecom_msg("/cc 点一下开始播放", "r8")])
    check("读不到前台窗口时直说", any("读不到前台窗口" in x for x in texts()), texts())


def test_screen_vision(tmpdir, fake_claude):
    """控件树里找不到时的退路：截图 → 让 CLI 给坐标 → 照旧等确认。"""
    b, st = screen_bridge(tmpdir, fake_claude, vision_fallback=True)
    st["els"] = []                                  # 自己画界面的应用，控件树是空的
    asked = []

    def fake_run(user, prompt):
        asked.append((user, prompt))
        return '好的。{"found": true, "x": 200, "y": 150, "what": "播放按钮"}'

    b.runner.run = fake_run
    drain(b, [wecom_msg("/cc 点一下开始播放", "v1")])
    t = texts()
    check("先说一句要去截图了", any("截个图" in x for x in t), t)
    check("截了图", len(st["shots"]) == 1, st["shots"])
    check("图片路径进了 prompt", asked and st["shots"][0][0] in asked[0][1],
          asked[:1])
    check("看图用的是单独一段会话，不混进正常上下文",
          asked and asked[0][0].startswith("screen:"), asked[:1])
    check("看图给的坐标换算成了屏幕坐标（400x300 的图 → 800x600 的窗口）",
          any("500,350" in x for x in t), t)
    check("说清了这是猜的", any("猜的" in x for x in t), t)
    check("看图之后同样要确认才点", st["clicks"] == [] and USER in b._pending,
          st["clicks"])
    drain(b, [wecom_msg("确认", "v2")])
    check("确认后按换算出来的坐标点", st["clicks"] == [(500, 350, False)], st["clicks"])

    b.runner.run = lambda user, prompt: "我看不出来哪个是播放按钮。"
    st["clicks"] = []
    drain(b, [wecom_msg("/cc 点一下开始播放", "v3")])
    check("看图也没找到就直说，不乱点",
          st["clicks"] == [] and not b._pending
          and any("也没找到" in x for x in texts()), texts())


def test_screen_dpi(tmpdir, fake_claude):
    """坐标体系对不上时宁可不点——真机上就是这里栽过一次。"""
    b, st = screen_bridge(tmpdir, fake_claude)
    check("缩放 1.0 时不啰嗦", b.screen.dpi_problem() is None, b.screen.dpi_problem())
    drain(b, [wecom_msg("/see", "d1")])
    check("/see 正常时不提 DPI", not any("DPI" in x for x in texts()), texts())

    st["scale"] = 2.0                    # 200% 缩放，且进程没设成 DPI 感知
    msg = b.screen.dpi_problem()
    check("量出缩放不是 1 就给说明", msg and "200%" in msg, msg)
    drain(b, [wecom_msg("/see", "d2")])
    check("/see 会把这件事说出来", any("200%" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 点一下开始播放", "d3")])
    check("坐标不可信时不进待确认、不点",
          st["clicks"] == [] and not b._pending, (st["clicks"], b._pending))
    check("拒绝时说清了为什么", any("DPI 感知" in x for x in texts()), texts())

    st["scale"] = 1.0                    # 计划的时候是好的
    drain(b, [wecom_msg("/cc 点一下开始播放", "d4")])
    check("这时候正常进待确认", USER in b._pending, b._pending)
    st["scale"] = 2.0                    # 等确认期间坐标体系变了（换了屏、改了缩放）
    drain(b, [wecom_msg("确认", "d5")])
    check("确认那一刻再查一遍，对不上就不点", st["clicks"] == [], st["clicks"])

    st["scale"] = 1.0
    st["clicks"] = []
    b._pending = {}
    drain(b, [wecom_msg("/cc 点一下开始播放", "d6")])
    drain(b, [wecom_msg("确认", "d7")])
    check("恢复正常之后照旧能点", st["clicks"] == [(450, 320, False)], st["clicks"])

    def boom():
        raise OSError("量不出来")

    b.screen._dpi_fn = boom
    check("量缩放本身失败时不拦路", b.screen.dpi_problem() is None)


def test_screen_desktop(tmpdir, fake_claude):
    """前台是桌面时的正确行为：别看桌面、别截桌面，让用户点名要哪个窗口。"""
    check("Program Manager 认成桌面", screen_control.is_shell_window("Program Manager"))
    check("输入法那些隐形窗口也算壳子", screen_control.is_shell_window("Default IME"))
    check("正经应用不算壳子", not screen_control.is_shell_window("波点音乐"))
    check("标题里带「搜索」的应用别误伤",
          not screen_control.is_shell_window("网易云音乐 - 搜索结果"))

    b, st = screen_bridge(tmpdir, fake_claude, vision_fallback=True)
    check("列窗口时把桌面滤掉",
          [w["title"] for w in b.screen.windows()] == ["波点音乐", "微信"],
          [w["title"] for w in b.screen.windows()])

    st["win"] = dict(st["wins"][0])                 # 人不在电脑前，前台就是桌面
    drain(b, [wecom_msg("/see", "w1")])
    t = texts()
    check("/see 直说现在是桌面", any("桌面" in x for x in t), t)
    check("顺手把开着的窗口列出来", any("波点音乐" in x and "微信" in x for x in t), t)

    drain(b, [wecom_msg("/cc 点一下开始播放", "w2")])
    check("桌面上不去截图、不花额度",
          st["shots"] == [] and st["clicks"] == [] and not b._pending,
          (st["shots"], st["clicks"]))
    check("并且告诉你该怎么做", any("/see" in x for x in texts()), texts())

    drain(b, [wecom_msg("/see 波点音乐", "w3")])
    check("带名字的 /see 会把那个窗口切到前面", st["activated"] == [11], st["activated"])
    check("切过去之后才列控件", any("开始播放" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 点一下开始播放", "w4")])
    drain(b, [wecom_msg("确认", "w5")])
    check("这时候点得下去了", st["clicks"] == [(450, 320, False)], st["clicks"])

    st["activated"] = []
    drain(b, [wecom_msg("/see 没这个窗口", "w6")])
    check("窗口名不存在时给清单，不乱切",
          st["activated"] == [] and any("没有哪个窗口叫" in x for x in texts()), texts())

    drain(b, [wecom_msg("/windows", "w7")])
    check("/windows 列窗口", any("波点音乐" in x for x in texts()), texts())

    st["win"] = dict(st["wins"][0])
    st["activate_ok"] = False                        # 系统的前台锁没让它切过去
    st["activated"] = []
    drain(b, [wecom_msg("/see 微信", "w8")])
    check("切不过去就如实说，不假装成功",
          st["activated"] == [12] and any("没让它到最前面" in x for x in texts()),
          texts())

    check("「/seek 什么」不当窗口名解析", b._see_target("/seek 什么") is None)
    check("不空格也认", b._see_target("/see波点音乐") == "波点音乐")
    check("带路径的不当窗口名", b._see_target("/see D:\\a") is None)


def test_screen_probe_diag(tmpdir, fake_claude):
    """「控件树是空的」必须分清三种：查失败、有节点但没名字、真的一个都没有。

    真机上连栽两次都是被这一句话盖住的，所以这组是回归测试，别删。
    """
    b, st = screen_bridge(tmpdir, fake_claude)

    # ① 探测这一步自己失败了（Add-Type 挂了、FromHandle 给空、FindAll 抛异常）
    st["raw"] = {"items": [], "wide": [], "total": 0, "scanned": 0,
                 "kinds": {}, "error": "Add-Type 找不到 UIAutomationClient"}
    info = {}
    check("查失败时返回空", b.screen.elements(11, info) == [])
    check("查失败会被记下来", info.get("error") == "Add-Type 找不到 UIAutomationClient",
          info)
    d = b.screen.describe(st["win"], [], info=info)
    check("查失败不许说成「窗口自己画界面」", "自己画界面" not in d, d)
    check("查失败要把原因说出来", "UIAutomationClient" in d and "失败" in d, d)
    drain(b, [wecom_msg("/see", "p1")])
    check("/see 也照实说", any("UIAutomationClient" in x for x in texts()), texts())
    drain(b, [wecom_msg("/cc 点一下开始播放", "p2")])
    check("查失败时不说「不存在」",
          any("读控件树失败" in x for x in texts()), texts())
    check("查失败时不点、不进待确认",
          st["clicks"] == [] and not b._pending, (st["clicks"], b._pending))

    # ② 摸到一堆节点，但没有一个是带名字可见的可点控件
    st["raw"] = {"items": [], "wide": [], "total": 57, "scanned": 57,
                 "kinds": {"Pane": 30, "Text": 27}, "error": None}
    info = {}
    check("这种情况也返回空", b.screen.elements(11, info) == [])
    d = b.screen.describe(st["win"], [], info=info)
    check("说清摸到了多少节点", "57" in d, d)
    check("顺手报类型分布", "Pane" in d and "Text" in d, d)

    # ③ 自绘界面：标准类型一个都没有，从 Custom/Text 里翻出候选（小的排前面）
    st["raw"] = {"items": [],
                 "wide": [{"name": "整块面板", "type": "Pane", "x": 100, "y": 50,
                           "w": 800, "h": 600, "enabled": True},
                          {"name": "开始播放", "type": "Custom", "x": 400, "y": 300,
                           "w": 100, "h": 40, "enabled": True}],
                 "total": 402, "scanned": 402,
                 "kinds": {"Custom": 380, "Pane": 22}, "error": None}
    info = {}
    els = b.screen.elements(11, info)
    check("兜底候选按面积从小到大", [e["name"] for e in els] == ["开始播放", "整块面板"],
          [e["name"] for e in els])
    check("标明这是放宽之后翻出来的", info.get("widened") is True, info)
    d = b.screen.describe(st["win"], els, info=info)
    check("列出来的同时提醒不一定准", "开始播放" in d and "不一定准" in d, d)

    drain(b, [wecom_msg("/cc 点一下开始播放", "p3")])
    t = texts()
    check("自绘控件也能进待确认", USER in b._pending, b._pending)
    check("但要说清是自绘控件", any("自绘控件" in x for x in t), t)
    drain(b, [wecom_msg("确认", "p4")])
    check("确认后点在那个控件的中心", st["clicks"] == [(450, 320, False)], st["clicks"])

    # ④ 真的一个节点都没有，才说「自己画界面」
    st["raw"] = {"items": [], "wide": [], "total": 0, "scanned": 0,
                 "kinds": {}, "error": None}
    info = {}
    b.screen.elements(11, info)
    check("真空的时候才提看图",
          "一个节点都没有" in b.screen.describe(st["win"], [], info=info),
          b.screen.describe(st["win"], [], info=info))

    # ⑤ 探测器抛异常（超时、PowerShell 没输出）同样算查失败，不算「空」
    st["raw"] = {"items": {"name": "只有一条", "type": "Button", "x": 400, "y": 300,
                           "w": 100, "h": 40, "enabled": True},
                 "wide": [], "total": 1, "scanned": 1,
                 "kinds": {"Button": 1}, "error": None}
    check("只有一条时 ConvertTo-Json 给对象也认",
          [e["name"] for e in b.screen.elements(11)] == ["只有一条"],
          b.screen.elements(11))

    st["raw"] = None

    # ⑥ 探测器抛异常（超时、PowerShell 没输出）同样算查失败，不算「空」
    def boom(hwnd, cap=300):
        raise RuntimeError("PowerShell 没有输出")

    b.screen._prober = boom
    info = {}
    check("抛异常时也返回空", b.screen.elements(11, info) == [])
    check("抛异常的原因也记下来", "PowerShell" in (info.get("error") or ""), info)


def test_screen_surface_only(tmpdir, fake_claude):
    """Flutter/自绘应用只报一块画布（真机上波点音乐就是一个 FLUTTERVIEW）。

    把画布当按钮列给用户毫无用处——点它的中心就是点窗口正中间。所以要认出来、
    直说「这个应用没把界面暴露出来」，并把人引到看图那条路上。
    """
    check("FLUTTERVIEW 是画布不是按钮", screen_control._is_surface("FLUTTERVIEW"))
    check("Chrome Legacy Window 同理",
          screen_control._is_surface("Chrome Legacy Window"))
    check("正常控件名不会被误判", not screen_control._is_surface("开始播放"))

    b, st = screen_bridge(tmpdir, fake_claude, vision_fallback=True)
    st["raw"] = {"items": [],
                 "wide": [{"name": "FLUTTERVIEW", "type": "Custom", "x": 100,
                           "y": 50, "w": 800, "h": 600, "enabled": True}],
                 "total": 1, "scanned": 1, "kinds": {"Custom": 1}, "error": None}
    info = {}
    els = b.screen.elements(11, info)
    check("只有画布时不给候选", els == [], els)
    check("并且记下是画布", info.get("surface_only") == "FLUTTERVIEW", info)
    d = b.screen.describe(st["win"], els, info=info)
    check("/see 说清是一块画布", "画布" in d and "FLUTTERVIEW" in d, d)
    check("并且指路到看图", "看看画面" in d, d)

    drain(b, [wecom_msg("/see", "s1")])
    check("/see 走完整条路也是这么说", any("画布" in x for x in texts()), texts())

    # 画布之外还有真候选时，画布本身照旧不出现在候选里
    st["raw"] = {"items": [],
                 "wide": [{"name": "FLUTTERVIEW", "type": "Custom", "x": 100,
                           "y": 50, "w": 800, "h": 600, "enabled": True},
                          {"name": "开始播放", "type": "Text", "x": 400, "y": 300,
                           "w": 100, "h": 40, "enabled": True}],
                 "total": 2, "scanned": 2, "kinds": {"Custom": 1, "Text": 1},
                 "error": None}
    info = {}
    els = b.screen.elements(11, info)
    check("画布被剔掉，只留真候选", [e["name"] for e in els] == ["开始播放"],
          [e["name"] for e in els])
    check("这时候不再算「只有画布」", "surface_only" not in info, info)


def test_screen_look(tmpdir, fake_claude):
    """「看看画面」：截一张图让 CLI 念一遍上面有什么。控件树不通的应用只剩这条路。

    这句话本来只写在回复里、却没有任何代码接——回复承诺了一个不存在的指令，
    比没有这个功能更糟。所以这组盯着它别再退化。
    """
    p = screen_control.parse_look
    check("「看看画面」认得出", p("看看画面"))
    check("「帮我看看屏幕」也认", p("帮我看看屏幕"))
    check("「截个图看看」也认", p("截个图看看"))
    check("带后文的不认（那是给 Claude 的活）", not p("看看画面里那个按钮怎么写的"))
    check("「看看今天的日志」不认", not p("看看今天的日志"))

    b, st = screen_bridge(tmpdir, fake_claude, vision_fallback=True)
    asked = []

    def fake_run(user, prompt):
        asked.append((user, prompt))
        return "这是波点音乐的播放页，正在放《海阔天空》。\n- 播放\n- 下一首\n- 我喜欢"

    b.runner.run = fake_run
    drain(b, [wecom_msg("看看画面", "k1")])          # 不带 /cc 前缀也认
    t = texts()
    check("先说一句要去截图、要花额度",
          any("额度" in x for x in t), t)
    check("截了图，而且只截窗口那一块",
          len(st["shots"]) == 1 and st["shots"][0][1:] == (100, 50, 800, 600),
          st["shots"])
    check("走的是单独一段会话", asked and asked[0][0].startswith("screen:"), asked[:1])
    check("prompt 里明确不要坐标", asked and "不要给坐标" in asked[0][1], asked[:1])
    check("把画面上有什么念回来", any("海阔天空" in x for x in t), t)
    check("看完不点任何东西", st["clicks"] == [] and not b._pending,
          (st["clicks"], b._pending))
    check("顺手告诉你接下来怎么点", any("点一下" in x for x in t), t)

    st["win"] = dict(st["wins"][0])                  # 前台是桌面
    st["shots"] = []
    drain(b, [wecom_msg("看看画面", "k2")])
    check("桌面上不截图、不花额度",
          st["shots"] == [] and any("桌面" in x for x in texts()), texts())

    st["win"] = dict(st["wins"][1])
    b.screen.vision_fallback = False
    drain(b, [wecom_msg("看看画面", "k3")])
    check("看图关掉时直说，不偷偷截图",
          st["shots"] == [] and any("关着的" in x for x in texts()), texts())
    b.screen.vision_fallback = True

    # 待确认的点击遇到「看看画面」要作废——说了别的事就不该再点
    st["raw"] = None
    drain(b, [wecom_msg("/cc 点一下开始播放", "k4")])
    check("先进待确认", USER in b._pending, b._pending)
    drain(b, [wecom_msg("看看画面", "k5")])
    check("「看看画面」会让待确认的点击作废",
          not b._pending and st["clicks"] == [], (b._pending, st["clicks"]))
