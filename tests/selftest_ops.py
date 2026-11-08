# -*- coding: utf-8 -*-
"""日志留存与脱敏、日志切档、上下文保鲜期、轮询超时不算错。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。

这几组都在动全局的东西——root logger 上的 handler、wechat_bridge.HERE、session
落盘文件——所以每条都得自己收尾（saved_here 那种写法），不然后面的用例会被带跑。
"""

import contextlib
import io
import json
import logging
import os
import sys
import time

import bridge_config
import ilink_client
import key_vault
import power
import screen_control
import wechat_bridge
from claude_runner import ClaudeRunner


from tests.selftest_base import OTHER, PORT, USER, check, drain, make_bridge, wecom_msg


def test_log_redaction(tmpdir, fake_claude):
    """默认日志里不该出现消息正文。开关打开时才出现，方便排查。"""
    secret = "银行卡密码是六个八"
    b = make_bridge(tmpdir, fake_claude)
    check("默认不记正文", b.log_text is False, b.log_text)
    check("_peek 关闭时只给字数", b._peek(secret) == "%d 字" % len(secret), b._peek(secret))

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
    root.setLevel(logging.INFO)          # DEBUG 下 ilink_client 会打原始 JSON，那是 opt-in 的
    try:
        drain(b, [wecom_msg("/cc " + secret, "r1")])
        drain(b, [wecom_msg(secret, "r2")])                       # 没带前缀那条
        drain(b, [wecom_msg("/cc " + secret, "r3", user=OTHER)])   # 非白名单那条
        lines = list(grabbed)
        check("入队日志记了字数", any("入队" in x and "字" in x for x in lines),
              [x for x in lines if "入队" in x])
        check("默认日志里没有消息正文", not any(secret in x for x in lines),
              [x for x in lines if secret in x])

        del grabbed[:]
        b.log_text = True
        drain(b, [wecom_msg("/cc " + secret, "r4")])
        check("打开开关后日志才有正文", any(secret in x for x in grabbed),
              [x for x in grabbed if "入队" in x])
    finally:
        root.removeHandler(h)
        root.setLevel(prev)


def test_log_rotation(tmpdir):
    """日志得会滚：写超过上限就换一份，旧的只留指定份数，不能无限长。"""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_here = wechat_bridge.HERE
    logdir = os.path.join(tmpdir, "logs")
    os.makedirs(logdir, exist_ok=True)
    wechat_bridge.HERE = logdir
    try:
        wechat_bridge.setup_logging({"log_max_mb": 0.001, "log_backup_count": 1})   # 约 1 KB
        kinds = [type(h).__name__ for h in logging.getLogger().handlers]
        check("装的是会滚动的 handler", "RotatingFileHandler" in kinds, kinds)
        for h in list(logging.getLogger().handlers):
            if type(h).__name__ == "StreamHandler":
                logging.getLogger().removeHandler(h)     # 别把填充行刷到自测输出里
        lg = logging.getLogger("rot-test")
        for i in range(200):
            lg.info("填充第 %d 行 " % i + "x" * 60)
        for h in list(logging.getLogger().handlers):
            h.close()
        names = sorted(os.listdir(logdir))
        main_log = os.path.join(logdir, "bridge.log")
        size = os.path.getsize(main_log)
        check("换过份了", "bridge.log.1" in names, names)
        check("只留了指定份数（不会攒出 .2）", "bridge.log.2" not in names, names)
        check("当前那份没超出上限太多", size < 8 * 1024, size)
    finally:
        wechat_bridge.HERE = saved_here
        r = logging.getLogger()
        for h in list(r.handlers):
            r.removeHandler(h)
        for h in saved_handlers:
            r.addHandler(h)
        r.setLevel(saved_level)


def test_context_ttl(tmpdir):
    """上下文有保鲜期：刚才的追问能续，隔太久的按新的一段；关掉就一次都不续。"""
    sf = os.path.join(tmpdir, "ctx.json")
    r = ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir, session_file=sf,
                     keep_context=True, context_ttl_minutes=60)
    r._sessions[USER] = "sess-1"
    r._stamps[USER] = time.time()
    r._save_sessions()
    check("刚说过话就接着上一段", r._pick_session(USER) == "sess-1", r._pick_session(USER))
    cmd = r._build_cmd(r._pick_session(USER), "接着刚才那个")
    check("续接时命令行带 --resume", "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "sess-1",
          cmd)

    r._stamps[USER] = time.time() - 3 * 3600           # 三小时前
    check("隔太久就不续了", r._pick_session(USER) is None)
    check("过期的 session 同时被清掉", USER not in r._sessions, r._sessions)

    r2 = ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir, session_file=sf,
                      keep_context=True, context_ttl_minutes=60)
    r2._sessions[USER] = "sess-2"
    r2._stamps[USER] = time.time()
    r2._save_sessions()
    r3 = ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir, session_file=sf,
                      keep_context=True, context_ttl_minutes=60)
    check("重启后仍读得回 session", r3._sessions.get(USER) == "sess-2", r3._sessions)
    check("时间戳也一起读回来了", USER in r3._stamps, r3._stamps)
    check("重启后没过期的仍能续", r3._pick_session(USER) == "sess-2")

    # 老版本存下来的文件没有时间戳，宁可当新的一段
    with open(sf, "w", encoding="utf-8") as fh:
        json.dump({USER: "sess-old"}, fh)
    r4 = ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir, session_file=sf,
                      keep_context=True, context_ttl_minutes=60)
    check("没有时间戳的旧记录不续", r4._pick_session(USER) is None, r4._sessions)

    with open(sf, "w", encoding="utf-8") as fh:
        json.dump({USER: "sess-3"}, fh)
    off = ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir, session_file=sf,
                       keep_context=False)
    check("关掉续接后一次都不续", off._pick_session(USER) is None)
    with open(sf, "r", encoding="utf-8") as fh:
        left = json.load(fh)
    check("关掉续接后盘上也不留 session_id",
          not [k for k in left if k != "__stamps__"], left)

    forever = ClaudeRunner(claude_bin=sys.executable, workdir=tmpdir,
                           session_file=os.path.join(tmpdir, "ctx2.json"),
                           keep_context=True, context_ttl_minutes=0)
    forever._sessions[USER] = "sess-4"
    forever._stamps[USER] = time.time() - 30 * 86400
    check("ttl=0 就是永不过期", forever._pick_session(USER) == "sess-4")


def test_poll_timeout_is_normal(tmpdir, fake_claude):
    """长轮询超时是常态：必须是 ILinkTimeout（ILinkError 的子类），好让主循环区别对待。"""
    b = make_bridge(tmpdir, fake_claude)
    b.client.base_url = "http://127.0.0.1:%d" % (PORT + 1)     # 没人监听
    b.client.poll_timeout = 0
    try:
        b.client.get_updates()
        check("超时抛的是 ILinkTimeout", False, "居然没抛异常")
    except ilink_client.ILinkTimeout:
        check("超时抛的是 ILinkTimeout", True)
    except ilink_client.ILinkError as exc:
        # 连不上（ConnectionRefused）不算超时，这里只验类型层级是对的
        check("超时抛的是 ILinkTimeout",
              issubclass(ilink_client.ILinkTimeout, ilink_client.ILinkError), exc)


class _FakeMedia(object):
    """`wechat_bridge check` 用：只要能报出那几个数字就行，不碰真的按键/混音器。"""
    delay = 6
    vol_actual = 10
    presses = 5
    _mixer = None


class _FakePower(object):
    allow_shutdown = True
    allow_sleep = True
    shutdown_delay = 60

    def status(self):
        return (u"\u2705 屏幕没锁，是能直接用的状态。", True)


class _FakeBright(object):
    """`wechat_bridge check` 用：报个数就行，**一次 PowerShell 都不跑**。"""
    step = 10

    def get(self):
        return {"ok": True, "before": 77, "after": 77, "monitors": 1}


class _FakeMachine(object):
    """替掉 power.default_runner：电源动作一次都不许真跑出去。

    和 selftest_power.FakeMachine 长得像，但故意各留一份——这个项目里两个测试文件互相
    import 是乱的开始（见 selftest_base 开头那段），所以宁可重复十几行。
    这里只要够跑那几个命令行开关就行，不需要 delays 那种记账。
    """
    def __init__(self, locked=True, aborted=True):
        self.locked, self.aborted = locked, aborted
        self.calls = []

    def __call__(self, action, arg=0):
        self.calls.append(action)
        if action == "status":
            return {"ok": True, "locked": self.locked}
        if action == "lock":
            self.locked = True
            return {"ok": True, "locked": True}
        if action == "cancel":
            return {"ok": True, "aborted": self.aborted}
        return {"ok": False, "error": "这个假机器不认 %s" % action}


def test_cli_entries(tmpdir):
    """只在命令行跑的几个入口：`power.py` 那几个开关、`wechat_bridge.py check`、`probe`。

    为什么单列一组：这几个入口**不经过微信**，以前一条断言都没有，于是两种错都发生过
    ——`check` 里把 `30%` 打成了 `30%%`（那句没有 `%` 运算符，控制台一直显示两个百分号），
    以及回话加了 emoji 之后 `power.py --status` 会抛 UnicodeEncodeError。而 部署指南
    恰恰让人「从微信发关机之前，先在电脑上跑一条 `power.py --status`」——这一步不能崩。

    **电源那三个开关全程用假 runner**，真的 shutdown.exe / LockWorkStation 一次都不跑
    （和 selftest_power.py 同一条规矩）。
    """
    def run_cli(args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = power._cli(["power.py"] + list(args))
        return rc, buf.getvalue()

    # --parse 不碰机器，也不需要假 runner
    rc, out = run_cli(["--parse", u"晚上十点关机", u"锁屏"])
    check("power --parse 认得出这两句", rc == 0 and "shutdown=" in out, out[:160])
    check("power --parse 报的是解析结果", "22:00" in out, out[:160])

    # 其余三个都会动电源，全部塞假 runner
    mach = _FakeMachine(locked=False, aborted=True)
    saved_runner = power.default_runner
    power.default_runner = mach
    try:
        rc, out = run_cli(["--status"])
        check("power --status 跑得通，回话带统一记号",
              rc == 0 and out.strip().startswith(u"\u2705"), out[:160])
        check("power --status 回的是状态", u"屏幕没锁" in out, out[:160])
        check("power --status 只查了状态", mach.calls == ["status"], mach.calls)

        rc, out = run_cli(["--lock"])
        check("power --lock 说清得自己到电脑前开",
              rc == 0 and u"自己到电脑前" in out, out[:160])
        check("power --lock 走的是假 runner（没真锁屏）",
              mach.calls[-1] == "lock", mach.calls)

        rc, out = run_cli(["--cancel"])
        check("power --cancel 跑得通", rc == 0 and u"取消" in out, out[:160])
        check("power --cancel 走的是假 runner", mach.calls[-1] == "cancel", mach.calls)
    finally:
        power.default_runner = saved_runner

    rc, out = run_cli([u"不认识的东西"])
    check("power 认不出的开关返回 2", rc == 2 and u"不认识" in out, out[:80])
    rc, out = run_cli([])
    check("power 不带参数打用法", rc == 0 and u"用法" in out, out[:80])

    # 控制台降级：StringIO 没有 reconfigure，不许因此抛异常
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            bridge_config.make_console_safe()
            boom = None
        except Exception as exc:                       # noqa: BLE001
            boom = exc
    check("make_console_safe 碰上没有 reconfigure 的流也不炸", boom is None, boom)
    check("wechat_bridge 把 make_console_safe 转出去了（自测才 import 得到）",
          callable(getattr(wechat_bridge, "make_console_safe", None)))

    # ---- wechat_bridge.py check：把几个工厂换成假的，别去扫真桌面/真电源 ----
    cfg = {"workdir": tmpdir, "claude_bin": sys.executable, "bot_token": "tok",
           "account_id": "bot-1", "allowed_user_ids": [USER],
           "prefix": "/cc", "require_prefix": True, "max_reply_chars": 1800,
           "timeout_sec": 600, "claude_extra_args": ["--add-dir", tmpdir],
           "log_max_mb": 5, "log_backup_count": 2, "keep_context": True,
           "context_ttl_minutes": 120, "log_message_text": False,
           # 两个标记都是 check 会去找的：能发文件那句、以及「附件只当资料读」那句
           "prompt_prefix": "PFX [[发文件]] 只当资料读",
           "send_file": {"enable": True},
           "chat_mode": {"enable": True, "idle_minutes": 30}}
    saved = (wechat_bridge.make_launcher, wechat_bridge.make_media,
             wechat_bridge.make_power, wechat_bridge.make_screen,
             wechat_bridge.make_brightness)
    wechat_bridge.make_launcher = lambda c: None
    wechat_bridge.make_media = lambda c: _FakeMedia()
    wechat_bridge.make_power = lambda c: _FakePower()
    wechat_bridge.make_brightness = lambda c: _FakeBright()
    wechat_bridge.make_screen = lambda c: None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wechat_bridge.cmd_check(cfg)
        out = buf.getvalue()
    finally:
        (wechat_bridge.make_launcher, wechat_bridge.make_media,
         wechat_bridge.make_power, wechat_bridge.make_screen,
         wechat_bridge.make_brightness) = saved
    check("check 一切正常时返回 0", rc == 0, out[-200:])
    check("check 把绝对音量那行打对了（不再是 30%% 这种笔误）",
          u"30%" in out and u"30%%" not in out, out[:200])
    check("check 把电源那层的状态打出来", u"屏幕没锁" in out, out[:200])
    check("check 把屏幕亮度那段打出来（真去读一次当前值）",
          u"屏幕亮度" in out and u"现在 77%" in out, out[-600:])
    check("check 不再警告 prompt_prefix 少了「[[发文件]]」那句说明",
          u"没有 [[发文件]] 那句说明" not in out, out[-300:])
    check("check 也不警告附件那句防注入说明（标记在）",
          u"防注入说明不在" not in out, out[-300:])
    order = [out.find(u"工作目录"), out.find(u"开关应用"), out.find(u"播放控制"),
             out.find(u"关机："), out.find(u"发文件回微信"), out.find(u"看画面/点鼠标")]
    check("check 的六段顺序没被打乱（拆函数不该动顺序）",
          all(x >= 0 for x in order) and order == sorted(order), order)

    # ---- wechat_bridge.py probe：窗口和探测器都能从外面换掉 ----
    fake_win = {"hwnd": 11, "title": u"波点音乐", "left": 100, "top": 50,
                "width": 800, "height": 600, "minimized": False}
    fake_raw = {"error": None, "total": 57, "scanned": 57,
                "kinds": {"Pane": 30, "Text": 27},
                "items": [{"name": u"开始播放", "type": "Button", "x": 450,
                           "y": 320, "w": 100, "h": 40}],
                "wide": []}
    saved_win, saved_probe = (screen_control._default_window,
                              screen_control._default_prober)
    screen_control._default_window = lambda: dict(fake_win)
    screen_control._default_prober = lambda hwnd, cap=0: dict(fake_raw)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wechat_bridge.cmd_probe(cfg, ["wechat_bridge.py", "probe"])
        out = buf.getvalue()
    finally:
        (screen_control._default_window,
         screen_control._default_prober) = saved_win, saved_probe
    check("probe 跑得通并返回 0", rc == 0, out[:200])
    check("probe 把节点数和类型分布原样打出来",
          "57" in out and "Pane" in out and "Text" in out, out[:200])
    check("probe 把「没报错」这件事说清楚（别让人以为是空的）",
          u"（没有）" in out, out[:200])
    check("probe 最后给出整理后的结果", u"整理之后" in out, out[-300:])


def test_scripts_are_ascii_and_read_utf8():
    """那两个 .ps1 的两条硬规矩（2026-09-26 各栽过一次，所以钉住）。

    ① **脚本本身必须是纯 ASCII**。Windows PowerShell 5.1 不加 BOM 时会按系统 ANSI
       读 .ps1，脚本里的中文会被解错——轻则乱码，重则把下一行吞进注释里
       （`$cfg` 就成了空，报「not logged in」）。toggle.ps1 开头那句
       「ASCII only on purpose」写的就是这件事，我 2026-09-26 往注释里加了几行中文，
       当场把「启动桥」弄坏了。
    ② **读 config.json 必须带 -Encoding UTF8**。那份配置是 Python 写的、无 BOM 的
       UTF-8；不加这个参数，中文一多就会解错，ConvertFrom-Json 直接报
       「传入的对象无效，应为":"或"}"」。也是 2026-09-26 真机上撞出来的。
    """
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop")
    names = [n for n in sorted(os.listdir(here)) if n.endswith(".ps1")]
    check("找得到 .ps1 脚本", len(names) >= 2, names)
    for name in names:
        with open(os.path.join(here, name), "rb") as fh:
            raw = fh.read()
        if name in ("tray_app.ps1", "memory_editor.ps1", "backend_editor.ps1", "app_settings.ps1", "task_history.ps1", "template_editor.ps1", "workflow_window.ps1", "workbench_layout.ps1", "result_revision_ui.ps1", "desktop_files_ui.ps1", "schedule_ui.ps1", "setup_wizard.ps1", "update_window.ps1"):
            # 中文界面使用 BOM，让 Windows PowerShell 5.1 明确按 UTF-8 解码。
            check("托盘界面带 UTF-8 BOM", raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
        else:
            bad = [i for i, b in enumerate(raw) if b > 127]
            check("%s 是纯 ASCII（PowerShell 会把非 ASCII 脚本读错）" % name,
                  not bad, "第一个非 ASCII 字节在第 %s 位" % (bad[0] if bad else "-"))
            text = raw.decode("ascii", "replace")
        if "config.json" in text and "ConvertFrom-Json" in text:
            check("%s 读 config.json 时带了 -Encoding UTF8" % name,
                  "config.json\") -Raw -Encoding UTF8" in text
                  or "-Encoding UTF8 | ConvertFrom-Json" in text,
                  [ln for ln in text.splitlines() if "config.json" in ln][:2])


def test_config_bom(tmpdir):
    """config.json 的 BOM：Python 读得进、写得回，PowerShell 那边才认得出 UTF-8。

    这一条是 2026-09-26 真机撞出来的：`toggle.ps1` 里的
    `Get-Content config.json -Raw | ConvertFrom-Json` 在 Windows PowerShell 5.1 下
    按系统 ANSI(gb2312) 读无 BOM 的 UTF-8，`prompt_prefix` 中文一多就把一个收尾
    引号吞掉，报「传入的对象无效」。两条防法都钉在这儿：
      ① `bridge_config.save_config` 写出去**必须带 BOM**（PowerShell 靠它认 UTF-8）；
      ② `load_config` 用 utf-8-sig，**带不带 BOM 都得读得进**（老文件可能没 BOM）。
    """
    path = os.path.join(tmpdir, "cfg-bom.json")
    cfg = dict(wechat_bridge.DEFAULTS)
    cfg.update({"base_url": "http://x", "bot_token": "tok-bom", "account_id": "acc",
                "workdir": tmpdir})
    bridge_config.save_config(cfg, path)
    raw = open(path, "rb").read()
    check("save_config 写出来的文件带 UTF-8 BOM",
          raw.startswith(b"\xef\xbb\xbf"), raw[:8])
    back = bridge_config.load_config(path)
    check("带 BOM 的文件 load_config 读得进", back["bot_token"] == "tok-bom",
          back.get("bot_token"))

    # 没 BOM 的老文件也必须读得进（不然一升级就读不了自己的配置）
    with open(path, "wb") as fh:
        fh.write(raw[3:])
    back2 = bridge_config.load_config(path)
    check("没有 BOM 的文件也读得进（老配置不许因为这条改坏）",
          back2["bot_token"] == "tok-bom", back2.get("bot_token"))

    # 项目里那两份真配置也得带 BOM（不然 toggle.ps1 又要炸）
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("config.json", "config.example.json"):
        p = os.path.join(here, name)
        if not os.path.exists(p):
            continue
        head = open(p, "rb").read(3)
        check("%s 带 UTF-8 BOM（PowerShell 靠它认编码）" % name,
              head == b"\xef\xbb\xbf", head)


def test_config_bot_token(tmpdir):
    """bot_token 落盘一律是密文，读回来还是原来那个；旧明文配置照读。

    2026-09-30 之前它和 DeepSeek 的 Key 是两个标准：Key 走 DPAPI，token 在这个
    目录里明文躺着。同目录、同一个威胁模型，现在两边一致。这条同时钉住「兼容旧
    明文」——selftest_base._cfg 那种直接塞明文 token 的夹具全靠它。
    """
    path = os.path.join(tmpdir, "cfg-token.json")
    token = "06000000000000000000000000000000000000"
    cfg = dict(wechat_bridge.DEFAULTS)
    cfg.update({"workdir": tmpdir, "bot_token": token, "account_id": "acc"})
    bridge_config.save_config(cfg, path)
    text = open(path, "r", encoding="utf-8-sig").read()
    check("bot_token 落盘是密文（明文搜不到）", token not in text, text[:120])
    check("落盘的 bot_token 认得出来是加密格式",
          key_vault.is_protected(json.loads(text)["bot_token"]))
    check("读回来还是原来那个 token",
          bridge_config.load_config(path)["bot_token"] == token)

    # 旧配置：明文 token 必须照读，而且读配置不许改盘上的字节
    with open(path, "w", encoding="utf-8-sig") as fh:
        json.dump({"workdir": tmpdir, "bot_token": token}, fh, ensure_ascii=False)
    before = open(path, "rb").read()
    check("旧明文 token 照样读得进",
          bridge_config.load_config(path)["bot_token"] == token)
    check("读配置不改盘上的字节", open(path, "rb").read() == before)

    # 迁移：一次性、幂等、别的字段一个不动
    changed, _ = bridge_config.secure_bot_token(path)
    check("明文配置能迁移成加密", changed)
    check("迁移后还是同一个 token",
          bridge_config.load_config(path)["bot_token"] == token)
    check("迁移保留其他字段",
          json.loads(open(path, "r", encoding="utf-8-sig").read())["workdir"] == tmpdir)
    check("再迁一次什么都不做", bridge_config.secure_bot_token(path)[0] is False)


SHARED_DOC_LINES = (
    # 这些句子在几份文档里是同一句话的多个拷贝。改一处就要改全部——不然就会出现
    # 2026-09-30 撞到的那种情况：同一个「最近一次」在两份文档里是两个数字。
    ("- 主界面显示易懂的故障原因与建议：", 4),
    ("“结束对话”或窗口“进入待机”会立刻停止接收新任务和新附件", 4),
    ("托盘窗口隐藏时每 5 秒刷新", 4),
    ("- 发文件：微信接口接受后", 3),
    ("只展示自己的任务内容和历史", 3),
    ("发送“现在怎么样”", 3),
    ("- 点击窗口右上角或托盘菜单的“固定记忆”", 3),
    ("- `/result 任务编号`：", 4),
    ("- `/resend 任务编号`：", 4),
    ("- `/resolve 任务编号 checked`：", 4),
    ("- 完整结果文字在任务库内保留 7 天", 4),
)


def test_shared_doc_paragraphs():
    """同一句话的多个拷贝不许各改各的。

    2026-09-30 的实例：`- 主界面显示易懂的故障原因与建议…` 这一句在四份文档里
    各有一份拷贝，上次改的时候只改了其中几处，于是「最近一次 2031 项」和
    「2035 项」同时挂在项目里。这里把共享的那几段钉住：出现份数对得上、而且逐字
    相同。真要分头写，就把表里对应那行删掉——那是个有意的决定，不是漏改。
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    names = ("docs/reference/功能详解.md", "docs/reference/使用速查.md",
             "docs/reference/部署指南.md", "docs/reference/托盘运行中心说明.md")
    documents = {name: open(os.path.join(here, name), encoding="utf-8-sig").read().splitlines()
                 for name in names}
    for prefix, expected in SHARED_DOC_LINES:
        label = prefix[:14]
        found = {name: [line for line in document if line.startswith(prefix)]
                 for name, document in documents.items()}
        present = {name: got for name, got in found.items() if got}
        check("「%s…」出现在 %d 份文档里" % (label, expected),
              len(present) == expected, sorted(present))
        copies = {line for got in present.values() for line in got}
        check("「%s…」的各份拷贝逐字相同" % label, len(copies) == 1,
              sorted(copies)[:2])
        check("「%s…」在每份文档里只有一条" % label,
              all(len(got) == 1 for got in present.values()),
              {name: len(got) for name, got in present.items()})
