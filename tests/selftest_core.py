# -*- coding: utf-8 -*-
"""解析与分片、权限边界、全链路、规则、并发、键名兜底、发送写法探测。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。

这几组是「桥还活着」的最小证明：字段怎么认、长回复怎么切、白名单和前缀拦不拦、
一条慢活会不会把轮询堵死、服务端换个键名或者拒掉 account_id 时还能不能发出去。
"""

import os
import sys
import time

import ilink_client
import wechat_bridge
from claude_runner import ClaudeRunner
from ilink_client import Message, split_text

from tests.selftest_base import (ACCOUNT, BASE, OTHER, USER, FakeILink, check, drain,
                           make_bridge, texts, wecom_msg)


def test_parsing():
    m = Message(wecom_msg("", "m0", items=[
        {"type": 1, "text_item": {"text": "/cc 看下这个目录"}},
        {"type": 2},
        {"type": 6, "voice_item": {"text": "语音转写出来的内容"}},
    ], ref_msg={"item_list": [{"type": 1, "text_item": {"text": "被引用的原话"}}]}))
    check("文本 item 解析", "看下这个目录" in m.text)
    check("语音自带转写被当文字用", "语音转写出来的内容" in m.text)
    check("图片被记成附件", m.attachments == ["图片"], m.attachments)
    check("引用消息被取出", m.quoted == "被引用的原话", m.quoted)

    alias = Message({"msgid": "x1", "sender_id": "u9",
                     "item_list": [{"type": 1, "text": "hi"}]})
    check("字段别名兜底", (alias.message_id, alias.from_user_id, alias.text) == ("x1", "u9", "hi"),
          (alias.message_id, alias.from_user_id, alias.text))


def test_split():
    src = "\n".join("第%d行%s" % (i, "字" * 200) for i in range(20))
    chunks = split_text(src, 1800)
    check("长文本被分片", len(chunks) > 1, len(chunks))
    check("每片不超上限", all(len(c) <= 1800 for c in chunks))
    check("分片不丢字", "".join(chunks).replace("\n", "") == src.replace("\n", ""))
    check("短文本不分片", split_text("短消息") == ["短消息"])
    check("超长单行也能切", len(split_text("x" * 5000, 1800)) == 3)

    # 单行超长要往回收（2026-09 加的）：一句没有换行的长中文，不许从句子中间劈开。
    # 原来是无条件 line[:limit]，切出来的半句话在手机上很难读，长 URL 更是直接点不开。
    prose = "这是一句比较长的中文，用来测分片会不会从中间劈开。" * 200
    one = split_text(prose, 1800)
    check("超长中文行照样被分成多片", len(one) > 1, len(one))
    check("每片都在标点后面收尾（不是从句子中间劈开）",
          bool(one) and all(c.endswith(("。", "，")) for c in one[:-1]), one[:1])
    check("往回收之后仍然不丢字、不超上限",
          all(len(c) <= 1800 for c in one) and "".join(one) == prose, len(one))
    check("一个标点都没有时退回硬切，不会卡死",
          len(split_text("x" * 5400, 1800)) == 3)


def test_guards(tmpdir):
    for bad in (["--dangerously-skip-permissions"],
                ["--permission-mode", "bypassPermissions"]):
        try:
            ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir, extra_args=bad,
                         session_file=os.path.join(tmpdir, "s.json"))
            check("拒绝危险参数 %s" % bad[-1], False, "居然放行了")
        except SystemExit:
            check("拒绝危险参数 %s" % bad[-1], True)

    root = os.path.abspath(os.sep)
    try:
        wechat_bridge.resolve_workdir({"workdir": root})
        check("拒绝把 workdir 设成根目录", False, "居然放行了")
    except SystemExit:
        check("拒绝把 workdir 设成根目录", True)
    try:
        wechat_bridge.resolve_workdir({"workdir": "~"})
        check("拒绝把 workdir 设成用户主目录", False, "居然放行了")
    except SystemExit:
        check("拒绝把 workdir 设成用户主目录", True)
    check("正常 workdir 放行", wechat_bridge.resolve_workdir({"workdir": tmpdir}) ==
          os.path.abspath(tmpdir))

    # --add-dir 可能是「能收多个值」的参数，prompt 紧跟在它后面就会被当成又一个目录
    r = ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir,
                     extra_args=["--add-dir", "D:\\", "--permission-mode", "acceptEdits"],
                     session_file=os.path.join(tmpdir, "s2.json"))
    cmd = r._build_cmd(None, "帮我看看这个目录")
    check("prompt 是最后一个参数", cmd[-1] == "帮我看看这个目录", cmd[-3:])
    check("prompt 前面隔着一个别的参数，不会被 --add-dir 吃掉",
          cmd[-3:-1] == ["--output-format", "json"], cmd[-4:])
    check("--add-dir 原样透传", "--add-dir" in cmd and cmd[cmd.index("--add-dir") + 1] == "D:\\",
          cmd)


def test_chain_basic(bridge, tmpdir):
    drain(bridge, [wecom_msg("/cc 看下这个目录", "m1")])
    t = texts()
    check("带前缀的消息被执行", any("已处理" in x for x in t), t)
    check("prompt 前缀被套上", any("PFX:" in x for x in t), t)
    first = FakeILink.sent[0]
    check("回带 context_token", first.get("context_token") == "ctx-1", first.get("context_token"))
    check("发消息字段正确", (first.get("account_id"), first.get("message_type"),
                        first.get("message_state")) == (ACCOUNT, 2, 2), first)
    check("client_id 是随机数", str(first.get("client_id", "")).isdigit(), first.get("client_id"))
    check("默认就用真机认的「包一层 msg」写法", FakeILink.wrapped and FakeILink.wrapped[0],
          FakeILink.wrapped)
    check("这套写法被记住并落盘",
          os.path.exists(bridge.client.style_file) and bridge.client.send_style == "包一层 msg",
          bridge.client.send_style)
    check("记住了 session", bridge.runner._sessions.get(USER) == "sess-abc",
          bridge.runner._sessions)

    check("getconfig 带上了 ilink_user_id",
          any(p.get("ilink_user_id") == USER for p in FakeILink.configs), FakeILink.configs)

    h = FakeILink.headers_seen[-1]
    check("Authorization 头正确", h.get("authorization") == "Bearer tok", h.get("authorization"))
    check("AuthorizationType 头正确", h.get("authorizationtype") == "ilink_bot_token")
    check("iLink-App-Id 头正确", h.get("ilink-app-id") == "bot")
    check("带了 X-WECHAT-UIN", bool(h.get("x-wechat-uin")))
    check("body 里注入了 channel_version",
          any((bi or {}).get("channel_version") == "2.1.3" for bi in FakeILink.base_infos))

    with open(bridge.client.cursor_file, "r", encoding="utf-8") as fh:
        on_disk = fh.read().strip()
    check("游标推进了", bridge.client.cursor.startswith("cursor-"), bridge.client.cursor)
    check("游标落盘了", on_disk == bridge.client.cursor, on_disk)

    st = [p.get("status") for p in FakeILink.typing]
    check("typing 开了又关", st and st[0] == 1 and st[-1] == 0, st)


def test_chain_rules(bridge, tmpdir, fake_claude):
    drain(bridge, [wecom_msg("/cc 看下这个目录", "m1")])
    check("重复 message_id 被忽略", texts() == [], texts())

    drain(bridge, [wecom_msg("看下这个目录", "m2")])
    check("没带前缀不执行", texts() == [], texts())

    drain(bridge, [wecom_msg("/cc rm 点什么", "m3", user=OTHER)])
    check("非白名单用户被静默丢弃", texts() == [], texts())

    drain(bridge, [wecom_msg("", "m4", items=[{"type": 2}])])
    check("纯图片消息给出提示", any("只认文字" in x for x in texts()), texts())

    drain(bridge, [wecom_msg("/cc 回声", "m5", message_type=2)])
    check("自己发的消息不触发", texts() == [], texts())

    drain(bridge, [wecom_msg("/help", "m6")])
    check("/help 不需要前缀", any("可用指令" in x for x in texts()), texts())

    drain(bridge, [wecom_msg("/id", "m7")])
    check("/id 回用户 ID", any(USER in x for x in texts()), texts())

    drain(bridge, [wecom_msg("/where", "m8")])
    check("/where 回工作目录", any(os.path.abspath(tmpdir) in x for x in texts()), texts())

    had = bridge.runner._sessions.get(USER)
    check("/new 之前确实存着 session", had == "sess-abc", bridge.runner._sessions)
    drain(bridge, [wecom_msg("/new", "m9")])
    check("/new 清掉了 session", USER not in bridge.runner._sessions, bridge.runner._sessions)
    check("/new 有回执", any("上下文" in x for x in texts()), texts())

    drain(bridge, [wecom_msg("/cc LONG", "m10")])
    check("长回复被分成多条", len(FakeILink.sent) > 1, len(FakeILink.sent))
    check("分片带「（1/N）」标号，不是日志味的 [1/N]",
          any(x.startswith("（1/") and not x.startswith("[1/") for x in texts()),
          texts()[:1])
    check("最后一片标了「完」，让人知道长回复发完了",
          texts()[-1].split("\n")[0].endswith("· 完）"), texts()[-1:])

    empty = make_bridge(tmpdir, fake_claude, allowed=())
    drain(empty, [wecom_msg("/cc 干点什么", "m11")])
    t = texts()
    check("白名单为空时不执行", not any("已处理" in x for x in t), t)
    check("白名单为空时告知用户 ID", any(USER in x for x in t), t)


def test_nonblocking(bridge):
    FakeILink.sent = []
    FakeILink.inbox = [wecom_msg("/cc SLEEP 慢活", "s1")]
    bridge.poll_once()
    FakeILink.inbox = [wecom_msg("/cc 第二条", "s2")]
    t0 = time.time()
    bridge.poll_once()
    dt = time.time() - t0
    check("长任务跑着也能继续收消息", dt < 1.0, "第二次轮询耗时 %.2fs" % dt)
    check("此时前一个任务确实没跑完", bridge.queue.unfinished_tasks >= 1)
    bridge.queue.join()
    time.sleep(0.3)
    check("两条都出了结果", sum(1 for x in texts() if "已处理" in x) == 2, texts())


def test_unknown_msg_key(tmpdir, fake_claude):
    """服务端换了消息列表的键名时，游标照样正常推进——看日志只会觉得"一切正常但收不到"。

    所以按形状兜底找一遍。这是真机上"窗口没动静"最难查的一种情况。
    """
    obj = {"ret": 0, "get_updates_buf": "c1",
           "data": {"inbox": [wecom_msg("/cc 换了键名", "k1")]}}
    hits = ilink_client._scan_for_msg_list(obj)
    check("能按形状找到改名后的消息列表", hits and hits[0][0] == "data.inbox", hits)
    check("不把随便一个字典列表当消息",
          not ilink_client._scan_for_msg_list({"a": [{"x": 1}, {"y": 2}]}))

    b = make_bridge(tmpdir, fake_claude)
    FakeILink.msg_key = "inbox_list"                 # 别名列表里没有这个名字
    try:
        drain(b, [wecom_msg("/cc 键名变了也要能收", "k2")])
        check("键名变了整条链路仍然通", any("已处理" in x for x in texts()), texts())
    finally:
        FakeILink.msg_key = "message_list"


def test_send_probe(tmpdir, fake_claude):
    """真机上 sendmessage 一路 errcode=-2，所以要能自己换写法试出来、并且记住。"""
    b = make_bridge(tmpdir, fake_claude)
    FakeILink.reject_account_id = True               # 前两套都带 account_id，必然被拒
    try:
        drain(b, [wecom_msg("/cc 换个写法试试", "p1")])
        check("被拒之后自动换写法发成功", any("已处理" in x for x in texts()), texts())
        check("记住了服务端认的写法", b.client.send_style == "去掉 account_id",
              b.client.send_style)
        check("最终发出的 payload 里没有 account_id",
              FakeILink.sent and all("account_id" not in p for p in FakeILink.sent),
              FakeILink.sent[:1])
        drain(b, [wecom_msg("/cc 第二条", "p2")])
        check("第二条直接用记住的写法", b.client.send_style == "去掉 account_id",
              b.client.send_style)

        # 重启后不该再白挨一轮 invalid arguments
        c = b.client.__class__(base_url=BASE, token="tok", account_id=ACCOUNT,
                               state_dir=tmpdir)
        check("重启后从盘上读回写法", c.send_style == "去掉 account_id", c.send_style)

        # 记住的写法失效时要能重新试一轮，而不是从此发不出消息
        c.send_style = "完整"                         # 这套带 account_id，必然被拒
        FakeILink.sent = []
        c.send_chunk(USER, "记住的写法失效了也要发出去")
        check("记住的写法失效后重新试通", len(FakeILink.sent) == 1, FakeILink.sent)
        check("失效后换到了能用的那套", c.send_style == "去掉 account_id", c.send_style)
    finally:
        FakeILink.reject_account_id = False
