# -*- coding: utf-8 -*-
"""`/parse`（parse_probe）与它「顺序和真桥一致」这件事。

这一组的重点是**顺序**：`parse_probe` 里那两张表的先后必须和 `wechat_bridge.handle()`
的分发顺序一致，否则那份报告就成了骗人的——用户照着它去改说法，结果还是不对。

所以后半段不满足于看报告文本，而是**拿真桥的效果反着验**：
「关闭电脑」必须真走电源（而不是去关一个叫「电脑」的程序）、「打开聊天」必须在免前缀
那条链里就被对话模式接走（而不是落到开应用）。这两条恰好也是 `handle()` 的注释里
点名说「顺序不能动」的地方。

顺带说明为什么这组只能验「顺序」，不能验「报告里每句话都准」：`/parse` 只调纯解析函数，
它判不了「桌面上到底有没有那个图标」「文件到底在不在」——那两条在报告里已经写明是例外。
"""

import parse_probe
import power

from tests.selftest_base import (USER, check, drain, make_bridge, make_launcher,
                           texts, wecom_msg)


def _report(body, state=None):
    return parse_probe.explain(body, state=state)


def test_report_shape():
    """报告本身：句式认不认得、每句话有没有落到对的通道上。

    这一组**不接桥**（不传 apps/files 回调），所以开关应用和发文件那两条只能是「软认」——
    解析通过、但「桌面上有没有图标」「文件在不在」查不了。接了桥的那种在
    test_order_matches_bridge 里验。
    """
    r = _report("关闭电脑")
    check("报告第一行是标题", r.startswith("🔍"), r[:40])
    check("报告分了两条链", "免前缀那条链" in r and "要带 /cc 的那条链" in r, r)
    check("「关闭电脑」在电源那行认了", "电源（锁屏/关机）：认（关机）" in r, r)
    check("同一条也被开关应用解析成「关闭 电脑」（两条都列出来，所以顺序就是答案）",
          "开关应用/播放键：软认（解析成关闭「电脑」" in r, r)
    check("报告解释了「软认」还差哪一步", "标着「软认」的还差一步" in r, r)
    check("全不认的那些标成「不认」", "进会：不认" in r, r)

    check("「关闭微信」电源那行不认（剩字不空）",
          "电源（锁屏/关机）：不认" in _report("关闭微信"), _report("关闭微信"))

    check("报告末尾说明「附件不走这条路」",
          "你发来的图片和文件不走这里" in _report("关闭电脑"), _report("关闭电脑")[-120:])
    check("也说明附件那半边还没做配额和删除",
          "还没做总配额和删除" in _report("关闭电脑"), "没有")

    # 收件那句「要腾地方吗」的答复（2026-09-27 加的）：只在词上认，真删另有前置条件
    check("报告里有「回收件的要腾地方吗」这一行",
          "回收件的「要腾地方吗」" in _report("删掉"), "没有")
    check("「删掉」在那行认成清最老的",
          "认（回「删掉」：把最老的那几个清掉）" in _report("删掉"), _report("删掉"))
    check("「不删」也在那行认",
          "认（回「不删」：这条不收）" in _report("不删"), _report("不删"))
    check("「确认」**不算**收件那句答复（一个字不能三头抢）",
          "回收件的「要腾地方吗」：不认" in _report("确认"), _report("确认"))
    check("「把这个文件删掉」不算答复（那是让我去干活）",
          "回收件的「要腾地方吗」：不认" in _report("把这个文件删掉"),
          _report("把这个文件删掉"))
    # 顺手钉一个老 bug：以前 `/parse 确认` 会 TypeError（默认值是先求值的）
    check("「确认」那行报告得出来（不再 NoneType + int 崩）",
          "回待确认的点击：认（回那次待确认的点击：确认）" in _report("确认"),
          _report("确认"))
    # 用「20 分钟后」而不是钟点：钟点跟着墙上时钟走，跑到晚上就变成「今天已经过去了」
    r2 = _report("20分钟后进会 413231713")
    check("「20分钟后进会 413231713」判成定时进会", "进会：认（定时" in r2, r2)
    # ---- 定时任务那一行（2026-09-27 加的）----
    check("「明天九点帮我跑备份」走定时任务那条",
          "定时跑一句话：认（明天 09:00 把那句「备份」交给 Claude 跑，会花额度）"
          in _report("明天九点帮我跑备份"), _report("明天九点帮我跑备份"))
    check("「提醒我」那句不会被定时任务那条认走",
          "定时跑一句话：不认" in _report("明天九点提醒我交房租"),
          _report("明天九点提醒我交房租"))
    check("报告里定时任务那行排在提醒后面、音量前面（顺序照 _dispatch_gated）",
          0 <= _report("明天九点帮我跑备份").find("提醒：")
          < _report("明天九点帮我跑备份").find("定时跑一句话")
          < _report("明天九点帮我跑备份").find("音量（调到/多少）"))
    check("「音量调到30%」走音量那条",
          "音量（调到/多少）：认（把系统音量设到 30%）" in _report("音量调到30%"),
          _report("音量调到30%"))
    check("「下一首」判成按键（不是去开应用）",
          "开关应用/播放键：认（按下一首键）" in _report("下一首"), _report("下一首"))
    # ---- 屏幕亮度那一行（2026-09-27 加的）----
    check("「亮度调到50%」走亮度那条",
          "屏幕亮度（调到/多少）：认（把屏幕亮度设到 50%）" in _report("亮度调到50%"),
          _report("亮度调到50%"))
    check("「屏幕亮一点」也走亮度那条",
          "屏幕亮度（调到/多少）：认（屏幕亮度调亮一次）" in _report("屏幕亮一点"),
          _report("屏幕亮一点"))
    check("「音量调到30%」不会被亮度那条认走",
          "屏幕亮度（调到/多少）：不认" in _report("音量调到30%"),
          _report("音量调到30%"))
    check("报告里音量那行排在亮度前面（顺序就是答案）",
          0 <= _report("音量调到30%").find("音量（调到/多少）")
          < _report("音量调到30%").find("屏幕亮度（调到/多少）"))
    check("报告里亮度那行排在电源前面（顺序照 _dispatch_gated）",
          0 <= _report("亮度调到50%").find("屏幕亮度（调到/多少）")
          < _report("亮度调到50%").find("电源（锁屏/关机）"))
    check("「发给我 D:\\a\\b.pptx」走发文件（没接桥所以是软认）",
          "发文件：软认（路径对：" in _report("发给我 D:\\a\\b.pptx"),
          _report("发给我 D:\\a\\b.pptx"))
    check("「点一下开始播放」走点击",
          "点一下：认（" in _report("点一下开始播放"), _report("点一下开始播放"))
    check("「结束对话框的样式改一下」在对话模式那行一个字都没认出来",
          "对话模式开关：不认" in _report("结束对话框的样式改一下"),
          _report("结束对话框的样式改一下"))

    check("拷出来不像路径时说明白",
          "不像一条路径" in _report("发给我 汇报.pptx"), _report("发给我 汇报.pptx"))
    check("空参数给用法，不硬猜",
          "把你要问的那句话写在后面" in _report(""), _report(""))
    check("状态会打在最上面", "状态：" in _report("锁屏", state=["在对话模式里"]), "missing")


def test_command_shape(tmpdir, fake_claude):
    """`/parse` 本身的句式：**不带前缀也认**，而且不许吃掉别的消息。"""
    b = make_bridge(tmpdir, fake_claude)
    check("/parse 单独发给用法", "把你要问的那句话写在后面" in b._parse_probe("/parse", USER))
    check("带空格的那种认", "这句话会怎么走" in b._parse_probe("/parse 关闭电脑", USER))
    check("中间不空格也认（手机上打中文常常连着写）",
          "这句话会怎么走" in b._parse_probe("/parse关闭电脑", USER))
    check("/解析 这个中文写法也认", "这句话会怎么走" in b._parse_probe("/解析 锁屏", USER))
    check("/why 也认", "这句话会怎么走" in b._parse_probe("/why 锁屏", USER))

    for bad in ("/parser 什么", "/parseX", "parse 打开微信", "/parse/tmp/x"):
        check("不该被当成 /parse：%s" % bad, b._parse_probe(bad, USER) is None)

    # 端到端：不带 /cc 就能用，而且一分额度都不花
    drain(b, [wecom_msg("/parse 关闭电脑", "pp1")])
    t = texts()
    check("/parse 不需要前缀", any("这句话会怎么走" in x for x in t), t)
    check("/parse 不花额度（没去找 Claude）", not any("已处理" in x for x in t), t)


def test_order_matches_bridge(tmpdir, fake_claude):
    """**顺序钉死**：报告里第一条「认」的那条，必须真是接走它的那条。

    这两条正是 `handle()` 注释里点名「顺序不能动」的地方——报告和真桥谁都别想单独走偏。
    """
    # ---- 「关闭电脑」归电源，不归开关应用 ----
    b = make_bridge(tmpdir, fake_claude)
    lc, _desktop, opened, killed, _shell = make_launcher(tmpdir)
    b.launcher = lc
    calls = []

    def fake_machine(action, arg=0):
        calls.append(action)
        return {"ok": True, "locked": False, "aborted": True}

    b.power = power.PowerControl(runner=fake_machine)

    # 接了桥之后的报告才是**确定结论**（能查桌面图标和文件了）
    r = b._parse_probe("/parse 关闭电脑", USER)
    check("接了桥之后，报告说电源那条会接走它",
          "电源（锁屏/关机）：认（关机）" in r, r)
    check("报告里电源那一行排在开关应用前面（顺序就是答案）",
          0 <= r.find("电源（锁屏/关机）") < r.find("开关应用/播放键"), r)
    check("开关应用那条给了确定结论：桌面上没有「电脑」这个图标 → 落回 Claude",
          "开关应用/播放键：落回 Claude" in r, r)

    drain(b, [wecom_msg("/cc 关闭电脑", "or1")])
    check("真桥也走了电源（安排了一次关机）", calls == ["shutdown"], calls)
    check("真桥没有去关一个叫「电脑」的程序", killed == [] and opened == [], (killed, opened))
    check("这种情况下不花额度", not any("已处理" in x for x in texts()), texts())

    # ---- 「打开聊天」在免前缀那条链里就被对话模式接走，轮不到开应用 ----
    b2 = make_bridge(tmpdir, fake_claude)
    lc2, _d2, opened2, _k2, _s2 = make_launcher(tmpdir)
    b2.launcher = lc2
    r2 = b2._parse_probe("/parse 打开聊天", USER)
    check("报告把「打开聊天」判成进对话模式",
          "对话模式开关：认（进对话模式）" in r2, r2)
    check("报告里对话模式那一行在开关应用之前（免前缀链先跑）",
          0 <= r2.find("对话模式开关") < r2.find("开关应用/播放键"), r2)

    drain(b2, [wecom_msg("/cc 打开聊天", "or2")])
    check("真桥进了对话模式", USER in b2._chat, b2._chat)
    check("真桥没去启动任何程序", opened2 == [], opened2)
    check("这一条也不花额度", not any("已处理" in x for x in texts()), texts())

    # ---- 接了桥之后：桌面上真有的应用，报告会给出确定结论 ----
    r3 = b2._parse_probe("/parse 打开微信", USER)
    check("桌面真有的应用报成「认（打开「微信」）」", "开关应用/播放键：认（打开「微信」）" in r3, r3)
    r4 = b2._parse_probe("/parse 发给我 " + tmpdir + "\\没有这个文件.pptx", USER)
    check("文件不在时报成落回 Claude", "发文件：落回 Claude" in r4, r4)

    # ---- 反面：全都不认的照旧交给 Claude ----
    b3 = make_bridge(tmpdir, fake_claude)
    drain(b3, [wecom_msg("/cc 结束对话框的样式改一下", "or3")])
    check("真桥把它交给了 Claude", any("已处理" in x for x in texts()), texts())
