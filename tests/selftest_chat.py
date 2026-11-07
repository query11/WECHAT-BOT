# -*- coding: utf-8 -*-
"""对话模式（免前缀）：开关、空闲超时自动退出、以及裸消息那句提示。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。
"""

import time

import wechat_bridge

from tests.selftest_base import (USER, FakeILink, check, drain, make_bridge, texts,
                           wecom_msg)


# 拿它把 client.send_text 弄坏，验「推不出去也照样退出」（原来在文件末尾）。
def _boom(*a, **kw):
    raise RuntimeError("context_token 过期了")


def test_chat_switch_parse():
    p = wechat_bridge.parse_chat_switch
    for s in ("开始对话", "开启对话", "进入对话模式", "开始聊天", "持续对话",
              "/on", "/chat", "帮我开始对话", "请进入对话模式", "那你先开始对话",
              "开始对话。", "开始对话！", " 开始对话 ", "/ON", "开始对话吧",
              # 「会话」和「对话」是一回事，手滑打成哪个都得认（2026-08-30 真机上踩到）
              "开始会话", "开启会话", "进入会话", "进入会话模式", "开始会话模式",
              "持续会话", "/开始会话", "帮我开始会话", "开始会话吧", "开始会话。",
              # 2026-09 换成「动词×名词」组合之后，这一批原来全被静默丢掉的写法都认了：
              # 「聊天」漏了「模式」那一格，「打开」这个动词整个没收
              "进入聊天模式", "开始聊天模式", "开启聊天模式",
              "打开对话", "打开聊天", "打开会话",
              "打开对话模式", "打开聊天模式", "打开会话模式", "免前缀模式"):
        check("认出开启：%s" % s, p(s) == "on", p(s))
    for s in ("结束对话", "停止对话", "退出对话模式", "关闭聊天", "/off", "/end",
              "麻烦结束对话", "结束对话，谢谢", "结束对话吧", "那你先结束对话",
              "结束会话", "停止会话", "退出会话", "关闭会话", "退出会话模式",
              "/结束会话", "麻烦结束会话", "结束会话吧",
              # 同理：「聊天模式」这四个字以前一条都不认
              "结束聊天模式", "停止聊天模式", "退出聊天模式", "关闭聊天模式"):
        check("认出关闭：%s" % s, p(s) == "off", p(s))
    # 宁可漏认不误认：下面这些都该当普通消息处理
    for s in ("", "开始", "停止", "继续", "开始播放", "对话", "聊天", "会话",
              "帮我想想怎么开始对话比较自然", "把开始对话这四个字写进 README",
              "开始对话之前先看一下昨天的日志在哪", "结束对话框的样式改一下",
              "结束会话框的样式改一下", "开始会话之前先看一下日志",
              # 剥两头客套之后仍然要跟组合全等，所以这些剥不出开关来
              "开始对话了吗", "结束对话吧我累了", "那就先这样吧", "谢谢",
              # 组合化新加的边界：动词对了、名词不对，一律不认。这些以前就落回 Claude，
              # 以后也不许被吞——尤其「打开微信」必须继续归开应用那条通道
              "打开微信", "打开文件", "打开聊天窗口", "打开对话列表",
              "开始播放", "进入设置", "退出登录", "关闭模式", "开启模式"):
        check("不误认：%r" % s, p(s) is None, p(s))
    check("超长的一律不当开关", p("开始对话" + "啊" * 30) is None)
    check("会话那份也一样有长度上限", p("开始会话" + "啊" * 30) is None)


def test_chat_mode_chain(tmpdir, fake_claude):
    b = make_bridge(tmpdir, fake_claude)
    b.chat_idle_seconds = 1800

    drain(b, [wecom_msg("现在几点", "cm0")])
    check("默认还是要带前缀", not texts(), texts())

    drain(b, [wecom_msg("开始对话", "cm1")])
    t = texts()
    check("「开始对话」不用带前缀就能开", any("对话模式" in x for x in t), t)
    check("开启时说清了怎么退出", any("结束对话" in x for x in t), t)
    check("开启时说了空闲多久自动退出", any("30 分钟" in x for x in t), t)
    # 2026-09：那条回话改成一份「照着说就能用」的常用功能清单（用户要的）
    check("开启时给了一份常用功能清单",
          any("打开微信" in x and "发给我" in x and "进会" in x for x in t), t)
    check("清单里指了 /help 看全部", any("/help" in x for x in t), t)
    # 关机是不可逆的，而对话模式免前缀就生效——这份「随便试」的清单里不该出现它
    check("清单里不列关机", not any("关机" in x for x in t), t)
    check("状态记住了", USER in b._chat, b._chat)

    drain(b, [wecom_msg("现在几点", "cm2")])
    check("对话模式下裸消息被执行", any("已处理" in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 现在几点", "cm3")])
    check("对话模式下带前缀照样能用", any("已处理" in x for x in texts()), texts())

    drain(b, [wecom_msg("开始对话", "cm4")])
    check("重复开启不报错也不重置", any("已经在对话模式" in x for x in texts()), texts())

    drain(b, [wecom_msg("结束对话", "cm5")])
    check("「结束对话」退出", any("退出对话模式" in x for x in texts()), texts())
    check("状态清了", USER not in b._chat, b._chat)

    drain(b, [wecom_msg("现在几点", "cm6")])
    check("退出后裸消息又不执行了", not texts(), texts())

    drain(b, [wecom_msg("结束对话", "cm7")])
    check("没开着的时候关一下会说明", any("本来就不在" in x for x in texts()), texts())


def test_chat_mode_expiry(tmpdir, fake_claude):
    b = make_bridge(tmpdir, fake_claude)
    b.chat_idle_seconds = 1800

    drain(b, [wecom_msg("开始对话", "ex1")])
    b._chat[USER] = time.time() - 60          # 才过一分钟
    drain(b, [wecom_msg("现在几点", "ex2")])
    check("没到点不退出", any("已处理" in x for x in texts()), texts())
    check("说了话就续期", time.time() - b._chat[USER] < 30, b._chat)

    # 主动通知：轮询循环里发现超时，不等下一句话就先说一声
    b._chat[USER] = time.time() - 3600
    FakeILink.sent = []
    b._expire_chat_modes()
    t = texts()
    check("超时后自动退出", USER not in b._chat, b._chat)
    check("主动发了一条告知", any("自动退出" in x for x in t), t)
    check("告知里写了怎么再开", any("开始对话" in x for x in t), t)
    check("推成功了就不用再补", USER not in b._chat_expired, b._chat_expired)

    drain(b, [wecom_msg("现在几点", "ex3")])
    check("退出之后裸消息不执行", not any("已处理" in x for x in texts()), texts())

    # 主动推失败（context_token 过期是常态）：标记留着，下一条消息里补一句
    drain(b, [wecom_msg("开始对话", "ex4")])
    b._chat[USER] = time.time() - 3600
    real_send, b.client.send_text = b.client.send_text, _boom
    try:
        b._expire_chat_modes()
    finally:
        b.client.send_text = real_send
    check("推不出去也照样退出", USER not in b._chat, b._chat)
    check("推失败就留着下次补", USER in b._chat_expired, b._chat_expired)
    drain(b, [wecom_msg("现在几点", "ex5")])
    t = texts()
    check("下一条裸消息里补上说明", any("没执行" in x for x in t), t)
    check("补完就不再啰嗦", USER not in b._chat_expired, b._chat_expired)
    check("补说明的那条不执行", not any("已处理" in x for x in t), t)

    drain(b, [wecom_msg("现在几点", "ex6")])
    check("再发一条就只是静默跳过", not texts(), texts())

    # idle_minutes = 0 就是永不自动退出
    b.chat_idle_seconds = 0
    drain(b, [wecom_msg("开始对话", "ex7")])
    check("关掉自动退出时不提超时", not any("分钟" in x for x in texts()), texts())
    b._chat[USER] = time.time() - 86400
    b._expire_chat_modes()
    check("设成 0 就一直开着", USER in b._chat, b._chat)


def test_no_prefix_hint(tmpdir, fake_claude):
    """不带前缀的消息不能无声无息地丢掉——那和桥挂了看着一模一样（2026-08-30 真机上
    「开始会话」打错一个字，人在那头白等）。但也不能每条都提示，手机上一串闲聊会被刷屏。
    """
    check("默认就开着提示", wechat_bridge.DEFAULTS["no_prefix_hint_minutes"] > 0,
          wechat_bridge.DEFAULTS["no_prefix_hint_minutes"])

    b = make_bridge(tmpdir, fake_claude)
    b.no_prefix_hint_seconds = 600

    drain(b, [wecom_msg("现在几点", "h1")])
    t = texts()
    check("裸消息回一句提示", any("没执行" in x for x in t), t)
    check("提示里说了要带什么", any(b.prefix in x for x in t), t)
    check("提示里说了怎么免前缀", any("开始对话" in x for x in t), t)
    check("提示那条不花额度", not any("已处理" in x for x in t), t)

    drain(b, [wecom_msg("那再看看", "h2")])
    check("紧接着的第二条不再提示", not texts(), texts())

    b._hint_at[USER] = time.time() - 601
    drain(b, [wecom_msg("还在吗", "h3")])
    check("过了间隔又提示一次", any("没执行" in x for x in texts()), texts())

    # 提示里绝不回显原文：发错地方的口令不该被再抄一遍
    b._hint_at[USER] = 0
    secret = "kouling-9527-xyz"
    drain(b, [wecom_msg(secret, "h4")])
    check("提示里不带原文", not any(secret in x for x in texts()), texts())

    drain(b, [wecom_msg("/cc 干活", "h5")])
    check("带前缀的照旧执行", any("已处理" in x for x in texts()), texts())

    b._hint_at.pop(USER, None)
    drain(b, [wecom_msg("开始对话", "h6")])
    drain(b, [wecom_msg("现在几点", "h7")])
    t = texts()
    check("对话模式里照旧执行，不提示",
          any("已处理" in x for x in t) and not any("没执行" in x for x in t), t)

    # 自动退出的那条说明本身就把话说清了，不该紧接着再提示一遍
    b2 = make_bridge(tmpdir, fake_claude)
    b2.no_prefix_hint_seconds = 600
    b2.chat_idle_seconds = 1800
    drain(b2, [wecom_msg("开始对话", "h8")])
    b2._chat[USER] = time.time() - 3600
    FakeILink.sent = []
    b2._expire_chat_modes()
    check("自动退出主动推了一条", any("自动退出" in x for x in texts()), texts())
    drain(b2, [wecom_msg("现在几点", "h9")])
    check("刚说过就不再提示一遍", not texts(), texts())

    b3 = make_bridge(tmpdir, fake_claude)      # make_bridge 里配的就是 0
    check("配 0 就是关掉", b3.no_prefix_hint_seconds == 0, b3.no_prefix_hint_seconds)
    drain(b3, [wecom_msg("现在几点", "h10")])
    check("关掉后完全静默", not texts(), texts())


def test_chat_mode_off(tmpdir, fake_claude):
    b = make_bridge(tmpdir, fake_claude)
    b.chat_mode_enable = False
    drain(b, [wecom_msg("开始对话", "no1")])
    t = texts()
    check("功能关掉时直说，不假装开了", any("没启用" in x for x in t), t)
    check("功能关掉时不进对话模式", USER not in b._chat, b._chat)
    drain(b, [wecom_msg("现在几点", "no2")])
    check("功能关掉后裸消息照旧跳过", not texts(), texts())
