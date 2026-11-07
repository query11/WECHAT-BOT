# -*- coding: utf-8 -*-
"""开关应用：扫桌面图标、名字怎么配、开和关，以及走到桥上的整条链路。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。

假桌面（DESKTOP_FILES / FakeShell / make_launcher）在 selftest_base.py——
不只这一组要用它。
"""

import os

import app_launcher

from tests.selftest_base import (OTHER, check, drain, make_bridge, make_launcher,
                           texts, wecom_msg)


# ---------- 开关应用 ----------

def test_app_scan(tmpdir):
    lc, desktop, _, _, _ = make_launcher(tmpdir)
    names = lc.names()
    check("桌面只扫 lnk/url/exe", "笔记" not in names and "desktop" not in names, names)
    check("桌面图标都扫到了", "波点音乐" in names and "便签" in names, names)
    check("名字就是文件名去掉后缀", "CC Switch" in names, names)


def test_app_match(tmpdir):
    lc, desktop, opened, _, _ = make_launcher(tmpdir)
    check("精确匹配", [n for n, _ in lc.match("波点音乐")] == ["波点音乐"], lc.match("波点音乐"))
    check("忽略大小写和空格", [n for n, _ in lc.match("ccswitch")] == ["CC Switch"],
          lc.match("ccswitch"))
    check("前缀匹配", [n for n, _ in lc.match("剪映")] == ["剪映专业版"], lc.match("剪映"))
    check("多打一两个字也认", [n for n, _ in lc.match("微信啊")] == ["微信"], lc.match("微信啊"))
    check("歧义返回多个候选", len(lc.match("PDF")) == 2, lc.match("PDF"))
    check("桌面上没有的返回空", lc.match("网易云音乐") == [], lc.match("网易云音乐"))


def test_app_parse():
    cases = [
        ("打开波点音乐", ("open", "波点音乐")),
        ("帮我打开 CC Switch", ("open", "CC Switch")),
        ("启动一下 微信", ("open", "微信")),
        ("open pdfgear", ("open", "pdfgear")),
        ("/open 波点音乐", ("open", "波点音乐")),
        ("关闭微信", ("close", "微信")),
        ("把微信关掉", (None, None)),            # 动词不在开头，不猜
        ("/close 微信", ("close", "微信")),
        ("打开 D:\\CC Workspace\\a.md 看看写了什么", (None, None)),
        ("打开浏览器，然后搜一下天气", (None, None)),
        ("startup 检查一下开机项", (None, None)),
        ("打开", (None, None)),
        ("看下这个目录", (None, None)),
        ("打开桌面上那个文件夹", (None, None)),
    ]
    for text, want in cases:
        got = app_launcher.parse_command(text)
        check("句式解析：%s" % text, got == want, got)
    check("尾巴上的语气词被剃掉", app_launcher.parse_command("打开波点音乐吧") ==
          ("open", "波点音乐"), app_launcher.parse_command("打开波点音乐吧"))
    check("「一下」「程序」这种废话也剃", app_launcher.parse_command("打开微信程序") ==
          ("open", "微信"), app_launcher.parse_command("打开微信程序"))


def test_app_open(tmpdir):
    lc, desktop, opened, _, _ = make_launcher(tmpdir)
    text, handled = lc.open("波点音乐")
    check("打开命中的应用", handled and "已请求打开 波点音乐" in text, text)
    check("启动的是桌面上那个快捷方式",
          opened == [os.path.join(desktop, "波点音乐.lnk")], opened)

    text, handled = lc.open("网易云音乐")
    check("桌面上没有就交回 Claude", (text, handled) == (None, False), (text, handled))
    check("没命中不会启动任何东西", len(opened) == 1, opened)

    text, handled = lc.open("PDF")
    check("有歧义时不动手，回去问", handled and "PDFgear" in text and "PDF2Zh" in text, text)
    check("有歧义时确实没启动", len(opened) == 1, opened)

    gone = os.path.join(desktop, "临时的.lnk")
    with open(gone, "w", encoding="utf-8") as fh:
        fh.write("x")
    lc.refresh()
    os.remove(gone)
    text, handled = lc.open("临时的")
    check("图标被挪走了会说清楚", handled and "不在了" in text, text)


def test_app_close(tmpdir):
    lc, desktop, _, killed, shell = make_launcher(tmpdir)
    text, handled = lc.close("波点音乐")
    check("关闭走进程名", handled and "已请求关闭 波点音乐" in text, text)
    check("taskkill 收到的是解析出来的 exe", killed == ["bodian_pc.exe"], killed)
    check("一次 PowerShell 把所有快捷方式都解析了", shell.calls == 1, shell.calls)

    lc.close("微信")
    check("第二次关不再解析（走缓存）", shell.calls == 1, shell.calls)
    check("第二次关的是微信", killed[-1] == "WeChat.exe", killed)

    text, handled = lc.close("系统资源管理器")
    check("指向系统进程的一律拒关", handled and "不能关" in text, text)
    check("拒关时没调用 taskkill", len(killed) == 2, killed)

    text, handled = lc.close("网易云音乐")
    check("关一个桌面上没有的，交回 Claude", (text, handled) == (None, False), (text, handled))

    lc2 = make_launcher(tmpdir, kill_code=128)[0]
    text, handled = lc2.close("波点音乐")
    check("没在跑时如实说", handled and "没在跑" in text, text)

    lc3 = make_launcher(tmpdir, allow_close=False)[0]
    check("allow_close=false 时不接关闭请求", lc3.close("波点音乐") == (None, False),
          lc3.close("波点音乐"))
    check("关掉关闭功能不影响打开", lc3.open("波点音乐")[1] is True)


def test_app_chain(tmpdir, fake_claude):
    """桥这一层：命中的自己回，认不出的照旧交给 Claude。"""
    b = make_bridge(tmpdir, fake_claude)
    lc, desktop, opened, killed, _ = make_launcher(tmpdir)
    b.launcher = lc

    drain(b, [wecom_msg("/cc 打开波点音乐", "a1")])
    t = texts()
    check("桥拦下打开请求", any("已请求打开 波点音乐" in x for x in t), t)
    check("打开不经过 Claude", not any("已处理" in x for x in t), t)
    check("桥确实启动了程序", len(opened) == 1, opened)

    drain(b, [wecom_msg("/cc 关闭微信", "a2")])
    check("桥拦下关闭请求", any("已请求关闭 微信" in x for x in texts()), texts())
    check("关的是 WeChat.exe", killed == ["WeChat.exe"], killed)

    drain(b, [wecom_msg("/cc 打开 D:\\CC Workspace\\a.md 看看写了什么", "a3")])
    t = texts()
    check("带路径的「打开」照旧交给 Claude", any("已处理" in x for x in t), t)
    check("这种情况没乱启动程序", len(opened) == 1, opened)

    drain(b, [wecom_msg("/cc 打开网易云音乐", "a4")])
    t = texts()
    check("桌面上没有的应用交给 Claude 去解释", any("已处理" in x for x in t), t)

    drain(b, [wecom_msg("/apps", "a5")])
    t = texts()
    check("/apps 不需要前缀就能看清单", any("波点音乐" in x for x in t), t)

    drain(b, [wecom_msg("打开波点音乐", "a6")])
    check("不带前缀不会误开程序", texts() == [] and len(opened) == 1, (texts(), opened))

    drain(b, [wecom_msg("/cc 打开波点音乐", "a7", user=OTHER)])
    check("非白名单用户开不了程序", texts() == [] and len(opened) == 1, (texts(), opened))

    off = make_bridge(tmpdir, fake_claude)
    check("app_launch.enable=false 时不装 launcher", off.launcher is None)
    drain(off, [wecom_msg("/cc 打开波点音乐", "a8")])
    check("功能关掉后「打开」照旧给 Claude", any("已处理" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc /open", "a9")])
    t = texts()
    check("/open 后面没跟名字就给清单", any("波点音乐" in x for x in t), t)
    check("给清单时没启动任何东西", len(opened) == 1, opened)
