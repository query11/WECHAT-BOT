# -*- coding: utf-8 -*-
"""微信 → 本机 Claude Code → 微信。

    python wechat_bridge.py login     扫码登录，把凭据写进 config.json
    python wechat_bridge.py run       开始收消息干活
    python wechat_bridge.py check     只检查配置和 CLI，不联网
    python wechat_bridge.py probe [窗口名]
                                      把控件树探测的原始结果打出来（排查「控件树是空的」）

设计要点：
  - 收消息走长轮询（纯出站），所以不需要公网 IP、隧道、回调 URL，也没有超时限制
  - 轮询在主线程，执行在 worker 线程：长任务跑着的时候照样能收新消息
  - 一次只跑一个任务（串行），并发开多个 claude 既费额度又容易互相踩文件
  - session_id 落盘，服务重启后上下文还连着
"""

import logging
from contextlib import nullcontext
import logging.handlers
import os
import queue
import shutil
import sys
import threading
import time

import app_actions
import brightness
import parse_probe
import power
import remind
import screen_control
import volume
import wemeet
from app_actions import MediaControl
from app_launcher import AppLauncher, installed_browser_apps, find_start_menu_dirs
from bridge_standby import StandbyMixin
from bridge_progress import ProgressMixin
from bridge_admin import AdminMixin, is_quick_status, is_control_message
from bridge_delivery import DeliveryMixin
from bridge_artifacts import ArtifactMixin
from bridge_chat import ChatMixin
from bridge_config import (CONFIG_PATH, DEFAULTS, HERE, MARK_INBOX_GUARD,
                           allowed_set, load_config, make_console_safe,
                           resolve_workdir, save_config)
# allowed_roots / file_send_reason 原来也定义在本模块里，跟着 SendFileMixin 一起挪走了，
# 这里照旧 re-export——selftest.py 里 B.file_send_reason、B.allowed_roots 不用改。
from bridge_files import SendFileMixin, allowed_roots, file_send_reason
from bridge_inbox import _INBOX_QUEUE_MAX, InboxMixin, InboxStore
from bridge_media import MediaMixin
from bridge_meeting import MeetingMixin
from bridge_power import PowerMixin
from bridge_remind import RemindMixin
from bridge_screen import ScreenMixin
# 下面这些名字原来就定义在本模块里，2026-09-02 挪进 bridge_text.py 之后照旧 re-export：
# selftest.py 里 wechat_bridge.parse_chat_switch、wechat_bridge._SEND_DENY_HERE
# 这类写法一个都不用改。
from bridge_text import (HELP, MARK_SEND_FILE, _CHAT_LEAD, _CHAT_MAX_CHARS,
                         _CHAT_NOUNS, _CHAT_OFF_VERBS, _CHAT_ON_VERBS, _CHAT_TAIL,
                         _SEND_DENY_HERE, _SEND_DENY_NAMES, _SEND_LEAD,
                         _SEND_MAX_CHARS, _SEND_POLITE, _SEND_PUNCT,
                         _SEND_STRIP_LEAD, _SEND_STRIP_TAIL, _SEND_TAIL,
                         _chat_norm, _norm_path, _rstrip_polite, human_size,
                         looks_like_path, parse_chat_switch, parse_send_file,
                         strip_file_marks)
# TaskCancelled 也 re-export：selftest 里拿它当「这条是被叫停的」的判据
from claude_runner import ClaudeRunner, TaskCancelled
from task_control import TaskControl, TaskQueue, TransferCancelled
from task_feedback import compact_failure
from task_journal import TaskJournal
from runner_result import result_failed
from task_artifacts import ARTIFACT_PROMPT, strip_artifact_marks
from snapshot_lifecycle import reclaim_snapshots
from attachment_batches import AttachmentBatchStore
from bridge_batches import BatchMixin
from task_templates import TaskTemplateStore, validate_snapshot
from bridge_templates import TemplateMixin
from deepseek_runner import DeepSeekRunner
from claude_api_runner import ClaudeAPIRunner
from ilink_client import (ILinkClient, ILinkError, ILinkTimeout, MSG_TYPE_BOT,
                          qr_login)
from screen_control import ScreenControl

log = logging.getLogger("bridge")


class ReceiveStorageError(OSError):
    """Intake storage failed before any business dispatch took place."""


def setup_logging(cfg=None):
    """日志滚动写：超过 log_max_mb 就换一份，只留 log_backup_count 份旧的。

    不做轮转的话 bridge.log 会一直长（-DebugLog 下每次空轮询都写一行），
    既占地方，也等于把很久以前的记录无限期留在磁盘上。
    """
    cfg = cfg or {}
    # 先把控制台的编码错误关掉：回话里有 emoji 状态记号（✅/⏳/❌…），GBK 控制台
    # 直接打印会抛 UnicodeEncodeError，而这个窗口里那几行本来只是给人瞟一眼的
    make_console_safe()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    # 字段名没有官方文档，对不上的时候用 BRIDGE_DEBUG=1 把原始返回的键名打出来
    root.setLevel(logging.DEBUG if os.environ.get("BRIDGE_DEBUG") else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    try:
        max_mb = float(cfg.get("log_max_mb", DEFAULTS["log_max_mb"]))
    except (TypeError, ValueError):
        max_mb = DEFAULTS["log_max_mb"]
    try:
        backups = int(cfg.get("log_backup_count", DEFAULTS["log_backup_count"]))
    except (TypeError, ValueError):
        backups = DEFAULTS["log_backup_count"]
    max_bytes = max(0, int(max_mb * 1024 * 1024))
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(HERE, "bridge.log"), maxBytes=max_bytes,
        backupCount=max(0, backups), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)



def make_launcher(cfg):
    """做不出来就返回 None——开不了应用不该拖累整个桥。"""
    opts = cfg.get("app_launch") or {}
    if not opts.get("enable", True):
        return None
    try:
        return AppLauncher(extra_dirs=opts.get("extra_dirs") or [],
                           installed_apps=installed_browser_apps(),
                           start_menu_dirs=find_start_menu_dirs(), validate_shortcuts=True,
                           preferences_file=os.path.join(HERE,"app_preferences.json"),
                           allow_close=bool(opts.get("allow_close", True)),
                           cache_file=os.path.join(HERE, "app_targets.json"))
    except Exception:
        log.exception("应用启动器初始化失败，开关应用的功能这次先不开")
        return None


def make_mixer(cfg):
    """系统混音器（绝对音量）。**这是加分项**：建不起来就返回 None，音量退回按键。

    这里只是造个对象，不碰 COM（真正跑 PowerShell 要到第一次用的时候），
    所以启动阶段不会因为它变慢。
    """
    opts = cfg.get("media_control") or {}
    if not opts.get("absolute_volume", True):
        return None
    try:
        return volume.VolumeMixer()
    except Exception:
        log.exception("系统混音器初始化失败，音量只走按键")
        return None


def make_media(cfg):
    opts = cfg.get("media_control") or {}
    if not opts.get("enable", True):
        return None
    try:
        return MediaControl(
            delay=opts.get("open_delay_seconds", 6),
            vol_step=opts.get("volume_step_percent", app_actions.DEFAULT_VOL_STEP),
            mixer=make_mixer(cfg))
    except Exception:
        log.exception("播放控制初始化失败，这次先不开")
        return None


def make_brightness(cfg):
    """屏幕亮度（WMI 背光）。关掉就返回 None，那时候「亮度」这些话照旧交给 Claude。

    这里**只是造个对象**，不碰 WMI（真正跑 PowerShell 要到第一次用的时候），所以
    启动阶段不会因为它变慢。
    """
    opts = cfg.get("brightness") or {}
    if not opts.get("enable", True):
        return None
    try:
        return brightness.BrightnessControl(
            step=opts.get("step_percent", brightness.DEFAULT_STEP))
    except Exception:
        log.exception("屏幕亮度初始化失败，这次先不开")
        return None


def make_meeting(cfg):
    """进腾讯会议。关掉就返回 None，_try_meeting 那边会明说是功能关着。"""
    opts = cfg.get("meeting") or {}
    if not opts.get("enable", True):
        return None
    try:
        return wemeet.MeetingJoiner(
            url_template=opts.get("url_template") or wemeet.DEFAULT_URL,
            password_param=opts.get("password_param", wemeet.DEFAULT_PASSWORD_PARAM))
    except Exception:
        log.exception("进会功能初始化失败，这次先不开")
        return None


def make_reminders(cfg):
    """定时提醒的待办表。功能关掉就返回 None，那时候「提醒我」这些话照旧交给 Claude。"""
    opts = cfg.get("remind") or {}
    if not opts.get("enable", True):
        return None
    try:
        return remind.ReminderStore(os.path.join(HERE, "reminders.json"),
                                    max_pending=opts.get("max_pending", 10))
    except Exception:
        log.exception("定时提醒初始化失败，这次先不开")
        return None


def make_inbox_store(cfg):
    """收下来的东西的登记表。功能关着就返回 None，那时候附件照旧只回「拿不到内容」。"""
    opts = cfg.get("inbox") or {}
    if not opts.get("enable", True):
        return None
    try:
        return InboxStore(os.path.join(HERE, "inbox.json"))
    except Exception:
        log.exception("收件登记表初始化失败，这次先不收")
        return None


def make_schedule(cfg, joiner):
    """定时进会的待办表。进会本身关着、或者 timer_enable 关着，就返回 None。"""
    opts = cfg.get("meeting") or {}
    if joiner is None or not opts.get("timer_enable", True):
        return None
    try:
        return wemeet.MeetingSchedule(os.path.join(HERE, "meetings.json"),
                                      max_pending=opts.get("max_pending", 5))
    except Exception:
        log.exception("定时进会初始化失败，这次先不开")
        return None


def make_power(cfg):
    """关机 / 锁屏。做不出来返回 None，那时候这些话照旧交给 Claude。"""
    opts = cfg.get("power_control") or {}
    if not opts.get("enable", True):
        return None
    try:
        return power.PowerControl(
            shutdown_delay=opts.get("shutdown_delay_seconds", power.DEFAULT_DELAY),
            allow_shutdown=bool(opts.get("allow_shutdown", True)),
            allow_sleep=bool(opts.get("allow_sleep", True)))
    except Exception:
        log.exception("电源控制初始化失败，这次先不开")
        return None


def make_screen(cfg):
    """看画面 + 点鼠标。做不出来同样返回 None，不拖累整个桥。"""
    opts = cfg.get("screen_control") or {}
    if not opts.get("enable", True):
        return None
    try:
        return ScreenControl(
            shots_dir=os.path.join(HERE, "shots"),
            confirm_seconds=opts.get("confirm_seconds", 180),
            vision_fallback=bool(opts.get("vision_fallback", True)),
            keep_shots=opts.get("keep_shots", 1),
            refuse_titles=opts.get("refuse_titles") or [])
    except Exception:
        log.exception("看画面/点鼠标初始化失败，这次先不开")
        return None


# 这个类的方法有一多半在别的文件里（2026-09-02 拆的）：进会和定时在 bridge_meeting、
# 开关应用和音量在 bridge_media、关机锁屏在 bridge_power、发文件在 bridge_files、
# 看画面和点鼠标在 bridge_screen、对话模式在 bridge_chat、状态/日志/重读配置/队列/叫停
# 在 bridge_admin、收你发来的图片和文件在 bridge_inbox。留在本文件里的是主干——__init__
# 把零件装起来，handle 决定一条消息走哪条通道，worker 线程执行，poll_forever 长轮询。
# 几个 mixin 之间没有同名方法，所以继承顺序不影响谁生效；写成这个顺序只是照着 handle
# 里的分发次序排的，方便对着读。
class Bridge(TemplateMixin, BatchMixin, ArtifactMixin, DeliveryMixin, ProgressMixin, StandbyMixin, MeetingMixin, RemindMixin, MediaMixin, PowerMixin, SendFileMixin,
             ScreenMixin, ChatMixin, AdminMixin, InboxMixin):
    def __init__(self, cfg):
        self.cfg = cfg
        self.standby_enabled = bool(cfg.get("standby_on_start", False))
        self._awake = set()
        self._draining = set()
        self._recovery_notified = set()
        self.journal = None
        if cfg.get("task_journal_file"):
            from task_history_path import resolve_journal_path
            from task_journal import TaskJournal
            self.journal = TaskJournal(resolve_journal_path(HERE, cfg["task_journal_file"]))
        self.batch_store = AttachmentBatchStore(self.journal.path) if self.journal else None
        self.template_store = TaskTemplateStore(self.journal.path) if self.journal else None
        self._desktop_lock = threading.RLock()
        self._desktop_stop = threading.Event()
        self._reply_local = threading.local()
        self._failed_dispatches = {}
        self._receiving_blocked = False
        self._receive_warning_at = None
        # ---------- 运行状态 ----------
        # 这些**不是**从 config 派生的，`/reload` 一个都不许碰——换了就等于把正在排队
        # 的、正在等确认的东西抹掉。派生出来的那些全在 _build_config_state 里。
        self._hint_at = {}          # uid -> 上次「没带前缀」提示的时间戳，只在内存里
        self._pending_meeting = {}  # uid -> 到点了、正在等「确认」的那一场
        self._await_code = {}       # uid -> 说了时间还没给号（只在内存里）
        self._notes = {}            # uid -> 主动通知没发出去的，下条消息里补
        self._pending = {}          # 每个用户最多一条待确认的点击
        self._chat = {}             # uid -> 最后一次说话的时间戳
        self._chat_expired = set()  # 自动退出后还没告诉用户的，下一条消息里补一句
        self._last_ctx = {}         # uid -> (from_user_id, context_token)，主动通知要用
        self._seen = {}
        self._lock = threading.Lock()
        self._started_at = time.time()   # /status 用
        self._msg_count = 0              # /status 用
        # 含“已出队、准备执行”的任务；出队发布与停止快照共用 queue.mutex。
        self._running = None
        self.queue = TaskQueue()
        self._timer = None
        # 收附件那条线：轮询线程只往这个队列里塞，下载/解密/落盘在它自己的线程上做
        # （塞不进去也不阻塞，和 _enqueue 一个道理）
        self._inbox_q = queue.Queue(maxsize=_INBOX_QUEUE_MAX)
        # 等用户回答「要不要删掉最老的腾地方」的那一条（每个用户最多一条）
        self._inbox_pending = {}
        self._inbox = None

        # ---------- 登录凭据：只有这里建一次 ----------
        # `/reload` 不重建客户端：换 bot_token / account_id / base_url 要重新扫码登录，
        # 不是热更的事。能就地改的那两个（单条回复上限、轮询超时）在 _apply_config_state
        # 里直接改属性（理由见 bridge_admin 开头 ①）
        self.client = ILinkClient(
            base_url=cfg.get("base_url"), token=cfg.get("bot_token"),
            account_id=cfg.get("account_id"), state_dir=HERE,
            poll_timeout=cfg.get("poll_timeout_seconds", 35),
            max_reply_chars=cfg.get("max_reply_chars", 1800))

        # ---------- 其余全部从 config 派生 ----------
        self._apply_config(cfg)
        # worker 在配置装好之后再起：反过来的话，理论上第一条消息可能在 self.runner
        # 还不存在的时候被取出来执行
        self._worker = threading.Thread(target=self._loop_worker, daemon=True)
        self._worker.start()
        self._ensure_timer()
        self._ensure_inbox()

    def _ensure_timer(self):
        """时钟心跳按需起一次（5 秒一跳）。定时进会和定时提醒共用这一条线程。

        整块都关着时它是 None，`/reload` 把它们打开之后得补上——不然「定下的会到点了」
        永远不响。重复调用是安全的（已经有就不再起）。
        """
        if self._timer is not None:
            return
        if self.schedule is None and self.reminders is None:
            return
        # 计时单独一条线程：轮询那条会在 get_updates 里挂住 35 秒，靠它查时间的话
        # 「18:40」最多能晚半分钟才问出口
        self._timer = threading.Thread(target=self._timer_loop, daemon=True)
        self._timer.start()

    def _ensure_inbox(self):
        """收附件那条线程按需起一次。功能关着时不起（那就照旧回「拿不到内容」）。

        和 `_ensure_timer` 一样：`/reload` 把功能打开之后得补上，不然附件永远
        没人下。重复调用是安全的。
        """
        if self._inbox is not None or not getattr(self, "inbox_enable", False):
            return
        self._inbox = threading.Thread(target=self._inbox_loop, daemon=True)
        self._inbox.start()

    def _build_runner(self, cfg):
        """Build only the selected model runner; do not reload unrelated services."""
        wd = resolve_workdir(cfg)
        backend = str(cfg.get("backend", "claude")).lower()
        if backend not in ("claude", "deepseek"):
            raise SystemExit("backend 只能是 claude 或 deepseek")
        runner_type = ClaudeRunner if backend == "claude" else DeepSeekRunner
        options = {}
        session_name = ".runtime/deepseek-sessions.json" if backend == "deepseek" else "sessions.json"
        if backend == "claude" and (cfg.get("claude_api") or {}).get("enabled"):
            ca = cfg["claude_api"]
            runner_type = ClaudeAPIRunner
            options = {"key_file": os.path.join(HERE, ca.get("key_file", "")),
                       "model": ca.get("model", ""), "base_url": ca.get("base_url", ""),
                       "config_dir": os.path.join(HERE, ".runtime/claude-api-cli")}
            session_name = ".runtime/claude-api-sessions.json"
        if backend == "deepseek":
            ds = cfg.get("deepseek") or {}
            options = {"key_file": os.path.join(HERE, ds.get("key_file", ".runtime/deepseek-api.key")),
                       "model": ds.get("model", "deepseek-flash"),
                       "config_dir": os.path.join(HERE, ".runtime/deepseek-cli")}
        backend_name = "DeepSeek" if backend == "deepseek" else "Claude"
        runner = runner_type(
            **options,
            claude_bin=cfg.get("claude_bin", "claude"), workdir=wd,
            timeout_sec=cfg.get("timeout_sec", 600),
            extra_args=cfg.get("claude_extra_args"),
            session_file=os.path.join(HERE, session_name),
            keep_context=cfg.get("keep_context", True),
            context_ttl_minutes=cfg.get("context_ttl_minutes", 120),
            memory_file=(os.path.join(HERE, cfg["bot_memory_file"])
                         if cfg.get("bot_memory_file") else None))
        return runner, backend_name

    def _build_config_state(self, cfg):
        """把 config 派生的东西**全部算好装成一个 dict，一个 self 都不碰**。

        这样 `/reload` 就能「先整份算出来、算成功了再一次性换上」：中间哪一步抛
        （`resolve_workdir`、`ClaudeRunner.__init__`、几个 `make_*` 都可能拿
        **SystemExit** 报配置错），旧配置和我这边的运行状态原封不动，桥照旧能干活。
        换成边算边往 self 上写的话，抛在中间就是个半新半旧的桥——比不重载还糟。

        只有这一份：`__init__` 和 `/reload` 走的都是它。`/reload` 要是另抄一遍，
        两边迟早不一样——这个项目在「同一个事实存两份」上吃过亏（wemeet 的时间解析）。
        """
        wd = resolve_workdir(cfg)
        st = {"workdir": wd}
        st["prefix"] = (cfg.get("prefix") or "/cc").strip()
        st["require_prefix"] = bool(cfg.get("require_prefix", True))
        st["no_prefix_hint_seconds"] = max(
            0, int(cfg.get("no_prefix_hint_minutes", 10) or 0)) * 60
        st["allowed"] = allowed_set(cfg)
        st["enable_typing"] = bool(cfg.get("enable_typing", True))
        st["log_text"] = bool(cfg.get("log_message_text", False))
        st["notice_after"] = int(cfg.get("progress_notice_seconds", 30) or 0)
        # 排队上限：0 = 不限。满了直说不接（见 handle），/status 和 /queue 也报它
        st["queue_max"] = max(0, int(cfg.get("queue_max", 0) or 0))
        st["log_path"] = os.path.join(HERE, "bridge.log")
        # 这两个是 ILinkClient 的属性，不重建客户端、就地改
        st["client_tuning"] = {
            "max_reply_chars": int(cfg.get("max_reply_chars", 1800) or 1800),
            "poll_timeout": int(cfg.get("poll_timeout_seconds", 35) or 35)}
        st["runner"], st["backend_name"] = self._build_runner(cfg)
        st["launcher"] = make_launcher(cfg)
        st["media"] = make_media(cfg)
        # 屏幕亮度：笔记本内屏的背光，走 WMI。和音量一样，关掉就整条交给 Claude
        st["brightness"] = make_brightness(cfg)
        st["power"] = make_power(cfg)
        st["screen"] = make_screen(cfg)
        st["meeting"] = make_meeting(cfg)
        io_ = cfg.get("inbox") or {}
        st["inbox_enable"] = bool(io_.get("enable", True))
        st["inbox_dir"] = str(io_.get("dir") or "D:\\微信bot文件夹")
        st["inbox_max_bytes"] = int(float(io_.get("max_mb", 20) or 0) * 1024 * 1024)
        # 总配额（0 = 不限）和那句问话等多久：超了先问，回「删掉」才删（只丢回收站）
        st["inbox_quota_bytes"] = int(float(io_.get("quota_mb", 1024) or 0) * 1024 * 1024)
        st["inbox_ask_seconds"] = max(0, int(io_.get("ask_seconds", 300) or 0))
        st["inbox_store"] = make_inbox_store(cfg)
        # 定时进会。到点不直接进，先发一条问话等「确认」——你可能已经在别的会里，
        # 也可能根本没在电脑前。这几个数字都在 config.json 的 meeting 里
        st["schedule"] = make_schedule(cfg, st["meeting"])
        mo = cfg.get("meeting") or {}
        st["meeting_confirm_seconds"] = max(30, int(mo.get("confirm_seconds", 300) or 300))
        st["meeting_late_seconds"] = max(0, int(mo.get("late_minutes", 10) or 0)) * 60
        # 定时提醒：「明天九点提醒我交房租」。和定时进会共用一条计时线程，但**到点直接
        # 发**，不用等「确认」——提醒没有后果，错过它才是真损失。落盘在 reminders.json
        st["reminders"] = make_reminders(cfg)
        ro = cfg.get("remind") or {}
        st["remind_stale_seconds"] = max(0, int(ro.get("stale_hours", 24) or 0)) * 3600
        sf = cfg.get("send_file") or {}
        st["send_file_enable"] = bool(sf.get("enable", True))
        st["send_file_max_mb"] = float(sf.get("max_mb", 20) or 0)
        st["send_file_limit"] = max(1, int(sf.get("max_per_reply", 3) or 1))
        st["send_roots"] = allowed_roots(cfg, wd)
        cm = cfg.get("chat_mode") or {}
        st["chat_mode_enable"] = bool(cm.get("enable", True))
        st["chat_idle_seconds"] = max(0, int(cm.get("idle_minutes", 30) or 0)) * 60
        return st

    def _apply_config(self, cfg):
        """算 + 换上。给 `__init__` 用的；`/reload` 走的是先算后换的两步。"""
        self._apply_config_state(self._build_config_state(cfg))

    def _apply_config_state(self, st):
        """把 `_build_config_state` 算好的那份换上。**不会抛**（只 setattr）。"""
        for k, v in st.items():
            if k in ("client_tuning", "queue_max"):
                continue
            setattr(self, k, v)
        self.queue_max = st["queue_max"]
        # 队列上限是 Queue 自己的属性（0 = 不限），就地改——重建队列会把已经排进去的
        # 任务丢掉，而那正是你要保留的东西
        self.queue.maxsize = self.queue_max
        for k, v in (st.get("client_tuning") or {}).items():
            setattr(self.client, k, v)

    # ---------- 小工具 ----------

    def _peek(self, text, n=80):
        """日志里默认只留长度，不留正文。真要排查时把 log_message_text 打开。"""
        s = text or ""
        return s[:n] if self.log_text else "%d 字" % len(s)

    def _is_duplicate(self, mid, ttl=300):
        if not mid:
            return False
        now = time.time()
        with self._lock:
            for k, t in list(self._seen.items()):
                if now - t > ttl:
                    del self._seen[k]
            if mid in self._seen:
                return True
            self._seen[mid] = now
        return False

    def _enqueue(self, item, msg, recovery_id=None):
        with self._reply_scope():
            return self._enqueue_inner(item, msg, recovery_id)

    def _enqueue_inner(self, item, msg, recovery_id=None):
        """**唯一的入队口。** 返回 None = 收下了；返回一句话 = 没接，把这句话回给用户。

        满了为什么不能阻塞：`queue.Queue(maxsize=N)` 的 `put` 会一直等，而调它的
        **就是轮询线程**——一挂住，`get_updates` 那边就停了，连 `/stop`、
        `/queue clear` 这些本来用来救场的指令都进不来，等于把自己锁在门外。
        所以一律 `put_nowait`，满了就把话交给调用方去回。
        `queue_max` 填 0 时 maxsize 是 0（不限），`Full` 永远不抛，就回到以前的行为。

        启用 task_progress 时，普通任务在可入队后先回接收提示，再交给 worker。
        """
        try:
            if self.queue.full():
                raise queue.Full
            if recovery_id:
                recovered = (self.journal.claim_entry(msg.from_user_id, recovery_id)
                             if isinstance(self.journal, TaskJournal) else None)
                body = recovered['body'] if recovered else (None if isinstance(self.journal, TaskJournal)
                                                            else self.journal.claim(msg.from_user_id, recovery_id))
                if body is None:
                    return '🔕 找不到可恢复的未开始任务，或它已恢复。发 /tasks 查看。'
                msg._journal_id = recovery_id
                inputs = recovered['inputs'] if recovered else None
                snapshot = recovered.get('template') if recovered else None
                job = {}
                if inputs:
                    job['batch'] = inputs
                if snapshot:
                    job['template'] = snapshot
                if recovered and recovered.get('revision'):
                    job['revision'] = recovered['revision']
                if recovered and recovered.get('local_files'):
                    job['local_files'] = recovered['local_files']
                item = (msg, body, job or None)
            elif item[2] and item[2].get('local_files'):
                ident, added, snapshot = self.journal.add_local_files(msg.from_user_id, getattr(msg, 'message_id', ''), item[2]['local_files'])
                if not added:
                    return '这条文件任务已有记录，请查看任务记录，不会重复执行。'
                msg._journal_id = ident
                item = (msg, snapshot['body'], {'local_files': snapshot})
            elif item[2] and item[2].get('revision'):
                ident, added, snapshot = self.journal.add_revision(msg.from_user_id, getattr(msg, 'message_id', ''), item[2]['revision'])
                if not added:
                    return '这条修改任务已有记录，请查看任务记录，不会重复执行。'
                msg._journal_id = ident
                item = (msg, snapshot['instructions'], {'revision': snapshot})
            elif item[2] and item[2].get('batch'):
                snapshot = (validate_snapshot(item[2]['template'], msg.from_user_id)
                            if item[2].get('template') else None)
                ident, added, manifest = self.journal.add_batch(msg.from_user_id, getattr(msg, 'message_id', ''),
                                                               item[1], item[2]['batch']['id'],
                                                               template=snapshot)
                if not added:
                    return '🔕 这条批次任务已有记录，不自动重复执行。发 /tasks 查看。'
                msg._journal_id = ident
                job = {'batch': manifest}
                if snapshot:
                    job['template'] = snapshot
                item = (msg, item[1], job)
            elif item[2] and item[2].get('template'):
                ident, added, snapshot = self.journal.add_template(msg.from_user_id, getattr(msg, 'message_id', ''),
                                                                  item[1], item[2]['template'])
                if not added:
                    return '🔕 这条模板任务已有记录，不自动重复执行。发 /tasks 查看。'
                msg._journal_id = ident
                item = (msg, item[1], {'template': snapshot})
            elif self.journal:
                ident, added = self.journal.add(msg.from_user_id, getattr(msg, "message_id", ""),
                    "artifact_send" if item[2] and item[2].get('artifacts') else "task", self._mask(self._job_title(item[1], item[2])),
                    payload=item[1] if item[2] is None and not getattr(msg, 'attachments', None) else None)
                if not added:
                    return "🔕 这条任务已有记录，不自动重复执行。发 /tasks 查看；确认需要重试时请重新发送任务。"
                msg._journal_id = ident
            if self.cfg.get('task_progress', False) and item[2] is None and not getattr(msg, '_desktop_origin', False):
                ahead = self.queue.unfinished_tasks
                try:
                    self.reply(msg, '⏳ 任务已接收，前面还有 %d 条待处理。' % ahead if ahead else '⏳ 任务已接收，马上处理。')
                except Exception:
                    log.exception('接收提示发送失败，已接收任务继续入队')
            msg._task_control = TaskControl()
            local = getattr(self, '_reply_local', None)
            if getattr(local, 'ready', None) is not None:
                msg._dispatch_ready = threading.Event()
                local.ready.append(msg._dispatch_ready)
            self.queue.put_nowait(item)
            return None
        except queue.Full:
            if self.journal and getattr(msg, "_journal_id", None):
                if not recovery_id and item[2] and item[2].get('batch'):
                    self.journal.reject_batch(msg.from_user_id, msg._journal_id)
                else:
                    self.journal.update(msg._journal_id, "recoverable" if recovery_id else "cancelled")
            log.warning("队列满了（上限 %d 条），这条没接 user=%s",
                        self.queue_max, getattr(msg, "from_user_id", "?"))
            return ("❌ 前面已经排了 %d 条（上限 %d），这条我没接。\n"
                    "缓一会儿再发；想看排着什么发 /queue，想清掉发 /queue clear。"
                    % (self.queue.qsize(), self.queue_max))
        except Exception:
            if recovery_id and getattr(msg, '_journal_id', None):
                self.journal.update(msg._journal_id, 'recoverable')
            elif item[2] and item[2].get('batch') and getattr(msg, '_journal_id', None):
                self.journal.reject_batch(msg.from_user_id, msg._journal_id)
            raise


    # ---------- 收到一条消息 ----------

    def handle(self, msg):
        with self._reply_scope():
            if self._desktop_stop.is_set():
                return
            uid, mid = msg.from_user_id, str(msg.message_id or '')
            journal = getattr(self, 'journal', None)
            failures = getattr(self, '_failed_dispatches', {})
            if (uid, mid) in failures:
                if isinstance(journal, TaskJournal):
                    journal.fail_message(uid, mid, failures[(uid, mid)])
                else:
                    raise RuntimeError('先前分发失败仍未获得通知确认，不重复执行业务')
                return
            msg._receive_started = False
            eligible = (uid in self.allowed and mid and msg.message_type != MSG_TYPE_BOT
                        and uid != str(self.client.account_id or '')
                        and not is_control_message(msg.text, self.prefix))
            if eligible and isinstance(journal, TaskJournal):
                try:
                    state = journal.begin_message(uid, mid, self._mask(msg.text or '')[:100])
                except Exception as exc:
                    raise ReceiveStorageError('任务接收记录暂时无法保存；业务尚未分发') from exc
                if state != 'new':
                    return
                msg._receive_started = True
            try:
                result = self._handle(msg)
                if msg._receive_started:
                    journal.finish_message(uid, mid)
                return result
            except Exception:
                if msg._receive_started:
                    if not hasattr(self, '_failed_dispatches'):
                        self._failed_dispatches = {}
                    self._failed_dispatches[(uid, mid)] = self._mask(msg.text or '')[:100]
                raise

    def _handle(self, msg):
        """收到一条消息。**分发顺序是硬的**，动这里之前先读完这段：

        ① 回声 / 自己发的 / 重复的 / 不在白名单的 —— 直接丢，一句话都不回。
        ② 不用带 `/cc` 的那条链（`_dispatch_free`）：内置指令 → 到点问话的答复 →
           待确认的点击 → 对话模式开关 → 「看看画面」。这些**回复里就是这么让你说的**，
           再要求带前缀就没法用；链内顺序也固定：「明确说进会」必须排在待确认的点击
           之前，否则同一个「确认」会被点击那边抢走。
        ③ 前缀闸门：没带前缀、又不在对话模式 —— 回一句「这条我没执行」或者静默跳过。
           这一关之后才轮到「要前缀」的那条链。
        ④ 要带 `/cc` 的那条链（`_dispatch_gated`）：进会 → 音量 → 电源 → 开关应用 →
           发文件 → 点击。里头两处顺序不能动：**电源必须排在开关应用之前**（「关闭电脑」
           不能被当成去桌面上找一个叫「电脑」的图标），**音量排在开关应用之前**
           （那些句子里没有应用名，本来也不会跟它抢）。
        ⑤ 谁都没接 → 入队交给 Claude；前面还有活就先回一句在排队。

        这条顺序有几条断言盯着，拆或者挪之前先看它们：`test_power_chain`
        （「关闭电脑」是关机、不是去开一个叫「电脑」的图标；「关闭微信」照旧走开关应用）、
        `test_timer_chain`（明确说「进会」的归会议、不归点击），以及 selftest_chat.py
        那一组（免前缀链的正反两面）。

        两条链各自返回「要回的话」，由这里统一 `reply`——这样「什么时候回话」只有一处。
        """
        if msg.message_type == MSG_TYPE_BOT:
            return                                        # 自己发出去的那条回声
        if self.client.account_id and msg.from_user_id == str(self.client.account_id):
            return
        if self._is_duplicate(msg.message_id):
            log.info("重复消息，忽略 message_id=%s", msg.message_id)
            return

        uid, text = msg.from_user_id, (msg.text or "").strip()

        if not self.allowed:
            # 唯一的引导路径：iLink 的用户 ID 事先没法知道，只能等第一条消息告诉你
            log.warning("白名单为空，不执行任何任务。发消息的用户 ID 是：%s", uid)
            self.reply(msg, "白名单是空的，我不会执行任何东西。\n你的用户 ID：\n%s\n"
                            "把它填进 config.json 的 allowed_user_ids，然后重启。" % uid)
            return
        if uid not in self.allowed:
            log.warning("拒绝非白名单用户 %s：%s", uid, self._peek(text))
            return

        self._msg_count += 1        # /status 用：只数白名单里那位发来的
        # 对话模式自动退出时要主动发消息，而 send_text 需要一个收信人和 context_token
        self._last_ctx[uid] = (msg.from_user_id, msg.context_token)

        if not getattr(msg, 'attachments', None) and is_quick_status(text, self.prefix):
            return self.reply(msg, self._quick_status(uid))

        history_body = text
        if self.prefix and history_body.lower().startswith(self.prefix.lower()):
            history_body = history_body[len(self.prefix):].lstrip(' :：,，').strip()
        if self._artifact_command(msg, history_body):
            return
        if self._batch_command(msg, history_body):
            return
        if self._template_command(msg, history_body):
            return
        if self._history_command(msg, history_body):
            return
        if self.journal:
            if history_body.lower() in ("/tasks", "任务记录", "最近任务"):
                try:
                    history = self.journal.describe(uid, timeout=.05, read_only=True)
                except Exception:
                    history = '⚠️ 任务历史暂时无法读取，请稍后再查；停止和状态查询仍可使用。'
                return self.reply(msg, history)
            if not is_control_message(text, self.prefix) and uid not in self._recovery_notified:
                try:
                    unresolved = self.journal.unresolved(uid, timeout=.05, read_only=True)
                except Exception:
                    unresolved = False
                if unresolved:
                    self.reply(msg, "⚠️ 有任务在上次运行中未完成。发 /tasks 查看：未开始的文字任务可手动恢复；结果不确定的任务或中断的附件请先检查实际结果。不会自动重跑。",
                        after_send=lambda: self._recovery_notified.add(uid))
        if self._standby_gate(msg, uid, text):
            return
        if history_body.lower().split(' ', 1)[0] == '/resume':
            if not self.journal:
                return self.reply(msg, '任务记录未启用，无法恢复。')
            parts = history_body.split()
            if len(parts) != 2:
                return self.reply(msg, '请从 /tasks 复制完整的 /resume 任务编号。')
            try:
                result = self._enqueue((msg, '', None), msg, recovery_id=parts[1])
            except Exception as exc:
                log.warning('任务恢复记录不可用（%s）', type(exc).__name__)
                return self.reply(msg, '⚠️ 原任务或固定输入清单暂不可用，本次没有恢复执行。请先核对 /tasks 和本机日志。')
            return self.reply(msg, result or '✅ 已将未开始的任务重新加入队列。')

        # 主动通知（定时进会到点了、错过了）没发出去的，趁这条消息补上
        notes = self._notes.pop(uid, None)
        if notes:
            self.reply(msg, "\n\n".join(notes))

        if not text:
            # 纯附件消息（只发了张图、没写字）：交给收件那条线（它落完盘自己回话）。
            # 这条路**不看前缀**——和以前一样，那时候它就是回一句「只认文字和语音」。
            taken, note = self._inbox_start(msg, uid, "")
            if taken:
                if note:
                    self.reply(msg, note)
                return
            what = "、".join(msg.attachments) or "这种消息"
            self.reply(msg, "🔕 只认文字和语音，%s暂时不处理。" % what)
            return

        body, prefixed = text, False
        if self.prefix and body.lower().startswith(self.prefix.lower()):
            body = body[len(self.prefix):].lstrip(" :：,，").strip()
            prefixed = True

        reply = self._dispatch_free(msg, uid, body)
        if reply is not None:
            return self.reply(msg, reply)

        chatting = self.chat_mode_enable and self._in_chat(uid)
        if self.require_prefix and not prefixed and not chatting:
            note = self._expired_note(uid)
            if note:
                # 刚自动退出、用户还不知道：明说这条没执行，别让人以为桥死了
                self._hint_at[uid] = time.time()
                return self.reply(msg, note)
            hint = self._no_prefix_hint(uid)
            log.info("没带前缀，跳过%s：%s", "（回了提示）" if hint else "",
                     self._peek(text, 60))
            if hint:
                return self.reply(msg, hint)
            return
        if not body:
            return self.reply(msg, "🔕 %s 后面要跟上具体内容。" % self.prefix)
        if chatting:
            self._chat[uid] = time.time()      # 说了话就续期

        reply = self._dispatch_gated(msg, uid, body)
        if reply is not None:
            return self.reply(msg, reply)

        if msg.quoted:
            body = "（用户引用了这段内容）\n%s\n\n%s" % (msg.quoted[:2000], body)
        if msg.attachments:
            # 带正文的附件（「看看这张图」）：也交给收件那条线——它落完盘会把
            # 「正文 + 落盘路径」替我们入队，这样 Claude 拿到的路径**确实已经存在**。
            # 走到这里说明前缀那几关都过了，所以这条消息本来就是要执行的。
            taken, note = self._inbox_start(msg, uid, body)
            if taken:
                if note:
                    return self.reply(msg, note)
                return
            body += "\n\n（这条消息还附了%s，但我拿不到内容。）" % "、".join(msg.attachments)

        # 入队。**满了不阻塞**（理由见 _enqueue）：阻塞会把轮询线程挂住，
        # 那样连 /stop、/queue clear 这些本来用来救场的指令都进不来
        waiting = self.queue.qsize()
        full = self._enqueue((msg, body, None), msg)
        if full is not None:
            return self.reply(msg, full)
        log.info("入队 user=%s 队列长度=%d 内容=%s", uid, waiting + 1, self._peek(body))
        if waiting and not self.cfg.get("task_progress", False):
            self.reply(msg, "⏳ 前面还有 %d 条在排队，排到了就开工。" % waiting)

    def _probe_state(self, uid):
        """给 `/parse` 报告用的当前状态。**只看，什么都不改。**"""
        bits = []
        if self.chat_mode_enable and uid in self._chat:
            left = ""
            if self.chat_idle_seconds:
                left = "，还能免前缀 %d 分钟" % max(
                    0, int((self._chat[uid] + self.chat_idle_seconds - time.time()) // 60))
            bits.append("在对话模式里%s" % left)
        else:
            bits.append("不在对话模式")
        bits.append("有待确认的点击" if uid in self._pending else "没有待确认的点击")
        if uid in self._pending_meeting:
            bits.append("有一条问话在等你回")
        if uid in self._await_code:
            bits.append("记了时间、还等你的会议号")
        return bits

    def _parse_probe(self, body, uid):
        """「/parse 一句话」：报告这句话会走哪条通道。不是这个句式就返回 None。

        和 `/see 窗口名` 一个规矩：`/parse` 后面可以不空格（手机上打中文常常连着写），
        但不能因此把 `/parsexxx` 这种吃掉——所以要么跟着分隔符，要么跟着中文。
        """
        t = (body or "").strip()
        low = t.lower()
        for cmd in ("/parse", "/why", "/解析"):
            if low == cmd:
                return parse_probe.explain("", apps=self._probe_apps,
                                           files=self._probe_files)
            if len(t) <= len(cmd) or not low.startswith(cmd):
                continue
            nxt = t[len(cmd)]
            if nxt.split() and nxt not in " :：,，" and ord(nxt) < 128:
                continue
            return parse_probe.explain(t[len(cmd):].strip(" :：,，"),
                                       state=self._probe_state(uid),
                                       apps=self._probe_apps, files=self._probe_files)
        return None

    def _probe_apps(self, query):
        """`/parse` 用：桌面上有没有这个名字的图标。**只查已经扫过的那份清单，不重扫。**"""
        if self.launcher is None:
            return None                      # 功能没开就查不了 → 报告里会写成「软认」
        return [n for n, _p in self.launcher.match(query)]

    def _probe_files(self, path):
        """`/parse` 用：这个路径下的文件在不在、能不能发。都是 os.stat 级别的开销。"""
        if not os.path.exists(path):
            return False, ""
        return True, file_send_reason(path, self.send_roots, self.send_file_max_mb)

    def _dispatch_free(self, msg, uid, body):
        """不用带 `/cc` 的那条链。返回要回的话；None = 不是这些，交给 handle() 往下走。"""
        low = body.lower()
        # 诊断用：「/parse 一句话」把这句话喂给各通道的解析器，看它会怎么走。
        # 纯解析、不动手、不花额度——所以放在最前面，不占任何通道的位置。
        probe = self._parse_probe(body, uid)
        if probe is not None:
            return probe
        if low in ("/help", "help", "帮助", "?", "？"):
            return HELP
        if low == "/id":
            return "你的用户 ID：\n%s" % uid
        if low == "/where":
            return "工作目录：%s" % self.workdir
        if low in ("/apps", "/app", "/应用", "应用列表"):
            if self.launcher is None:
                return "❌ 开关应用的功能没启用（config.json 里 app_launch.enable）。"
            self.launcher.refresh()
            return self.launcher.describe()
        if low == "/new":
            had = self.runner.reset_session(uid)
            self._pending.pop(uid, None)
            return "上下文已清空，下一条从头开始。" if had else "本来就是新的一段。"
        if low in ("/see", "/screen", "/窗口", "看窗口"):
            return self._see()
        see_target = self._see_target(body)
        if see_target is not None:
            return self._see(see_target)
        if low in ("/windows", "/win", "/窗口列表", "有哪些窗口"):
            if self.screen is None:
                return "❌ 看画面的功能没启用（config.json 里 screen_control.enable）。"
            return ("开着的窗口：\n%s\n发「/see 名字」我去把它切到前面再看。"
                    % self.screen.describe_windows(self.screen.windows()))
        # 提醒的清单和取消也不用带前缀（和「取消定时」「/meetings」一个待遇）：
        # 新建要走 _dispatch_gated（要 /cc），但「看看有哪些」「不想要了」这两件事
        # 不该还得先打前缀
        if remind.parse_list(body):
            return self._remind_list(uid)
        if remind.parse_cancel(body)[0]:
            return self._remind_cancel(uid, body)
        if wemeet.parse_list_timer(body):
            return self._list_timers(uid)

        # 到点那条问话的答复。放在待确认的点击之前：明确说了「进会」的一律归这边，
        # 光一个「确认」而且当时还有个待确认的点击时才让给点击那边（一个字不能两头抢）
        reply = self._resolve_meeting_confirm(uid, body)
        if reply is not None:
            return reply
        # 收件那句「要腾地方吗」的答复。只认「删掉／删吧」和「不删」——**刻意不认
        # 「确认」**（它已经有两个主人了：进会到点、待确认的点击，那个字不能三头抢）
        reply = self._resolve_inbox_ask(uid, body)
        if reply is not None:
            return reply
        if wemeet.parse_cancel_timer(body):
            return self._cancel_timers(uid)

        # 待确认的点击：「确认」「取消」「2」这类回复不要求带前缀——手机上要为了确认
        # 一下再打一遍 /cc 太烦。跟这次确认无关的消息会让它作废（那时返回 None，
        # 于是**继续往下走**，后面几条链照旧有机会接）
        if uid in self._pending:
            reply = self._resolve_pending(uid, body)
            if reply is not None:
                return reply

        # 管理通道：/status /log /reload /queue /stop。刻意放在待确认的点击**之后**，
        # 所以它同样会让待确认的点击作废（和对话模式开关一个规矩）；这几个词又都是
        # 全等匹配，不会去抢「确认」。
        reply = self._try_admin(uid, body)
        if reply is not None:
            return reply

        # 对话模式的开关：故意不要求带前缀——「开始对话」这句话本身还得打 /cc 就没意义。
        # 放在待确认块之后，所以它同样会让待确认的点击作废
        reply = self._try_chat_switch(uid, body)
        if reply is not None:
            return reply

        # 「看看画面」：控件树里没有名字可用的应用（Flutter/自绘）只剩这条路，
        # 和确认一样不要求带前缀——回复里就是这么让你说的
        if screen_control.parse_look(body):
            return self._ask_look(msg, uid)
        return None

    def _dispatch_gated(self, msg, uid, body):
        """要带 `/cc`（或者在对话模式里）的那条链。返回要回的话；None = 交给 Claude。"""
        reply = self._try_meeting(uid, body)
        if reply is not None:
            return reply

        # 紧挨着进会：两个都是「定时」类，而且都靠 wemeet 那套时间解析。
        # 放在进会后面不影响谁：进会那些判据要「进会/入会/腾讯」这类词，
        # 而提醒要「提醒我」这类词，两边不重叠
        reply = self._try_remind(uid, body)
        if reply is not None:
            log.info("走提醒通道 user=%s 内容=%s", uid, self._peek(body, 40))
            return reply

        # 紧挨着提醒：也是「定时」类，也用 remind 那套时间解析，而且**共用同一张表**
        # （kind 区分）。排在提醒后面，所以带「提醒我」的一律按提醒办；两边词表不重叠
        # （这边要「帮我跑」这类词），谁先谁后其实都不抢。
        reply = self._try_task(uid, body)
        if reply is not None:
            log.info("走定时任务通道 user=%s 内容=%s", uid, self._peek(body, 40))
            return reply

        reply = self._try_volume(body)
        if reply is not None:
            log.info("走音量通道 user=%s 内容=%s", uid, self._peek(body, 40))
            return reply

        # 紧挨着音量：两条都是「设备上的一个转钮」，词表不重叠（这边「亮度/屏幕」、
        # 那边「音量/声音」）。放在开关应用前面，理由和音量一样——这些句子里没有应用名。
        reply = self._try_brightness(body)
        if reply is not None:
            log.info("走亮度通道 user=%s 内容=%s", uid, self._peek(body, 40))
            return reply

        # 放在开关应用前面：power.py 那几个解析比 app_launcher 严得多（剩字、问句、
        # 路径链接一律不认），而「关闭电脑」要是先被开关应用看到，会去找一个叫
        # 「电脑」的图标。具体走了哪个动作由 bridge_power 自己记，这里只记谁说的。
        reply = self._try_power(body)
        if reply is not None:
            log.info("走电源通道 user=%s", uid)
            return reply

        reply = self._try_app(body)
        if reply is not None:
            log.info("走应用快速通道 user=%s 内容=%s", uid, self._peek(body, 40))
            return reply

        reply = self._try_send_file(msg, uid, body)
        if reply is not None:
            log.info("走发文件通道 user=%s", uid)
            return reply

        reply = self._try_click(msg, uid, body)
        if reply is not None:
            log.info("走点击通道 user=%s 内容=%s", uid, self._peek(body, 40))
            return reply
        return None

    @staticmethod
    def _job_title(body, job):
        """这一条活儿在 /status、/queue 里叫什么。

        job 是 None 就是交给 Claude 的普通任务，body 就是那句原话；带 job 的是
        「点一下」「看看画面」「发文件」这几条自己排上去的活——它们也要有个名字，
        因为它们同样会调 runner.run()、同样能跑满 timeout_sec。
        """
        if job is None:
            return body
        if job.get('local_files'):
            template = job['local_files'].get('template')
            return '文件任务 · ' + (template['name'] if template else body)
        if job.get('revision'):
            return '继续修改 · ' + body
        if job.get('template'):
            return '任务模板 · %s · 第 %d 版%s' % (job['template']['name'], job['template']['revision'],
                   ' · 附件批次 ' + job['batch']['name'] if job.get('batch') else '')
        if job.get('batch'):
            return '附件批次 · %s · %s' % (job['batch']['name'], body)
        if job.get("sendfile"):
            if job.get('artifacts'):
                return "取回任务产物"
            return "把文件发到微信"
        if job.get("look"):
            return "看看画面（截图让它念）"
        return "看画面找坐标（点一下那条）"

    def _publish_task(self, item):
        """In-memory handoff only; called under TaskQueue's mutex."""
        msg, body, job = item
        control = getattr(msg, '_task_control', None)
        if control is None:
            control = msg._task_control = TaskControl()
        control.progress.enter('prepare')
        self._running = {"uid": getattr(msg, "from_user_id", ""),
                         "label": self._job_title(body, job),
                         "started_at": time.time(), "cancel": control,
                         "stage": "准备执行，正在保存任务状态"}

    def _take_task(self):
        if isinstance(self.queue, TaskQueue):
            return self.queue.take(self._publish_task)
        # Compatibility for callers that inject a standard Queue (offline tests).
        item = self.queue.get()
        try:
            self._publish_task(item)
        except Exception as exc:
            return item, exc
        return item, None

    def _loop_worker(self):
        while True:
            item, handoff_error = self._take_task()
            msg, body, job = item
            msg._receipt_outcome = "completed"
            started = False
            control = msg._task_control
            watcher = None
            try:
                if handoff_error is not None:
                    raise handoff_error
                msg._task_control = control
                msg._result_summary = ""
                msg._output_files = []
                if self.journal and getattr(msg, "_journal_id", None):
                    self.journal.update(msg._journal_id, "running")
                started = True
                # The control was published during dequeue, before slow storage.
                if getattr(self, "runner", None) is not None:
                    watcher = threading.Thread(target=control.watch_runner, args=(self.runner,), daemon=True)
                    watcher.start()
                control.check()
                ready = getattr(msg, '_dispatch_ready', None)
                if ready is not None:
                    while not ready.wait(.05):
                        control.check()
                    control.check()
                if job is None:
                    self._set_stage(msg, getattr(self, "backend_name", "Claude") + " 正在处理任务", notify=True)
                    self._run_task(msg, body)
                elif job.get('revision'):
                    from task_revisions import run_revision
                    run_revision(self, msg, job['revision'])
                elif job.get('local_files'):
                    from desktop_file_tasks import run_local_files
                    run_local_files(self, msg, job['local_files'])
                elif job.get('template'):
                    self._run_template(msg, body, job)
                elif job.get('batch'):
                    self._run_batch(msg, body, job['batch'])
                elif job.get("sendfile"):
                    self._run_sendfile(msg, job)
                elif job.get("look"):
                    self._set_stage(msg, "正在截图并请求分析", phase='model')
                    self._run_look(msg, job)
                else:
                    self._set_stage(msg, "正在分析屏幕，寻找操作位置", phase='model')
                    self._run_vision(msg, job)
            except TaskCancelled as exc:
                uncertain = getattr(exc, 'uncertain', False) or msg._receipt_outcome == 'uncertain'
                msg._receipt_outcome = 'uncertain' if uncertain else 'cancelled'
                msg._execution_state = msg._receipt_outcome
                message = ('⏹ 已停止本机发送，最后一个附件是否送达尚未确认。请先检查微信，避免重复发送。'
                           if uncertain else '⏹ 已叫停当前任务。此前可能已动过文件，已发出的附件不会撤回。')
                if self.journal and getattr(msg, '_journal_id', None):
                    try:
                        self.journal.update(msg._journal_id, msg._receipt_outcome)
                    except Exception:
                        log.exception('取消状态未能保存')
                        message += '\n⚠️ 取消状态未能保存；请修复后发 /tasks 检查记录，不会自动重试任务。'
                try:
                    self.reply(msg, message)
                except Exception:
                    log.exception('任务已取消，但结果通知失败')
                msg._receipt_outcome = 'uncertain' if uncertain else 'cancelled'
            except Exception:
                msg._receipt_outcome = "uncertain" if started else "queued"
                msg._execution_state = msg._receipt_outcome
                log.exception("执行任务时崩了")
                # 连回话都失败也不能让线程死掉——这是最后一道
                try:
                    self.reply(msg, "❌ 任务异常中断，结果未确认，请检查实际结果后再决定是否重发。" if started else "❌ 执行状态未能保存，任务未开始。请检查本机日志；修复后重启并发 /tasks 查看。")
                except Exception:
                    log.exception("回话也失败了")
                # Notification failure does not change whether execution began.
                msg._receipt_outcome = "uncertain" if started else "queued"
            finally:
                try:
                    self._set_stage(msg, '正在保存本次任务记录', phase='finish')
                    if started and self.journal and getattr(msg, "_journal_id", None):
                        self.journal.update(msg._journal_id, msg._receipt_outcome, summary=getattr(msg,"_result_summary",""), outputs=getattr(msg,"_output_files",[]), execution_state=getattr(msg, '_execution_state', msg._receipt_outcome))
                except Exception:
                    log.exception("任务结果未能落盘，下次启动须检查结果")
                control.finished.set()
                if watcher is not None:
                    watcher.join()  # Old cancellation must finish before runner reuse.
                with self.queue.mutex:
                    self._running = None
                self.queue.task_done()
                self._finish_draining()

    def _keep_typing(self, msg, stop):
        """微信的「正在输入」会自己消失，得定期续。纯装饰，失败不影响任务。"""
        try:
            ticket = self.client.typing_ticket(msg.from_user_id, msg.context_token)
        except ILinkError as exc:
            log.debug("拿 typing_ticket 失败（忽略）：%s", exc)
            return
        if not ticket:
            return
        while not stop.is_set():
            self.client.send_typing(msg.from_user_id, ticket, 1)
            stop.wait(8)
        self.client.send_typing(msg.from_user_id, ticket, 0)

    def _notice(self, msg, stop):
        if not stop.is_set():
            if self.cfg.get('task_progress', False):
                self.reply(msg, self._progress_notice(msg), result=False)
                next_notice = threading.Timer(60, self._notice, args=(msg, stop))
                next_notice.daemon = True
                next_notice.start()
            else:
                self.reply(msg, "⏳ 还在跑，跑完就发结果。", result=False)

    def _run_task(self, msg, body, input_context=None):
        started = time.time()
        stop = threading.Event()
        if self.enable_typing:
            threading.Thread(target=self._keep_typing, args=(msg, stop), daemon=True).start()
        timer = None
        if self.notice_after > 0:
            timer = threading.Timer(self.notice_after, self._notice, args=(msg, stop))
            timer.daemon = True
            timer.start()
        try:
            control = getattr(msg, "_task_control", None)
            if control is not None:
                control.check()
            self._set_stage(msg, '正在准备本次输入资料', phase='input')
            prompt = (self._recent_inbox_context(msg.from_user_id) if input_context is None else input_context) + ARTIFACT_PROMPT + (self.cfg.get("prompt_prefix") or "") + body
            self._set_stage(msg, getattr(self, 'backend_name', '模型') + ' 正在处理任务', phase='model')
            text = self.runner.run(msg.from_user_id, prompt)
        except TaskCancelled:
            if getattr(msg, "_task_control", None) is not None:
                raise
            # 被 /stop 叫停了。半截的改动可能已经落在文件上，所以这句话得说清楚，
            # 别让人以为「叫停了就什么都没发生」
            log.info("任务被叫停 user=%s", msg.from_user_id)
            msg._receipt_outcome = msg._execution_state = 'cancelled'
            self.reply(msg, "⏹ 已叫停（跑到一半停了）。\n"
                            "它可能已经动过盘上的文件了，想确认现在什么状态就再发一条指令。")
            return
        finally:
            stop.set()
            if timer:
                timer.cancel()
        if getattr(msg, "_task_control", None) is not None:
            msg._task_control.check()
        self._set_stage(msg, "正在整理并发送结果")
        cost = time.time() - started
        if result_failed(text):
            msg._receipt_outcome = 'failed'
            msg._execution_state = 'failed'
            error_text, _ = strip_file_marks(strip_artifact_marks(str(text))[0], self.send_file_limit)
            if self.cfg.get("log_message_text", False):
                log.error("任务失败详情：%s", self._mask(error_text))
            else:
                log.error("任务失败 backend=%s 输出=%d 字（正文日志已关闭）", getattr(self, "backend_name", "模型"), len(error_text))
            self.reply(msg, compact_failure(self._mask(error_text), getattr(self, "backend_name", "模型")))
            return
        # Claude 想发文件时会在回复里留「[[发文件]]<路径>」，那几行不该出现在微信里
        msg._execution_state = 'completed'
        text = self._capture_artifacts(msg, text)
        text, files = strip_file_marks(text or "", limit=None)
        file_delivery = getattr(msg, '_desktop_file_delivery', None)
        delivery_records = None
        if file_delivery in ('summary', 'files'):
            # Desktop choice, not model markers, determines automatic delivery.
            delivery_records = [r for r in getattr(msg, '_artifacts', []) if r.get('status') == 'valid']
            files = [r['path'] for r in delivery_records] if file_delivery == 'files' else []
        out = text.strip()
        notice = self._artifact_notice(msg)
        check_issues = any(r.get('status') != 'passed' for r in getattr(msg, '_result_checks', []))
        if any(r.get('status') != 'valid' for r in getattr(msg, '_artifacts', [])):
            msg._receipt_outcome = msg._execution_state = 'failed'
            self.reply(msg, '⚠️ 任务产物未全部通过基础检查，请先查看下面的原因。\n' + notice)
            return
        if files and not self.send_file_enable:
            msg._receipt_outcome = 'failed'
            msg._execution_state = 'failed'
            self.reply(msg, '❌ 发文件的功能是关着的，本次没有上传文件。' + ('\n\n' + notice if notice else ''))
            return
        if files:
            # Model prose may already say "sent". Release it only after every
            # requested attachment has an actual transport acceptance receipt.
            result = (self._send_files(msg, files, artifact_records=delivery_records)
                      if delivery_records is not None else self._send_files(msg, files))
            if result['accepted'] != len(files):
                if notice:
                    self.reply(msg, notice)
                return
            if not out:
                if notice:
                    self.reply(msg, notice)
                return  # Per-file receipts already describe the result.
        if not out:
            out = "✅ 执行完了，但没有文本输出。"
        if check_issues:
            out = '⚠️ 生成流程已结束，结果检查有未通过或未能检查的项目，请核对下方结论。\n\n' + out
        if cost >= 20:
            out += "\n\n⏱ 用时 %d 秒" % int(cost)
        if notice:
            out += '\n\n' + notice
        log.info("完成 user=%s 用时=%.1fs 输出=%d 字 附文件=%d", msg.from_user_id,
                 cost, len(out), len(files))
        if getattr(msg, "_task_control", None) is not None:
            msg._task_control.check()
        self.reply(msg, out, include_result_command=True)


    # ---------- 主循环 ----------

    def poll_once(self):
        """收一轮并分发，返回收到的条数。抽出来是为了能离线测。"""
        msgs, new_buf = self.client.get_updates()
        blocked = False
        for m in msgs:
            if getattr(self, '_desktop_stop', None) and self._desktop_stop.is_set():
                return 0  # A partially dispatched batch remains guarded by receipts.
            try:
                self.handle(m)
            except Exception as exc:
                recorded = False
                if m.from_user_id not in self.allowed:
                    log.exception('处理非白名单消息失败，未执行业务')
                    continue
                journal = getattr(self, 'journal', None)
                safe_retry = isinstance(exc, ReceiveStorageError)
                if not safe_retry:
                    failures = getattr(self, '_failed_dispatches', None)
                    if failures is None:
                        failures = self._failed_dispatches = {}
                    label = failures.setdefault((m.from_user_id, str(m.message_id or '')), self._mask(getattr(m, 'text', '') or '')[:100])
                    if isinstance(journal, TaskJournal):
                        try:
                            journal.fail_message(m.from_user_id, m.message_id, label)
                            recorded = True
                        except Exception:
                            pass
                now = time.monotonic()
                previous = getattr(self, '_receive_warning_at', None)
                if previous is None or now - previous >= 30:
                    self._receive_warning_at = now
                    log.exception('消息处理或接收记录失败（%s），未确认的批次不会提交游标', type(exc).__name__)
                notice = ('❌ 这条任务尚未接收，正在等待本机存储恢复；暂时不要重复发送。停止和状态查询仍可使用。' if safe_retry else
                          '❌ 处理这条消息时出错了，结果未确认。请先检查实际状态，再决定是否重发；细节见本机 bridge.log。')
                try:
                    notified = self.reply(m, notice) is not False
                except Exception:
                    notified = False
                # A configured journal must durably record the disposition.
                # Notification alone cannot compensate for a failed task write.
                if safe_retry or (isinstance(journal, TaskJournal) and not recorded) or (not journal and not notified):
                    blocked = True
        self._receiving_blocked = blocked
        if not blocked:
            self.client.commit_cursor(new_buf)
        return len(msgs)


    def poll_forever(self):
        # Run only when starting the service loop, never on fixture construction.
        # The desktop heartbeat is already running; recovery is strictly bounded.
        reclaim_snapshots(os.path.join(HERE, '.runtime', 'snapshot-lifecycle'),
                          allowed_roots=[HERE, self.workdir], max_records=32, max_seconds=.15)
        log.info("开始长轮询：base_url=%s account_id=%s", self.client.base_url,
                 self.client.account_id or "（空）")
        log.info("工作目录=%s 触发前缀=%s 白名单=%d 人", self.workdir,
                 self.prefix if self.require_prefix else "（不要求）", len(self.allowed))
        if self.chat_mode_enable and self.require_prefix:
            log.info("对话模式可用：发「开始对话」免前缀，%s",
                     "空闲 %d 分钟自动退出" % (self.chat_idle_seconds // 60)
                     if self.chat_idle_seconds else "只能手动「结束对话」")
        backoff = 1
        while not self._desktop_stop.is_set():
            try:
                got = self.poll_once()
                if hasattr(self, "desktop_runtime"):
                    self.desktop_runtime.connected()
                backoff = 1
                self._expire_chat_modes()
                if self._receiving_blocked:
                    self._desktop_stop.wait(2)  # Bounded storage retry, no busy loop.
                if not got:
                    time.sleep(0.5)
            except ILinkTimeout:
                # 长轮询本来就是挂住等消息，超时是常态，不退避也不刷日志。
                # 这里是每 ~35 秒必然经过的点，顺手查一次对话模式有没有空闲超时
                self._expire_chat_modes()
                backoff = 1
                continue
            except (ILinkError, OSError) as exc:
                text = str(exc)
                if hasattr(self, "desktop_runtime"):
                    self.desktop_runtime.failed(exc, "HTTP 401" in text or "HTTP 403" in text)
                if "HTTP 401" in text or "HTTP 403" in text:
                    log.error("凭据被拒（%s）。重新跑一次 `python wechat_bridge.py login`。", text)
                    return
                log.warning("收消息失败，%d 秒后重试：%s", backoff, text)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)


# ---------- 子命令 ----------

def cmd_login(cfg):
    print("正在向微信申请二维码……")
    got = qr_login(base_url=cfg.get("base_url"), out_dir=HERE)
    cfg.update({"base_url": got["base_url"], "bot_token": got["bot_token"],
                "account_id": got["account_id"]})
    save_config(cfg)
    # 二维码和原始返回用完就删，别留在目录里（图片格式不定，所以按前缀清）
    for name in os.listdir(HERE):
        if name.startswith("ilink_qrcode.") or name == "ilink_login_raw.json":
            try:
                os.remove(os.path.join(HERE, name))
            except OSError:
                pass
    print("登录成功，凭据已写进 config.json。account_id=%s" % (got["account_id"] or "（空）"))
    print("接着跑：python wechat_bridge.py run")


def cmd_check(cfg):
    """把配置和这台机器现在能不能干活打一遍。**只读**，不改任何东西。

    拆成了下面那几个 `_check_*`（2026-09）：原来是一个 140 行的函数，加一层功能就长一
    截，而且每一段「ok 怎么算」都混在同一串 if/else 里。拆开之后谁改哪一层就只看哪一段。
    **输出顺序和文字一个字都没变**，自测里那条「六段顺序」的断言管着这件事。
    """
    wd = resolve_workdir(cfg)
    ok = _check_basics(cfg, wd)
    # 用 `&=` 而不是 `and=`：后者会短路，后面几段就都不打印了
    ok &= _check_launcher(cfg)
    ok &= _check_media(cfg)
    ok &= _check_brightness(cfg)
    ok &= _check_power(cfg)
    ok &= _check_send_file(cfg, wd)
    ok &= _check_inbox(cfg)
    ok &= _check_remind(cfg)
    ok &= _check_screen(cfg)
    return 0 if ok else 1


def _check_basics(cfg, wd):
    """工作目录、claude CLI、登录凭据、白名单，以及前缀/对话模式/日志/上下文。"""
    ok = True
    print("[OK]   工作目录 %s" % wd)
    backend = str(cfg.get("backend", "claude")).lower()
    print("[INFO] 模型后端：%s（本机工具执行器：Claude Code）" % backend)
    if backend not in ("claude", "deepseek"):
        print("[WARN] backend 只能是 claude 或 deepseek")
        ok = False
    elif backend == "deepseek":
        ds = cfg.get("deepseek") or {}
        try:
            DeepSeekRunner(workdir=wd, key_file=os.path.join(HERE, ds.get("key_file", ".runtime/deepseek-api.key")),
                           model=ds.get("model", "deepseek-flash"))
            print("[OK]   DeepSeek 本机配置有效；此项未调用 API")
        except SystemExit as exc:
            print("[WARN] %s" % exc)
            ok = False

    bin_cfg = cfg.get("claude_bin", "claude")
    probe = bin_cfg[0] if isinstance(bin_cfg, (list, tuple)) and bin_cfg else bin_cfg
    found = shutil.which(str(probe))
    if found:
        rest = " ".join(str(x) for x in bin_cfg[1:]) if isinstance(bin_cfg, (list, tuple)) else ""
        print("[OK]   claude 可执行文件 %s %s" % (found, rest))
    else:
        ok = False
        print("[WARN] 找不到 %s。装好 Claude Code CLI，或把 claude_bin 填成绝对路径；"
              "\n       .cmd 包装器跑不起来的话，可以写成数组：[\"node\", \"C:\\\\...\\\\cli.js\"]"
              % probe)

    if cfg.get("bot_token"):
        print("[OK]   已有 bot_token（account_id=%s）" % (cfg.get("account_id") or "空"))
    else:
        ok = False
        print("[WARN] 还没登录，先跑 python wechat_bridge.py login")

    n = len([x for x in (cfg.get("allowed_user_ids") or []) if str(x).strip()])
    if n:
        print("[OK]   白名单 %d 人" % n)
    else:
        # 白名单为空不算「配置坏了」：这是第 3 步之前的正常状态，程序会回你用户 ID
        print("[WARN] 白名单为空：现在谁发消息都不会执行，只会回一句用户 ID。"
              "\n       这是引导用的——把回给你的那个 ID 填进 allowed_user_ids 再重启。")

    print("[INFO] 触发前缀 %s；单条回复上限 %s 字；任务超时 %s 秒"
          % (cfg.get("prefix") if cfg.get("require_prefix") else "（不要求）",
             cfg.get("max_reply_chars"), cfg.get("timeout_sec")))
    _cm = cfg.get("chat_mode") or {}
    if cfg.get("require_prefix") and _cm.get("enable", True):
        _idle = int(_cm.get("idle_minutes", 30) or 0)
        print("[INFO] 对话模式：微信里发「开始对话」之后免前缀，「结束对话」退出；%s。"
              "\n       状态只在内存里，这个进程一关就回到要前缀的状态。"
              % ("空闲 %d 分钟自动退出" % _idle if _idle else "不会自动退出"))
    print("[INFO] 透传给 CLI 的参数：%s" % (cfg.get("claude_extra_args") or []))
    if cfg.get("log_message_text"):
        print("[WARN] bridge.log 会记下消息正文（log_message_text=true）。"
              "\n       排查完记得改回 false，日志里就只留字数。")
    else:
        print("[OK]   bridge.log 不记消息正文，只记字数。"
              "\n       例外：用 -DebugLog / BRIDGE_DEBUG=1 启动时，收发的原始 JSON 会整条写进日志。")
    print("[INFO] 日志滚动：单份最大 %s MB，另留 %s 份旧的（bridge.log.1 …）"
          % (cfg.get("log_max_mb"), cfg.get("log_backup_count")))
    if not cfg.get("keep_context", True):
        print("[INFO] 上下文：每条都是新会话（keep_context=false），追问「刚才那个」不管用")
    elif float(cfg.get("context_ttl_minutes") or 0) > 0:
        print("[INFO] 上下文：续接，但隔 %s 分钟没说话就当新的一段"
              % cfg.get("context_ttl_minutes"))
    else:
        print("[INFO] 上下文：一直续接，不过期（context_ttl_minutes=0）")
    return ok


def _check_launcher(cfg):
    """检查桌面、开始菜单和固定识别的应用启动项。"""
    launcher = make_launcher(cfg)
    if launcher is None:
        print("[INFO] 开关应用：已关闭")
        return True
    if not launcher.dirs and not launcher.start_menu_dirs and not launcher.names():
        print("[WARN] 开关应用：没找到应用目录。"
              "\n       把桌面路径填进 config.json 的 app_launch.extra_dirs 再试。")
        return False
    print("[OK]   开关应用：扫描 %s，发现 %d 个有效启动项%s"
          % ("、".join(launcher.dirs + launcher.start_menu_dirs), len(launcher.names()),
             "，允许关闭" if launcher.allow_close else "，只许打开"))
    if launcher.names():
        print("       %s" % "、".join(launcher.names()))
    return True


def _check_media(cfg):
    """播放控制（系统媒体键）与音量；绝对音量是加分项，没开也能用。"""
    media = make_media(cfg)
    if media is None:
        print("[INFO] 播放控制：已关闭")
        return True
    print("[OK]   播放控制：系统媒体键（播放、暂停、上一首、下一首、停止），"
          "开完应用等 %d 秒再按" % int(media.delay))
    print("       音量：加/减一次约 %d%%（按 %d 下音量键），静音是开关键"
          % (media.vol_actual, media.presses))
    if getattr(media, "_mixer", None) is None:
        print("       绝对音量（音量调到 30%／音量多少）：没开，只有相对调整")
    else:
        print("       绝对音量：开着（音量调到 30%、音量多少）。第一次用要 1~3 秒"
              "编一段 C#，失败会自动退回音量键")
    return True


def _check_brightness(cfg):
    """屏幕亮度。**这条路只认笔记本内屏**（WMI 的背光），所以真去读一次当前值——
    读得到就说明这台机器能做，比只报配置有用得多。"""
    br = make_brightness(cfg)
    if br is None:
        print("[INFO] 屏幕亮度：已关闭")
        return True
    r = br.get()
    if r is None:
        print("[INFO] 屏幕亮度：**这台机器读不到**（WMI 里没有 WmiMonitorBrightness，"
              "多半是没有内屏、或者背光不归系统管）；说「亮度」会照实说做不到")
        return True
    print("[OK]   屏幕亮度：说「亮度调到 50%%」「亮度多少」「屏幕亮一点」都行；"
          "现在 %d%%（认到 %d 块屏）" % (r["after"], int(r.get("monitors") or 1)))
    print("       一次「亮一点」调 %d%%；下限留在 %d%%（再低屏幕就看不见了，"
          "而人还在手机那头）。只管内屏，外接显示器动不了"
          % (br.step, brightness.MIN_BRIGHTNESS))
    return True


def _check_power(cfg):
    """关机、锁屏、睡眠。状态查一次要 ~0.3 秒（起一次 tasklist），值得：马上就知道通不通。"""
    pw = make_power(cfg)
    if pw is None:
        print("[INFO] 关机/锁屏/睡眠：已关闭")
        return True
    print("[OK]   锁屏：说「锁屏」就锁（LockWorkStation，不用额外权限）")
    if pw.allow_shutdown:
        print("       关机：开着，不说时间就是 %s后关；这段时间里说「取消关机」还能拦住"
              % power._dur(pw.shutdown_delay))
    else:
        print("       关机：已关闭（power_control.allow_shutdown）")
    print("       解锁：不做（2026-09-03 砍掉的，要提权）。锁了只能你自己到电脑前开。")
    # 睡眠能力问一次 `powercfg /a`（也是 ~0.3 秒）。**只认「可用」那一段**：
    # 「不可用」那一段里照样会出现「待机」「休眠」这些词（本机就有一条被策略禁掉的
    # 「待机(S0 低电量待机) 连接的网络」），全文搜会把禁掉的读成可用。
    if pw.allow_sleep:
        try:
            st = power.read_sleep_states()
        except Exception as exc:                          # noqa: BLE001
            print("       睡眠：问不出这台机器行不行（powercfg /a 失败：%s）" % exc)
        else:
            if st.get("sleep"):
                print("       睡眠：开着——说「睡眠」就进待机"
                      "（回话先发给你，8 秒后生效；唤醒按一下电源键）")
            else:
                print("       睡眠：**这台机器没有可用的「待机」**"
                      "（它不在 powercfg /a 的「可用」那一段里），说「睡眠」会被拦下来")
            print("       休眠：%s"
                  % ("开着——说「休眠」就写进 hiberfil.sys 再断电"
                     if st.get("hibernate") else
                     "不可用（powercfg /a 的「可用」那一段里没有它）"))
    else:
        print("       睡眠/休眠：已关闭（power_control.allow_sleep）")
    print("       现在：%s" % pw.status()[0].replace("\n", " "))
    return True


def _check_send_file(cfg, wd):
    """把本机文件发回微信：可发范围是 workdir 加上 --add-dir 的那些目录。"""
    sf = cfg.get("send_file") or {}
    if not sf.get("enable", True):
        print("[INFO] 发文件回微信：已关闭")
        return True
    roots = allowed_roots(cfg, wd)
    print("[OK]   发文件回微信：单个最大 %s MB，一条回复最多 %s 个；"
          "可发的目录 %s" % (sf.get("max_mb", 20), sf.get("max_per_reply", 3),
                            "、".join(roots) or "（没有）"))
    print("       凭据类文件（.env/.pem/config.json 等）一律拒发；"
          "微信自己对文件大小的限制没有文档，max_mb 是我定的保守值")
    if MARK_SEND_FILE not in (cfg.get("prompt_prefix") or ""):
        print("[WARN] prompt_prefix 里没有 %s 那句说明，Claude 不知道自己能发文件。"
              "\n       说不清路径的时候（「桌面那个 ppt 发给我」）就发不出去，"
              "只有直接给路径才行。" % MARK_SEND_FILE)
    return True


def _check_inbox(cfg):
    """收你发来的图片和文件：只看配置和那个文件夹在不在，不联网。"""
    opts = cfg.get("inbox") or {}
    if not opts.get("enable", True):
        print("[INFO] 收你发来的图片和文件：已关闭（附件还是只回「拿不到内容」）")
        return True
    out_dir = str(opts.get("dir") or "D:\\微信bot文件夹")
    max_mb = opts.get("max_mb", 20)
    st = make_inbox_store(cfg)
    if st is None:
        print("[WARN] 收附件：开着，但登记表建不起来，附件会收不下来（看 bridge.log）")
        return True
    print("[OK]   收你发来的图片和文件：单个最大 %s MB；落在 %s"
          % (max_mb, out_dir))
    if os.path.isdir(out_dir):
        n = len(st.all())
        print("       那个文件夹在；登记在册 %d 个，共 %s" % (n, human_size(st.total_bytes())))
        quota = int(float(opts.get("quota_mb", 1024) or 0) * 1024 * 1024)
        if quota:
            left = quota - st.total_bytes()
            print("       总配额 %s，还剩 %s；满了先问一句「删掉最老的腾地方吗」，"
                  "回「删掉」才删（只删登记在册的，丢回收站）"
                  % (human_size(quota), human_size(max(0, left))))
        else:
            print("       总配额不限（inbox.quota_mb = 0）")
    else:
        print("       文件夹还没建——收到第一条附件时会自动建")
    print("       只写这个文件夹，只存不动（不执行、不自动打开）")
    if MARK_INBOX_GUARD not in (cfg.get("prompt_prefix") or ""):
        print("[WARN] 收附件那句防注入说明不在 prompt_prefix 里（少了「%s」），"
              "Claude 可能把文件里的字当指令执行。" % MARK_INBOX_GUARD)
    return True


def _check_remind(cfg):
    """定时提醒。它和别的层不一样的是东西**落盘**，所以顺手报一下现在记了几条。"""
    st = make_reminders(cfg)
    if st is None:
        print("[INFO] 定时提醒：已关闭")
        return True
    print("[OK]   定时提醒：说「明天九点提醒我交房租」就记下；最多 %d 条，现在 %d 条；"
          "落盘 %s" % (st.max_pending, len(st.all()), st.path))
    print("       到点直接发微信（不问确认，错过了才是真损失）；那一刻开关得开着。"
          "\n       说「每天早上九点提醒我吃药」是**重复**的：到点发一条，"
          "然后自己排到第二天（桥中途关过也只补一次）。"
          "\n       定时任务共用这张表：说「明天九点帮我跑：把那句交给 Claude 去跑」"
          "到点是**真执行**（花额度），太旧的（超过 stale_hours）我不跑、只说明一句。"
          "\n       「看提醒」看清单、「取消提醒」删掉（都不用带 /cc）")
    return True

def _check_screen(cfg):
    """看画面、点鼠标。Linux 上读窗口必然失败，那条 WARN 是正常的。"""
    screen = make_screen(cfg)
    if screen is None:
        print("[INFO] 看画面/点鼠标：已关闭")
        return True
    print("[OK]   看画面/点鼠标：只看当前活动窗口，每次点击都要在微信里回「确认」"
          "（%d 秒不回作废）" % screen.confirm_seconds)
    print("       看图兜底：%s；截图最多留 %d 张，在 %s"
          % ("开（会把当前窗口的画面送到模型服务端）" if screen.vision_fallback
             else "关（控件树找不到就算了）", screen.keep_shots, screen.shots_dir))
    win = screen.active()
    if win:
        print("       现在的前台窗口：%s（%d,%d %dx%d）%s"
              % (win.get("title") or "（没有标题）", win.get("left", 0),
                 win.get("top", 0), win.get("width", 0), win.get("height", 0),
                 "——这是桌面，不是应用窗口" if screen.is_desktop(win) else ""))
    else:
        print("       [WARN] 现在读不到前台窗口（Linux 上必然如此；Windows 上"
              "多半是这个终端没有窗口标题）")
    wins = screen.windows()
    print("       开着的应用窗口 %d 个%s"
          % (len(wins), "：" + "、".join((w.get("title") or "?")
                                        for w in wins[:8]) if wins else ""))
    dpi = screen.dpi_problem()
    if dpi:
        print("       [WARN] %s" % dpi)
        return False
    print("       坐标体系：窗口矩形、截图、鼠标都在物理像素上（DPI 感知已生效）")
    return True


def cmd_run(cfg):
    if not cfg.get("bot_token"):
        raise SystemExit("还没登录。先跑：python wechat_bridge.py login")
    bridge = Bridge(cfg)
    from bridge_runtime import Runtime
    bridge.desktop_runtime = Runtime(bridge)
    bridge.desktop_runtime.start()
    try:
        bridge.poll_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，退出。")
        bridge._desktop_stop.set()
    finally:
        bridge.desktop_runtime.finish()
    return 0


def cmd_probe(cfg, argv):
    """把某个窗口的控件树探测过程原原本本打出来。排查「控件树是空的」专用。

    微信那头只能看到结论，这里能看到实情：PowerShell 有没有报错、摸到多少节点、
    类型分布长什么样、以及那些节点的名字。真机上两次「控件树是空的」都是被
    「查失败」和「真没有」混在一起坑的，所以这条命令值得单独存在。
    """
    scr = cfg.get("screen_control") or {}
    screen = ScreenControl(
        shots_dir=os.path.join(HERE, "shots"),
        confirm_seconds=scr.get("confirm_seconds", 180),
        vision_fallback=bool(scr.get("vision_fallback", True)),
        keep_shots=scr.get("keep_shots", 1),
        refuse_titles=scr.get("refuse_titles") or [])
    target = " ".join(argv[2:]).strip()
    if target:
        cands = screen.find_window(target)
        if not cands:
            print("没有窗口叫「%s」。现在开着的是：" % target)
            print(screen.describe_windows(screen.windows()))
            return 1
        win = cands[0]
        okk, cur = screen.activate(win)
        print("切窗口：%s（现在最前面是「%s」）"
              % ("成了" if okk else "系统没让它到最前面",
                 (cur or {}).get("title") or "读不出来"))
    else:
        win = screen.active()
    if not win:
        print("读不到窗口。")
        return 1
    print("窗口：%s  hwnd=%s  %d,%d %dx%d"
          % (win.get("title"), win.get("hwnd"), win.get("left", 0),
             win.get("top", 0), win.get("width", 0), win.get("height", 0)))
    print("缩放自查：%s" % (screen.dpi_problem() or "1.0（坐标体系一致）"))
    try:
        raw = screen_control._default_prober(win["hwnd"])
    except Exception as exc:
        print("探测这一步整个失败了：%r" % (exc,))
        print("——这就是微信里那句「控件树是空的」的真正原因。")
        return 1
    if isinstance(raw, dict):
        print("PowerShell 里的报错：%s" % (raw.get("error") or "（没有）"))
        print("节点：总共 %s 个，摸了 %s 个" % (raw.get("total"), raw.get("scanned")))
        print("类型分布：%s" % (raw.get("kinds") or "（空）"))
        for key in ("items", "wide"):
            rows = raw.get(key) or []
            print("%s：%d 条" % ("标准可点控件" if key == "items" else "自绘兜底候选",
                                 len(rows)))
            for r in rows[:20]:
                print("   %-14s %-10s %s,%s %sx%s"
                      % (str(r.get("name"))[:14], r.get("type"), r.get("x"),
                         r.get("y"), r.get("w"), r.get("h")))
    else:
        print("探测器返回的是列表（%d 条），不是新版的诊断结构" % len(raw or []))
    info = {}
    els = screen.elements(win["hwnd"], info)
    print("\n整理之后：%d 个候选，info=%s" % (len(els), info))
    print(screen.describe(win, els, info=info))
    return 0


def main(argv):
    action = (argv[1] if len(argv) > 1 else "").lower()
    if action not in ("login", "run", "check", "probe"):
        print(__doc__)
        return 2
    # 先读配置再配日志：轮转的大小和份数写在配置里。配置本身读不了的话
    # load_config 会直接 SystemExit 并打印原因，那时候还没有日志也无所谓。
    cfg = load_config(for_login=True) if action == "login" else load_config()
    setup_logging(cfg)

    if action == "login":
        cmd_login(cfg)
        return 0
    if action == "check":
        return cmd_check(cfg)
    if action == "probe":
        return cmd_probe(cfg, argv)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

