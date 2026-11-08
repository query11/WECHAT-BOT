# -*- coding: utf-8 -*-
"""管理通道（/status /log /reload /queue /stop /stopall）和「叫停」那一套的自测。

分两块：
  · 纯解析（parse_admin / parse_lines）——正反两面都卡住，不许误吞；
  · 走真桥的效果——用假 iLink + 假 claude，电池全拔掉（不真杀进程、不真碰电源）。

**两条绝不能真的发生的事**，这组用例盯着：
  ① `/reload` 不能碰到项目目录里那份真 config.json。所以 `bridge_admin.CONFIG_PATH`
     在每个用例里都指到临时文件，用完还原。
  ② 叫停不能真去 taskkill。`ClaudeRunner._killer` 就是为此留的注入口，这里一律换成假的
     （`kill_tree` 本身的真假两个分支单独测，用的也是假的 run）。
"""
import json
import os
import queue
import threading
import time

import bridge_admin
import claude_runner
import wechat_bridge
from ilink_client import Message

from tests.selftest_base import (USER, FakeILink, check, drain, make_bridge, texts,
                           wecom_msg)


# ---------- 小工具 ----------

def _wait_for(cond, secs=3.0):
    """等一个条件成立。等不到就返回 False，让断言去红——不用 sleep 硬扛。"""
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _cfg(tmpdir, **over):
    """造一份能用的 config.json 写到临时目录，返回路径。

    基线取 wechat_bridge.DEFAULTS，再按用例覆写；所有「会真动手」的通道一律关掉。
    """
    cfg = dict(wechat_bridge.DEFAULTS)
    cfg.update({
        "base_url": "http://127.0.0.1:1", "bot_token": "tok", "account_id": "bot-1",
        "allowed_user_ids": [USER], "workdir": tmpdir, "claude_bin": "claude",
        "claude_extra_args": [], "app_launch": {"enable": False},
        "media_control": {"enable": False}, "screen_control": {"enable": False},
        "power_control": {"enable": False}, "meeting": {"enable": False},
        "remind": {"enable": False},
    })
    cfg.update(over)
    path = os.path.join(tmpdir, "cfg_reload.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return path


class _Reload:
    """把 bridge_admin.CONFIG_PATH 临时指到别处。用完还原，绝不漏。"""

    def __init__(self, path):
        self.path = path
        self.old = None

    def __enter__(self):
        self.old = bridge_admin.CONFIG_PATH
        bridge_admin.CONFIG_PATH = self.path
        return self

    def __exit__(self, *exc):
        bridge_admin.CONFIG_PATH = self.old
        return False


# ---------- 一、纯解析 ----------

def test_admin_parse():
    p = bridge_admin.parse_admin
    # 正面
    for s in ("/status", "/状态", "状态", "桥的状态", "  /status  "):
        check("认出看状态：%r" % s, p(s) == ("status", None), p(s))
    for s in ("/reload", "/重载"):
        check("认出重读配置：%r" % s, p(s) == ("reload", None), p(s))
    for s in ("/queue", "/队列"):
        check("认出看队列：%r" % s, p(s) == ("queue", None), p(s))
    for s in ("/queue clear", "清空队列", "队列清空"):
        check("认出清队列：%r" % s, p(s) == ("queue_clear", None), p(s))
    for s in ("/log", "/日志"):
        check("认出看日志：%r" % s,
              p(s) == ("log", bridge_admin.LOG_DEFAULT_LINES), p(s))
    # 带行数：可以空格，也可以连着写（手机上打中文常连着）
    for s, n in (("/log 50", 50), ("/log50", 50), ("/日志 7", 7), ("/log:12", 12),
                 ("/log，20", 20)):
        check("认出带行数：%r" % s, p(s) == ("log", n), p(s))
    # 行数越界要夹住，别让它去读一百万行
    check("/log 0 至少读 1 行", p("/log 0") == ("log", 1), p("/log 0"))
    check("/log 999 夹到上限",
          p("/log 999") == ("log", bridge_admin.LOG_MAX_LINES), p("/log 999"))
    # 写得不像话就当没写行数
    for s in ("/log abc", "/log 12abc"):
        check("行数不像话就用默认：%r" % s,
              p(s) == ("log", bridge_admin.LOG_DEFAULT_LINES), p(s))

    # 反面：宁可漏认不误认。这些一个都不许被吞
    for s in ("", "  ", "/statusx", "/logs", "/logx", "日志 50", "看看状态",
              "/queues", "清空", "清空聊天记录", "重新加载", "重启",
              "/stop", "/stopall", "叫停"):
        check("不误认：%r" % s, p(s) is None, p(s))


def test_stop_words():
    """叫停的词表：只收明确的几个，光秃秃的「停」「停止」一律不收。"""
    b = _mk_bare()
    for s in ("/stop", "叫停", "别跑了", "取消任务"):
        check("认得出叫停：%s" % s, b._try_admin(USER, s) is not None)
    for s in ("/stopall", "/stop-all", "全停", "都别干了"):
        check("认得出全停：%s" % s, b._try_admin(USER, s) is not None)
    # 这几个是最要紧的：正常说话里出现得太频繁，误认就是把正经活儿杀掉
    for s in ("停", "停止", "停下", "停一下", "停车位在哪", "停止播放", "先把那个停了"):
        check("不误认（交给后面的通道）：%r" % s, b._try_admin(USER, s) is None)


def _mk_bare():
    """一个不跑 __init__ 的空壳，只为调纯逻辑方法。

    只补这几个方法会碰的属性——不补的话冒出来的是 AttributeError 而不是断言失败，
    那就看不出到底哪儿不对了。
    """
    b = wechat_bridge.Bridge.__new__(wechat_bridge.Bridge)
    b._running = None
    b.queue = queue.Queue()
    b.queue_max = 0
    return b


# ---------- 二、走真桥：/status /queue ----------

def test_status_and_queue(tmpdir, fake_claude):
    b = make_bridge(tmpdir, fake_claude)
    b.log_path = os.path.join(tmpdir, "bridge.log")

    # 什么都没跑的时候
    t = b._try_admin(USER, "/status")
    check("/status 有回话", isinstance(t, str) and t, t)
    check("/status 说清没有在跑的任务", "没有在跑的任务" in t, t)
    check("/status 说了运行了多久", "已运行" in t, t)
    check("/status 报消息条数", "条消息" in t, t)
    check("/status 报对话模式状态", "对话模式：" in t, t)
    check("/status 不用带 /cc 也认（走免费链）", b._try_admin(USER, "状态") is not None)

    # 队列和 /status 说的是同一件事
    q = b._try_admin(USER, "/queue")
    check("/queue 有回话", isinstance(q, str) and q, q)
    check("/queue 说清没有排队的", "没有" in q, q)
    check("/queue 报上限", "20" in q, q)

    # 消息计数：白名单里那位发来的才算
    drain(b, [wecom_msg("现在几点", "st1")])
    check("收到消息会计数", b._msg_count == 1, b._msg_count)
    check("/status 报的是最新条数", "1 条消息" in b._try_admin(USER, "/status"))


def test_queue_and_status_with_work(tmpdir, fake_claude):
    """有活儿在跑、后面还排着的时候，/status 和 /queue 都得如实说。"""
    b = make_bridge(tmpdir, fake_claude)
    gate = threading.Event()
    b.runner.run = lambda uid, prompt: (gate.wait(5.0), "✅ 干完了")[1]

    _send(b, "慢慢来", "q1")
    check("活儿起来了", _wait_for(lambda: b._running is not None))

    st = b._try_admin(USER, "/status")
    check("/status 报了正在跑的那条", "正在跑：" in st, st)
    check("/status 报的是你发的那句话", "慢慢来" in st, st)
    check("/status 报了跑了多久", "跑了" in st, st)

    _send(b, "排队甲", "q2")
    _send(b, "排队乙", "q3")
    check("两条真排进去了", b.queue.qsize() == 2, b.queue.qsize())

    st = b._try_admin(USER, "/status")
    check("/status 报了排队条数", "后面排着 2 条" in st, st)

    q = b._try_admin(USER, "/queue")
    check("/queue 列出了排队的原话", "排队甲" in q and "排队乙" in q, q)
    check("/queue 也报了正在跑的", "慢慢来" in q, q)

    gate.set()
    check("跑完了 _running 会清掉", _wait_for(lambda: b._running is None))

    # 队列清空
    b._running = None
    b.queue = queue.Queue()
    for i in range(3):
        b.queue.put((Message(wecom_msg("占位 %d" % i, "z%d" % i)), "占位", None))
    c = b._try_admin(USER, "/queue clear")
    check("清队列有回执", "清掉" in c and "3 条" in c, c)
    check("清完真的空了", b.queue.qsize() == 0, b.queue.qsize())
    c2 = b._try_admin(USER, "/queue clear")
    check("再清一次说本来就是空的", "本来就是空的" in c2, c2)


def test_queue_max(tmpdir, fake_claude):
    """队列上限：满了直说不接，别无限堆着。"""
    b = make_bridge(tmpdir, fake_claude)
    b.queue_max = 1
    b.queue.maxsize = 1
    gate = threading.Event()
    b.runner.run = lambda uid, prompt: (gate.wait(5.0), "✅ 干完了")[1]

    _send(b, "占住工作者", "mx0")
    check("先占住工作者", _wait_for(lambda: b._running is not None))

    _send(b, "第一条排队", "mx1")
    check("第一条能排进去", b.queue.qsize() == 1, b.queue.qsize())

    _clear()
    _send(b, "第二条该被拒", "mx2")
    t = texts()
    check("满了会回一句「没接」", any("没接" in x for x in t), t)
    check("回话里写清了上限", any("上限 1" in x for x in t), t)
    check("被拒的那条没进队列", b.queue.qsize() == 1, b.queue.qsize())

    gate.set()
    check("收尾", _wait_for(lambda: b._running is None))


def _send(b, text, mid, user=USER, prefixed=True):
    """喂一条消息给桥，**不等它跑完**。

    drain 会 join 队列、等任务真做完，而这里要的正是「工作者还堵着」那个状态，所以
    只用 poll_once。路子和真消息一样：原始 dict 进假服务端，get_updates 那边抹平成
    Message——直接把 dict 喂给 handle() 会炸（它要的是 .message_type 那种属性）。

    默认**补上触发前缀**：这里要验的都是「这条被派下去之后怎么样」，不补的话消息会
    被前缀闸门静默丢掉，表现成「活儿一直起不来」。`user` 用来假装别人也在白名单里
    （测「不许把别人的原话念给你听」那条）。
    """
    body = ((b.prefix + " ") if prefixed else "") + text
    FakeILink.inbox = [wecom_msg(body, mid, user=user)]
    return b.poll_once()


def _clear():
    """清掉假服务端记下的发出记录，好让 texts() 只看这一段。"""
    FakeILink.sent = []


# ---------- 三、/log ----------

def test_log(tmpdir, fake_claude):
    b = make_bridge(tmpdir, fake_claude)
    b.log_path = os.path.join(tmpdir, "sub", "bridge.log")

    t = b._try_admin(USER, "/log")
    check("没有日志文件时直说", "还没有日志文件" in t, t)

    b.log_path = os.path.join(tmpdir, "bridge.log")
    lines = ["第 %d 行，普普通通" % i for i in range(1, 51)]
    with open(b.log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    t = b._try_admin(USER, "/log")
    check("/log 默认给最后 30 行", "最后 30 行" in t, t[:80])
    check("给的是最后那几行", "第 50 行" in t, t[-200:])
    check("没有给最前面那几行", "第 1 行，" not in t, t[:200])

    t = b._try_admin(USER, "/log 3")
    check("/log 3 就 3 行", "最后 3 行" in t, t[:80])
    check("3 行里正好是 48/49/50", "第 48 行" in t and "第 50 行" in t, t)

    # 抹凭据：把 cfg 换成真凭据那种形状（短词会被下限跳过，见 _mask），埋进日志看抹干净没
    b.cfg = dict(b.cfg)
    b.cfg["bot_token"] = "06000000000000000000000000000000000000"
    b.cfg["account_id"] = "synthetic-bot-account"
    with open(b.log_path, "a", encoding="utf-8") as fh:
        fh.write("凭据演练 %s / %s\n" % (b.cfg["bot_token"], b.cfg["account_id"]))
    t = b._try_admin(USER, "/log 1")
    check("日志里的 token 被抹掉", "06000000000000ee" not in t, t)
    check("账号也被抹掉", b.cfg["account_id"] not in t, t)
    check("抹成了 ***", "***" in t, t)

    # 「按值抹」这条要能管住形状不像凭据的账号名——形状兜底认不出它
    b.cfg["account_id"] = "我的账号名压根不像凭据"
    with open(b.log_path, "a", encoding="utf-8") as fh:
        fh.write("又一条 %s\n" % b.cfg["account_id"])
    t = b._try_admin(USER, "/log 1")
    check("不按形状的账号名也按值抹掉", "我的账号名压根不像凭据" not in t, t)

    # 形状兜底：埋一个像 token 的东西，精确值不知道也认得出
    with open(b.log_path, "a", encoding="utf-8") as fh:
        fh.write("看到 0123456789abcdef@im.bot 这种东西\n")
    t = b._try_admin(USER, "/log 1")
    check("形状像 account_id 的也抹掉", "0123456789abcdef" not in t, t)


# ---------- 四、/reload ----------

def test_reload_ok(tmpdir, fake_claude):
    b = make_bridge(tmpdir, fake_claude)
    path = _cfg(tmpdir, prefix="/zz")

    with _Reload(path):
        t = b._try_admin(USER, "/reload")
    check("/reload 回了成功", t.startswith("✅"), t)
    check("说了进程没重启", "没重启" in t, t)
    check("前缀真的换了", b.prefix == "/zz", b.prefix)
    check("顺带报了改过哪些键", "prefix" in t, t)

    # 数值型的也要真的生效
    path2 = _cfg(tmpdir, prefix="/zz", max_reply_chars=999, queue_max=7)
    with _Reload(path2):
        b._try_admin(USER, "/reload")
    check("回复上限换上了", b.client.max_reply_chars == 999, b.client.max_reply_chars)
    check("队列上限换上了", b.queue_max == 7, b.queue_max)
    check("队列对象的上限也换了", b.queue.maxsize == 7, b.queue.maxsize)


def test_reload_refuses_bad_config(tmpdir, fake_claude):
    """写坏的配置一律**原样保留旧的**，桥照旧能干活——这是 /reload 的命根子。"""
    b = make_bridge(tmpdir, fake_claude)
    b.prefix = "/cc"
    b.queue_max = 20

    # ① JSON 写坏了：load_config 拿 SystemExit 报错，不是 Exception
    bad = os.path.join(tmpdir, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    with _Reload(bad):
        t = b._try_admin(USER, "/reload")
    check("JSON 坏了会拒绝", t.startswith("❌"), t)
    check("说了旧配置没动", "旧配置留着没动" in t, t)
    check("前缀没被改坏", b.prefix == "/cc", b.prefix)

    # ② 文件没了
    with _Reload(os.path.join(tmpdir, "nope.json")):
        t = b._try_admin(USER, "/reload")
    check("文件没了会拒绝", t.startswith("❌"), t)

    # ③ workdir 不存在：resolve_workdir 抛 SystemExit
    with _Reload(_cfg(tmpdir, workdir=os.path.join(tmpdir, "没有这个目录"))):
        t = b._try_admin(USER, "/reload")
    check("workdir 没了会拒绝", t.startswith("❌"), t)
    check("拒绝时也说旧配置没动", "旧配置留着没动" in t, t)
    check("workdir 没被改坏", b.workdir == os.path.abspath(tmpdir), b.workdir)

    # ④ claude_extra_args 里有被禁的开关：ClaudeRunner 抛 SystemExit
    with _Reload(_cfg(tmpdir, claude_extra_args=["--dangerously-skip-permissions"])):
        t = b._try_admin(USER, "/reload")
    check("禁用参数会拒绝", t.startswith("❌"), t)
    check("说了装不起来", "装不起来" in t, t)


def test_reload_guards_whitelist(tmpdir, fake_claude):
    """白名单守卫：重载成空的、或者把自己挤出去，都拒绝——那等于锁在门外。"""
    b = make_bridge(tmpdir, fake_claude)

    with _Reload(_cfg(tmpdir, allowed_user_ids=[])):
        t = b._try_admin(USER, "/reload")
    check("白名单空掉会拒绝", t.startswith("❌"), t)
    check("说清谁都执行不了", "谁都执行不了" in t, t)
    check("旧的白名单还在", b.allowed == {USER}, b.allowed)

    with _Reload(_cfg(tmpdir, allowed_user_ids=["别人"])):
        t = b._try_admin(USER, "/reload")
    check("把自己挤出去会拒绝", t.startswith("❌"), t)
    check("说清重载后自己发不动了", "发不动指令" in t, t)
    check("旧的白名单还在（第二次）", b.allowed == {USER}, b.allowed)


def test_reload_refuses_while_busy(tmpdir, fake_claude):
    """有活儿在跑的时候不换配置：换零件不换正在用的那个。"""
    b = make_bridge(tmpdir, fake_claude)
    path = _cfg(tmpdir, prefix="/zz")
    gate = threading.Event()
    b.runner.run = lambda uid, prompt: (gate.wait(5.0), "✅ 干完了")[1]

    _send(b, "慢慢来", "rb1")
    check("活儿起来了", _wait_for(lambda: b._running is not None))

    with _Reload(path):
        t = b._try_admin(USER, "/reload")
    check("忙着的时候拒绝重载", t.startswith("❌"), t)
    check("拒绝时指了条明路", "/stop" in t, t)
    check("配置确实没换", b.prefix != "/zz", b.prefix)

    gate.set()
    check("收尾", _wait_for(lambda: b._running is None))


def test_reload_warns_credentials(tmpdir, fake_claude):
    """token / account_id 换了热更不了，要说清而不是假装生效。"""
    b = make_bridge(tmpdir, fake_claude)
    with _Reload(_cfg(tmpdir, bot_token="newtok", account_id="bot-2")):
        t = b._try_admin(USER, "/reload")
    check("凭据变了会提醒", "重新登录" in t, t)
    check("点名了 bot_token", "bot_token" in t, t)
    check("客户端没被换掉（token 还是老的）", b.client.token == "tok", b.client.token)


def test_reload_enables_timer(tmpdir, fake_claude):
    """定时/提醒整块从关着被打开时，那条心跳线程得补起来。"""
    b = make_bridge(tmpdir, fake_claude)
    check("默认没装定时/提醒，心跳线程也没起", b._timer is None, b._timer)

    path = _cfg(tmpdir, meeting={"enable": True, "timer_enable": True},
                remind={"enable": True})
    with _Reload(path):
        t = b._try_admin(USER, "/reload")
    check("重载成功", t.startswith("✅"), t)
    check("定时装上来了", b.schedule is not None, b.schedule)
    check("提醒装上来了", b.reminders is not None, b.reminders)
    check("心跳线程补起来了", b._timer is not None and b._timer.is_alive(), b._timer)


# ---------- 五、叫停 ----------

def test_stop(tmpdir, fake_claude):
    b = make_bridge(tmpdir, fake_claude)

    t = b._try_admin(USER, "/stop")
    check("没在跑的时候直说", "没有在跑的任务" in t, t)
    check("没在跑时不报「没叫停成功」", "没叫停成功" not in t, t)

    # 有活儿在跑：cancel 换成假的，绝不真杀进程
    gate = threading.Event()
    calls = []
    b.runner.run = lambda uid, prompt: (gate.wait(5.0), "✅ 干完了")[1]
    b.runner.cancel = lambda: (calls.append(1), True)[1]

    _send(b, "慢慢来", "sp1")
    check("活儿起来了", _wait_for(lambda: b._running is not None))

    t = b._try_admin(USER, "/stop")
    check("叫停成功有回话", t.startswith("⏹"), t)
    check("回话里带上了那条的原话", "慢慢来" in t, t)
    check("停止请求送达执行器", _wait_for(lambda: len(calls) == 1), calls)

    # 叫停失败也要说清，不能假装成功
    b.runner.cancel = lambda: False
    t = b._try_admin(USER, "/stop")
    check("重复停止请求不会误报取消失败", "没叫停成功" not in t and ("停止请求已收到" in t or "没有在跑的任务" in t), t)

    gate.set()
    check("收尾", _wait_for(lambda: b._running is None))


def test_stop_does_not_touch_queue(tmpdir, fake_claude):
    """/stop 只停正在跑的那条，排着的留着一动不许动。"""
    b = make_bridge(tmpdir, fake_claude)
    gate = threading.Event()
    b.runner.run = lambda uid, prompt: (gate.wait(5.0), "✅ 干完了")[1]
    b.runner.cancel = lambda: True

    _send(b, "慢慢来", "sq1")
    check("活儿起来了", _wait_for(lambda: b._running is not None))
    _send(b, "排队甲", "sq2")
    _send(b, "排队乙", "sq3")
    check("两条排在后面", b.queue.qsize() == 2, b.queue.qsize())

    t = b._try_admin(USER, "/stop")
    check("说了排着的没动", "没动" in t, t)
    check("队列真的没动", b.queue.qsize() == 2, b.queue.qsize())

    gate.set()
    time.sleep(0.3)
    b.queue = queue.Queue()


def test_stopall_clears_queue(tmpdir, fake_claude):
    """/stopall 是「全」停：连排着的一起清。"""
    b = make_bridge(tmpdir, fake_claude)
    gate = threading.Event()
    b.runner.run = lambda uid, prompt: (gate.wait(5.0), "✅ 干完了")[1]
    b.runner.cancel = lambda: True

    _send(b, "慢慢来", "sa1")
    check("活儿起来了", _wait_for(lambda: b._running is not None))
    _send(b, "排队甲", "sa2")
    _send(b, "排队乙", "sa3")
    check("两条排在后面", b.queue.qsize() == 2, b.queue.qsize())

    t = b._try_admin(USER, "/stopall")
    check("全停有回话", t.startswith("⏹"), t)
    check("说了连排着的一起清", "也清了" in t, t)
    check("队列真被清了", b.queue.qsize() == 0, b.queue.qsize())

    gate.set()
    check("收尾", _wait_for(lambda: b._running is None))


def test_task_cancelled_chain(tmpdir, fake_claude):
    """run() 抛 TaskCancelled 时，桥要回「已叫停」而不是把半截输出当结果发出去。"""
    b = make_bridge(tmpdir, fake_claude)

    def killed(uid, prompt):
        raise claude_runner.TaskCancelled()

    b.runner.run = killed
    drain(b, [wecom_msg("/cc 干个长的", "tc1")])
    t = texts()
    check("回了「已叫停」", any("已叫停" in x for x in t), t)
    check("没用 ⁇ 那种出错口气", not any("执行失败" in x for x in t), t)
    check("提醒了可能已经动过文件", any("动过" in x for x in t), t)
    check("叫停之后 _running 清干净了", b._running is None, b._running)

    # 紧接着下一条还能正常干活（桥没被叫停带崩）——记得把 run 换回能干活的那个
    b.runner.run = lambda uid, prompt: "✅ 干完了：" + prompt
    drain(b, [wecom_msg("/cc 再来一条", "tc2")])
    check("叫停之后桥照旧能用", any("再来一条" in x for x in texts()), texts())

# ---------- 六、claude_runner 那一层 ----------

def test_kill_tree(tmpdir):
    """kill_tree 只负责发出 taskkill，成功/失败都要老实返回，绝不抛。"""
    seen = []

    def ok(argv, **kw):
        seen.append(argv)
        return None

    check("成功时返回 True", claude_runner.kill_tree(1234, run=ok) is True)
    check("发的是 taskkill /T /F", seen and seen[0][:2] == ["taskkill", "/T"], seen)
    check("带上了 pid", seen and "1234" in seen[0], seen)

    def boom(argv, **kw):
        raise OSError("这台机器上没有 taskkill")

    check("失败时返回 False（不抛）", claude_runner.kill_tree(1, run=boom) is False)


def test_runner_cancel(tmpdir, fake_claude):
    r = claude_runner.ClaudeRunner(claude_bin=fake_claude, workdir=tmpdir,
                                   session_file=os.path.join(tmpdir, "s.json"))
    killed = []
    r._killer = lambda pid: (killed.append(pid), True)[1]

    check("没在跑的时候 cancel 返回 False", r.cancel() is False)
    check("没在跑就不该去杀", not killed, killed)

    class _P:
        pid = 4242

        def poll(self):
            return None                 # 还在跑

    r._proc = _P()
    check("在跑的时候 cancel 返回 True", r.cancel() is True)
    check("去杀了那个 pid", killed == [4242], killed)
    check("记下了「已叫停」", r._cancelled is True)

    # 已经结束的进程：别谎报
    class _Done(_P):
        def poll(self):
            return 0

    r2 = claude_runner.ClaudeRunner(claude_bin=fake_claude, workdir=tmpdir,
                                    session_file=os.path.join(tmpdir, "s2.json"))
    r2._killer = lambda pid: True
    r2._proc = _Done()
    check("已经跑完的不算叫停成功", r2.cancel() is False)

    # 杀手本身抛异常时，要退回 proc.kill()，不能把叫停这条路弄崩
    class _P2:
        pid = 7
        killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    p2 = _P2()

    def boom(pid):
        raise RuntimeError("杀进程器坏了")

    r3 = claude_runner.ClaudeRunner(claude_bin=fake_claude, workdir=tmpdir,
                                    session_file=os.path.join(tmpdir, "s3.json"))
    r3._killer = boom
    r3._proc = p2
    check("杀手抛了也不崩", r3.cancel() is True)
    check("退回 proc.kill() 真的杀了", p2.killed is True)


# ---------- 七、HELP ----------

def test_help_lists_admin():
    h = wechat_bridge.HELP
    for s in ("/status", "/log", "/queue", "/reload", "/stop", "/stopall"):
        check("HELP 里有 %s" % s, s in h, h[-400:])
    check("HELP 说明了这批不用带前缀", "不用带 /cc" in h, h[-400:])


# ---------- 八、复审之后补上的那几条 ----------

def test_enqueue_never_blocks(tmpdir, fake_claude):
    """队列满了**绝不能阻塞**——阻塞的是轮询线程，那连 /stop 都进不来。

    这是复审揪出来的真回归：给 Queue 设了 maxsize 之后，`put` 会一直等。

    worker 是 `Bridge.__init__` 里起的**真线程**，所以这里得先把它占在一条活儿上
    （和 test_queue_max 一个路子）。不占住的话，它会在两条断言之间把队列里那条
    拿走，qsize 一会儿 1 一会儿 0，这个用例就时红时绿——第一版就是这么翻的车。
    """
    b = make_bridge(tmpdir, fake_claude)
    b.queue_max = 1
    b.queue.maxsize = 1
    gate = threading.Event()
    b.runner.run = lambda uid, prompt: (gate.wait(30.0), "✅ 干完了")[1]

    _send(b, "占住工作者", "eq0")
    check("先占住工作者", _wait_for(lambda: b._running is not None))

    # worker 忙着、队列空着：先把上限占满
    first = Message(wecom_msg("/cc 排队甲", "eq1"))
    check("第一条能排进去", b._enqueue((first, "排队甲", None), first) is None)
    check("队列满了（1/1）", b.queue.qsize() == 1, b.queue.qsize())

    t0 = time.time()
    again = Message(wecom_msg("/cc 再来", "eq2"))
    full = b._enqueue((again, "再来", None), again)
    dt = time.time() - t0
    check("满了立刻返回（没阻塞）", dt < 1.0, "%.2f 秒" % dt)
    check("满了会返回一句话让上层去回", isinstance(full, str) and full, full)
    check("那句话以 ❌ 开头", full.startswith("❌"), full)
    check("说了上限", "上限 1" in full, full)
    check("队列没被塞进去", b.queue.qsize() == 1, b.queue.qsize())

    check("清掉排着的那条", b._drain_queue() == 1, b.queue.qsize())
    ok = Message(wecom_msg("/cc 收下", "eq3"))
    check("清空后返回 None（收下了）", b._enqueue((ok, "收下", None), ok) is None)

    # queue_max=0 是「不限」：maxsize 0 不抛 Full，回到以前的行为（刚才那条也算上）
    b.queue_max = 0
    b.queue.maxsize = 0
    for i in range(50):
        m = Message(wecom_msg("/cc 塞 %d" % i, "n%d" % i))
        b._enqueue((m, "塞", None), m)
    check("不限时塞多少都收", b.queue.qsize() == 51, b.queue.qsize())

    # 收尾：先清掉排着的再放行，别让 worker 去跑这几十条假任务
    b._drain_queue()
    gate.set()
    check("收尾", _wait_for(lambda: b._running is None))


def test_all_enqueue_goes_through_one_door():
    """**所有**入队都必须走 `_enqueue`。

    直接在别处 `self.queue.put(...)` 的话，队列满了就会阻塞轮询线程——这个错改回来
    太容易了（本来就是 put），所以拿源码把它卡住。
    """
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
    bad = []
    for name in ("wechat_bridge.py", "bridge_screen.py", "bridge_files.py",
                 "bridge_meeting.py", "bridge_media.py", "bridge_power.py",
                 "bridge_remind.py", "bridge_chat.py", "bridge_admin.py"):
        with open(os.path.join(here, name), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if "queue.put(" in line and "put_nowait" not in line:
                    bad.append("%s:%d" % (name, i))
    check("没有绕过 _enqueue 的阻塞式入队", not bad, "｜".join(bad))
    with open(os.path.join(here, "wechat_bridge.py"), encoding="utf-8") as fh:
        check("_enqueue 用的是 put_nowait", "put_nowait" in fh.read())


def test_running_covers_every_job(tmpdir, fake_claude):
    """四种活儿都得有名字——看图那几分钟里 /status 不能报「没在跑」。"""
    t = wechat_bridge.Bridge._job_title
    check("普通任务用你那句原话", t("看一下 D 盘", None) == "看一下 D 盘")
    check("发文件有自己的名字", "文件" in t("", {"sendfile": ["x"]}),
          t("", {"sendfile": ["x"]}))
    check("看看画面有自己的名字", "看看画面" in t("", {"look": True}),
          t("", {"look": True}))
    check("看图找坐标有自己的名字", "坐标" in t("", {"query": "开始播放"}),
          t("", {"query": "开始播放"}))

    # 真跑一条「看看画面」的 job，跑着的时候 _running 得有值。
    # 把 `_run_look` 桩掉：要验的是「_loop_worker 给 job 类的活儿也写了 _running」，
    # 不是 _run_look 自己的实现（那个有它自己的一组用例）。
    # 队列项里的 msg 用真的 Message——`_loop_worker` 要读 msg.from_user_id。
    b = make_bridge(tmpdir, fake_claude)
    gate = threading.Event()

    def fake_look(msg, job):
        gate.wait(5.0)

    b._run_look = fake_look
    look_msg = Message(wecom_msg("/cc 看看画面", "j1"))
    b._enqueue((look_msg, "", {"look": True, "uid": USER}), look_msg)
    check("看图那条起来了", _wait_for(lambda: b._running is not None))
    st = b._try_admin(USER, "/status")
    check("/status 报的是看图那条", "正在跑：" in st and "看看画面" in st, st)
    check("_running 记的是这个用户", b._running.get("uid") == USER, b._running)
    gate.set()
    check("跑完清掉", _wait_for(lambda: b._running is None))


def test_run_timeout_and_proc(tmpdir, fake_claude):
    """换 Popen 之后，超时那条路和 `_proc` 的生命周期都得自己钉住。

    假 claude 见到 SLEEP 会睡 2 秒，所以 timeout 设成 1 秒就能撞上超时。这条会真的
    起一个子进程、真的把它杀掉——杀的只是**我们自己刚起的那个**（`make_fake_claude`
    造出来的脚本），不碰任何别人的东西，也不碰这台机器的电源。
    """
    r = claude_runner.ClaudeRunner(claude_bin=fake_claude, workdir=tmpdir,
                                   timeout_sec=1,
                                   session_file=os.path.join(tmpdir, "t.json"))
    seen = {}

    def watch():
        for _ in range(300):
            if r._proc is not None:
                seen["had_proc"] = True
                return
            time.sleep(0.01)

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    t0 = time.time()
    out = r.run("u", "SLEEP 一下")
    dt = time.time() - t0
    th.join(2)
    check("超时那句回话没变", "超过 1 秒仍未结束" in out, out)
    check("说清已中止", "已中止" in out, out)
    check("及时返回（没挂死）", dt < 20, "%.1f 秒" % dt)
    check("跑的时候 _proc 是拿得到的", seen.get("had_proc") is True, seen)
    check("跑完 _proc 清空了", r._proc is None, r._proc)
    check("跑完没留下叫停标记", r._cancelled is False, r._cancelled)

    out2 = r.run("u", "你好")
    check("正常一条也能跑", "已处理" in out2, out2)
    check("正常跑完 _proc 也清空", r._proc is None, r._proc)


def test_kill_tree_uses_returncode():
    """taskkill 返回非 0 时不能说「杀成功了」——那是「发过信号」，不是「已经死了」。"""

    class Done(object):
        def __init__(self, rc):
            self.returncode = rc

    check("返回 0 才算成功",
          claude_runner.kill_tree(1, run=lambda *a, **k: Done(0)) is True)
    check("返回 1 当失败（好退回 proc.kill）",
          claude_runner.kill_tree(1, run=lambda *a, **k: Done(1)) is False)
    check("返回 128（找不到进程）也当失败",
          claude_runner.kill_tree(1, run=lambda *a, **k: Done(128)) is False)


def test_reload_guard_runs_before_build(tmpdir, fake_claude):
    """白名单被拒时，**装配那一步不能已经被跑过**——它有盘上副作用。

    `ClaudeRunner(keep_context=False)` 会当场清空 sessions.json。守卫要是排在装配
    之后，「旧配置留着没动」就是假话：会话其实已经被抹了。
    """
    b = make_bridge(tmpdir, fake_claude)
    sess = os.path.join(tmpdir, "sessions.json")
    with open(sess, "w", encoding="utf-8") as fh:
        json.dump({"u-me": "sess-abc"}, fh)
    b.runner.session_file = sess

    path = _cfg(tmpdir, keep_context=False, allowed_user_ids=["别人"])
    with _Reload(path):
        t = b._try_admin(USER, "/reload")
    check("白名单被拒了", t.startswith("❌"), t)
    body = open(sess, encoding="utf-8").read() if os.path.exists(sess) else ""
    check("sessions.json 没被动过（守卫排在装配之前）", "sess-abc" in body, body or "文件没了")


def test_reload_survives_disk_error(tmpdir, fake_claude):
    """读配置碰到的磁盘错误（不是 SystemExit）也得回一句话，不能哑掉。"""
    b = make_bridge(tmpdir, fake_claude)

    def boom(path=bridge_admin.CONFIG_PATH):
        raise PermissionError("[Errno 13] Permission denied: 'config.json'")

    old = bridge_admin.load_config
    bridge_admin.load_config = boom
    try:
        t = b._try_admin(USER, "/reload")
    finally:
        bridge_admin.load_config = old
    check("磁盘错误也回话了", isinstance(t, str) and t.startswith("❌"), t)
    check("说清旧配置没动", "旧配置留着没动" in t, t)

    # 真实的「config.json 是个目录」也走一遍（load_config 的 open 会抛 OSError）
    d = os.path.join(tmpdir, "dircfg.json")
    os.makedirs(d, exist_ok=True)
    with _Reload(d):
        t2 = b._try_admin(USER, "/reload")
    check("config.json 变成目录也回话",
          isinstance(t2, str) and t2.startswith("❌"), t2)


def test_stop_hides_other_users_words(tmpdir, fake_claude):
    """/stop 的回话不许把别人的原话念给你听（和 /status、/queue 一个口径）。"""
    other = "u-别人"
    b = make_bridge(tmpdir, fake_claude, allowed=(USER, other))
    gate = threading.Event()
    b.runner.run = lambda uid, prompt: (gate.wait(5.0), "✅ 干完了")[1]
    b.runner.cancel = lambda: True

    _send(b, "别人的机密任务", "ou1", user=other)
    check("别人的活儿起来了", _wait_for(lambda: b._running is not None))
    check("_running 记的是别人", b._running.get("uid") == other, b._running)

    t = b._try_admin(USER, "/stop")
    check("说了叫停", t.startswith("⏹"), t)
    check("没把别人的原话念出来", "机密任务" not in t, t)
    check("只说是「另一个用户」", "另一个用户" in t, t)

    # 放过第一条、等 worker 空下来，再发你自己那条。worker 只有一条：上一条没跑完
    # 的话，新那条只是排在队列里，`_running` 还写着别人那条——断言就指错了对象
    gate.set()
    check("第一条收了尾", _wait_for(lambda: b._running is None))

    # 你自己的活儿还是照常报原话
    gate2 = threading.Event()
    b.runner.run = lambda uid, prompt: (gate2.wait(5.0), "✅ 干完了")[1]
    _send(b, "我自己那个表", "me1", user=USER)
    check("自己的活儿起来了", _wait_for(lambda: b._running is not None))
    check("这次跑的是你自己那条", b._running.get("uid") == USER, b._running)
    t2 = b._try_admin(USER, "/stop")
    check("自己的原话照常报", "我自己那个表" in t2, t2)

    gate2.set()
    check("收尾", _wait_for(lambda: b._running is None))
