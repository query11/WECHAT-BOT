# -*- coding: utf-8 -*-
"""管理通道：在微信里看桥的状态、看日志、让它重读配置、看队列、叫停正在跑的任务。

单独一个混入类，因为这几件事管的都是**桥自己**，不是「指挥电脑干活」——和
bridge_media / bridge_power 那些不是一类。全部**不用带 `/cc`**（和 `/help`、`/new`、
`/see` 一个待遇）：出问题的时候人正在着急，再让你打一遍前缀没有道理。

三条硬规矩，都是先想清楚才这么写的：

① **`/reload` 绝不退出进程。** 改完 `config.json` 想让它生效，本来要么回电脑前双击
   开关，要么让桥自己重启；后者一旦起不来（配置写错、零件拉不起来）你就只能走到
   电脑前——和当初砍掉「远程解锁」是同一类风险（见部署指南）。所以这里只做「就地
   重读」：先把新的整份算出来（`Bridge._build_config_state`，**不碰 self**），算成功了
   才一次性换上去。中间哪一步抛，旧配置一个字都不动。
   这一步非做不可的原因是：`load_config` 和 `ClaudeRunner.__init__` 都是拿
   **SystemExit** 报配置错的，而 SystemExit 不属于 Exception——只 catch Exception
   的话，一个写坏的 config.json 就能把桥弄死，那 `/reload` 反倒成了新风险。
   另外两条守卫：白名单重载成空的、或者新白名单里没有你，都拒绝——那等于把自己锁在
   门外，而 `/reload` 这个指令本身也在门外。

② **`/log` 发出去之前抹凭据。** 日志本身不记 token（`setup_logging` 只写「谁发的、
   几个字、跑了多久」），但 HTTP 出错那几行带的是服务端原话，抹一遍更保险。

③ **叫停收的是几个明确说法，不收光秃秃的「停」「停止」。** 命中就是真把活儿杀掉
   （写了一半的文件留在盘上），而「停一下」「停下」在正常一句话里出现得太频繁。
   宁可漏认：漏认的代价是它没被叫停，误认的代价是正要紧的活儿没了。

指令词明确匹配，单条队列操作严格校验完整编号和参数，不吃子串也不做模糊匹配。

多行回话（/status、/queue、/reload）刻意写成**一个**以记号开头的字面量、变量部分用
`%` 填，而不是 `lines.append(...)` 一行行拼。两个理由：一是 selftest_style 只认句首，
拼出来的话它管不着；二是这样「这条回话从哪个记号开始」在代码里是能一眼看见的。
"""
import logging
import os
import queue
import re
import time
from task_control import TaskControl, TaskQueue

from bridge_config import CONFIG_PATH, allowed_set, load_config

log = logging.getLogger("bridge")

# ---------- 词表 ----------
# 看状态 / 看日志 / 重读配置 / 看队列（都在免费链里，不用带 /cc）
_STATUS_WORDS = ("/status", "/状态", "状态", "桥的状态")
_QUICK_STATUS_WORDS = ('/brief', '现在怎么样', '现在什么情况', '还在处理吗', '处理到哪了', '进度怎么样')


def is_quick_status(text, prefix=''):
    body = (text or '').strip()
    if prefix and body.lower().startswith(prefix.lower()):
        body = body[len(prefix):].lstrip(' :：,，').strip()
    return body.rstrip('?？。!！').strip().lower() in _QUICK_STATUS_WORDS


_LOG_WORDS = ("/log", "/日志")
_RELOAD_WORDS = ("/reload", "/重载")
_QUEUE_WORDS = ("/queue", "/队列")
# 「清空队列」只认这几个整句，不认光秃秃的「清空」——那更像在说别的事
_QUEUE_CLEAR_WORDS = ("/queue clear", "/queue clear all", "清空队列", "队列清空")
# 叫停：只停正在跑的那一条（排着的留着，另有 /queue clear）
_STOP_WORDS = ("/stop", "叫停", "别跑了", "取消任务")
# 全停：连排着的一起清（是「全」这个字自己要来的语义，单独说 /stop 不会清队列）
_STOP_ALL_WORDS = ("/stopall", "/stop-all", "全停", "都别干了")


def is_control_message(text, prefix=''):
    from batch_dialog import parse_batch_dialog
    body = (text or '').strip()
    if prefix and body.lower().startswith(prefix.lower()):
        body = body[len(prefix):].lstrip(' :：,，').strip()
    low = body.lower()
    return (low in _STOP_WORDS + _STOP_ALL_WORDS or parse_admin(body) is not None
            or is_quick_status(body) or parse_batch_dialog(body) is not None or low in ('/tasks', '任务记录', '最近任务')
            or low in ('查看刚才的产物', '把刚才的产物发给我', '把刚才生成的文件发给我')
            or (low.split()[0] if low.split() else '') in
            ('/result', '/resend', '/resolve', '/resume', '/artifacts', '/getfile', '/batch', '/template'))

# /log 后面能跟行数。默认给 30 行——一屏左右，够看出刚才出了什么事
LOG_DEFAULT_LINES = 30
LOG_MAX_LINES = 100
# /log 一次最多读回来这么多字节，以及最多发出去这么多字（回复本身还会再分片）
LOG_TAIL_BYTES = 256 * 1024
LOG_MAX_CHARS = 4000

# 抹凭据用的形状。前两条是精确值替换（见 _mask），这条是兜底：
# 万一哪天有新的东西被写进日志，形状也认得出
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"[0-9a-fA-F]{16,}@im\.bot"),
    re.compile(r"bot[_-]?token\s*[:=]\s*\S+", re.IGNORECASE),
)


def parse_lines(body):
    """`/log 50` → 50；光 `/log` → 默认行数；压根不是看日志 → None。

    和 `/parse`、`/see 窗口名` 一个规矩：后面可以不空格（手机上连着打很常见），
    但不能因此把 `/logx` 这种吃掉——那种跟着的 ASCII 字母说明是别的词。
    而「说了 /log、行数写得不像话」（`/log abc`）**不当没说过**：按默认行数来。
    静默丢掉是最不该的——你明明喊了这条指令，它却一声不响。
    """
    t = (body or "").strip()
    for cmd in _LOG_WORDS:
        if not t.lower().startswith(cmd.lower()):
            continue
        rest = t[len(cmd):]
        if not rest:
            return LOG_DEFAULT_LINES
        if rest[0] in " :：,，":
            rest = rest.lstrip(" :：,，").strip()
        elif not rest[0].isdigit():
            if ord(rest[0]) < 128:
                continue               # `/logx`、`/logs`：是别的词，别吞
            return LOG_DEFAULT_LINES   # `/log…中文`：当它只是没写行数
        if not rest:
            return LOG_DEFAULT_LINES
        if not rest.isdigit():
            return LOG_DEFAULT_LINES   # `/log abc`、`/log 12abc`
        return max(1, min(LOG_MAX_LINES, int(rest)))
    return None


def parse_admin(body):
    """认出管理指令。返回 (动作, 参数)；None = 不是管理指令，交给后面的通道。

    单条队列操作只认完整编号，修改正文保留原大小写和换行。
    """
    low = (body or "").strip().lower()
    if low in _QUEUE_CLEAR_WORDS:
        return ("queue_clear", None)
    if low in _STATUS_WORDS:
        return ("status", None)
    if low in _RELOAD_WORDS:
        return ("reload", None)
    if low in _QUEUE_WORDS:
        return ("queue", None)
    command = re.match(r'^/(?:queue|队列)\s+(\S+)(?:\s+(.*))?$',
                       (body or '').strip(), re.IGNORECASE | re.DOTALL)
    if command:
        action, rest = command.group(1).lower(), command.group(2) or ''
        parts = rest.split(None, 1)
        if action not in ('cancel', 'edit', 'move') or not parts:
            return ('queue_help', None)
        ident = parts[0].lower()
        if not re.fullmatch(r'[0-9a-f]{32}', ident):
            return ('queue_help', None)
        if action == 'cancel' and len(parts) == 1:
            return ('queue_manage', (action, ident, None))
        if action == 'edit' and len(parts) == 2 and parts[1].strip():
            return ('queue_manage', (action, ident, parts[1]))
        if action == 'move' and len(parts) == 2 and re.fullmatch(r'[1-9][0-9]{0,8}', parts[1]):
            return ('queue_manage', (action, ident, int(parts[1])))
        return ('queue_help', None)
    n = parse_lines(body)
    if n is not None:
        return ("log", n)
    return None


def _human_secs(secs):
    """给人看的时长。超过一分钟就不报秒了——「跑了 4 分 20 秒」比「260 秒」好读。"""
    secs = max(0, int(secs))
    if secs < 60:
        return "%d 秒" % secs
    if secs < 3600:
        return "%d 分 %d 秒" % (secs // 60, secs % 60)
    return "%d 小时 %d 分" % (secs // 3600, (secs % 3600) // 60)


def _human_kb(size):
    if size < 1024:
        return "%d 字节" % size
    if size < 1024 * 1024:
        return "%.0f KB" % (size / 1024.0)
    return "%.1f MB" % (size / (1024.0 * 1024))


def _cut(text, n):
    """截到 n 个字，截了就带省略号。换行压成空格，免得把一行掰成两行。"""
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[:n] + "…"


class AdminMixin(object):
    """要靠 Bridge 提供这些：`queue`、`queue_max`、`_running`、`_started_at`、
    `_msg_count`、`schedule`、`reminders`、`runner`、`cfg`、`log_path`，
    以及 `_in_chat`、`_apply_config_state`、`_ensure_timer`、`_build_config_state`
    （后三个见 wechat_bridge）。

    `_running` 是工作者线程写的、这条路是轮询线程读的，所以两边都只做整体赋值/整体读
    （CPython 里这是原子的），不逐字段改——省一把锁，也省得两边抢。
    """

    # ---------- 入口 ----------

    def _try_admin(self, uid, body):
        """管理通道。返回要回的话；None = 不是这些，交给后面的通道。"""
        low = (body or "").strip().lower()
        if low in _STOP_WORDS:
            return self._stop_running(uid, all_of_queue=False)
        if low in _STOP_ALL_WORDS:
            return self._stop_running(uid, all_of_queue=True)
        got = parse_admin(body)
        if got is None:
            return None
        action, arg = got
        if action == "status":
            return self._admin_status(uid)
        if action == "log":
            return self._admin_log(arg)
        if action == "reload":
            return self._admin_reload(uid)
        if action == "queue":
            return self._admin_queue(uid)
        if action == "queue_clear":
            return self._admin_queue_clear()
        if action == 'queue_manage':
            return self._admin_queue_manage(uid, *arg)
        if action == 'queue_help':
            return self._queue_management_help()
        return None

    # ---------- 小工具 ----------

    def _quick_status(self, uid):
        """Read-only summary. Other users' task text and history stay private."""
        run = self._running
        queued = self._queue_snapshot()
        own = sum(1 for item in queued if isinstance(item, (tuple, list)) and item
                  and getattr(item[0], 'from_user_id', None) == uid)
        mode = ('收尾后待机' if uid in self._draining else
                '待机' if self.standby_enabled and uid not in self._awake else '已唤醒')
        worker = getattr(self, '_worker', None)
        if worker is not None and not worker.is_alive():
            mode = '任务线程已停止，请在电脑运行中心检查'
        if run:
            label = (_cut(self._mask(run.get('label') or ''), 60) or '（空）') if run.get('uid') == uid else '另一个用户的任务'
            current = '%s，已用时 %s' % (label,
                                        _human_secs(time.time() - run['started_at']))
            if run.get('uid') == uid and run.get('stage'):
                current += '；' + _cut(self._mask(run['stage']), 40)
        elif self.queue.unfinished_tasks > len(queued):
            current = '正在切换任务，请稍后再查'
        else:
            current = '暂无执行中的任务'
        latest = '暂无记录'
        hint = ''
        journal = getattr(self, 'journal', None)
        if journal:
            try:
                from task_journal import LABELS
                row = journal.latest_outcome(uid, timeout=.05, read_only=True)
                if row:
                    latest = '%s · %s' % (LABELS[row['state']], _cut(self._mask(row['label']), 48))
                if journal.unresolved(uid, timeout=.05, read_only=True):
                    hint = '\n有待检查或恢复的任务，发 /tasks 查看。'
            except Exception:
                log.exception('简要状态读取任务记录失败')
                latest = '暂时无法读取，请在电脑运行中心检查'
        if uid in self._pending or uid in self._pending_meeting or uid in self._inbox_pending:
            hint += '\n有操作等待你的确认，请查看之前的提示。'
        return ('📊 当前概览\n状态：%s\n当前任务：%s\n排队：全机 %d 项（你的 %d 项）'
                '\n附件接收：全机 %d 项\n最近记录：%s%s'
                % (mode, current, len(queued), own, self._inbox_q.unfinished_tasks, latest, hint))

    def _job_label(self, run, uid):
        """正在跑的那条叫什么。别人的任务只说「另一个用户」，不把他的原话露给你。"""
        if run.get("uid") != uid:
            return "另一个用户的任务"
        return _cut(run.get("label"), 60) or "（空）"

    def _running_line(self, uid, now):
        run = self._running
        if run is None:
            return "现在没有在跑的任务"
        return "正在跑：%s（跑了 %s）" % (self._job_label(run, uid),
                                        _human_secs(now - run["started_at"]))

    def _queue_snapshot(self):
        """队列里排着的那些。**只看，不动。**

        `queue.Queue` 没有 peek，这里借它的内部 deque 和那把 mutex 取一个一致的快照。
        不这么取，就得自己再维护一份「队列里有什么」的列表，而那份和真队列迟早对不上
        （队列项在生产者、工作者、queue clear 三处进出）——这个项目在「同一个事实存
        两份」上吃过亏，时间解析那次就是。`queue.queue` / `queue.mutex` 是 stdlib 里
        多年没动过的名字。
        """
        q = getattr(self, "queue", None)
        if q is None:
            return []
        with q.mutex:
            return list(q.queue)

    def _queued_label(self, item, uid):
        """队列项 → 一行字。队列里装的是 (msg, body, job) 三元组。

        job 是 None 就是交给 Claude 的普通任务，body 就是那句原话；带 job 的是
        「点一下」「看看画面」「发文件」这几条自己排上去的活。
        """
        try:
            msg, body, job = item
        except (TypeError, ValueError):
            return "（认不出来的队列项）"
        who = "你" if getattr(msg, "from_user_id", None) == uid else "另一个用户"
        if who != '你':
            return "另一个用户的任务"
        if job is None:
            return "%s：%s" % (who, _cut(body, 50) or "（空）")
        kind = "点一下" if job.get("click") else \
               "看看画面" if job.get("look") else \
               "发文件" if job.get("sendfile") else "别的活"
        return "%s：%s" % (who, kind)

    def _log_path(self):
        return getattr(self, "log_path", None)

    def _mask(self, text):
        """抹掉凭据。精确值 + 形状两道，都是「宁可抹多」——日志里出现 token 本身就是
        不该发生的事，真抹掉了你只损失一点可读性。"""
        out = text or ""
        for k in ("bot_token", "account_id"):
            val = (getattr(self, 'cfg', None) or {}).get(k)
            if val and len(str(val)) >= 6:
                out = out.replace(str(val), "***")
        for pat in _SECRET_SHAPES:
            out = pat.sub("***", out)
        return out

    # ---------- /status ----------

    def _admin_status(self, uid):
        now = time.time()
        waiting = self._queue_snapshot()
        queued = ("后面排着 %d 条" % len(waiting)) if waiting else "后面没有排队的"

        bits = []
        if self.schedule is not None:
            bits.append("定下的会 %d 条" % len(self.schedule.all(uid)))
        if self.reminders is not None:
            bits.append("记下的提醒 %d 条" % len(self.reminders.all(uid)))
        jobs = (" · ".join(bits)) if bits else "定时和提醒都关着"

        if self.chat_mode_enable and self._in_chat(uid):
            left = ""
            if self.chat_idle_seconds:
                left = "，还能免前缀 %d 分钟" % max(
                    0, int((self._chat[uid] + self.chat_idle_seconds - now) // 60))
            chat = "你在里面%s" % left
        else:
            chat = "不在（要免前缀就发「开始对话」）"

        try:
            size = os.path.getsize(self._log_path())
        except (OSError, TypeError):
            size = None
        logline = ("日志：bridge.log %s" % _human_kb(size)) if size is not None \
            else "日志：还没写出来"

        return ("📊 桥的状态\n"
                "\n"
                "已运行 %s（%s 起的）\n"
                "本进程收到 %d 条消息\n"
                "%s\n"
                "%s\n"
                "%s\n"
                "对话模式：%s\n"
                "%s"
                % (_human_secs(now - self._started_at),
                   time.strftime("%H:%M", time.localtime(self._started_at)),
                   self._msg_count, self._running_line(uid, now), queued,
                   jobs, chat, logline))

    # ---------- /log ----------

    def _admin_log(self, lines_wanted):
        """把 bridge.log 的最后几行发过来。发之前抹一遍凭据（见模块开头 ②）。"""
        path = self._log_path()
        if not path or not os.path.exists(path):
            return ("🔕 还没有日志文件（%s）。桥刚起来、或者日志写到别处去了。"
                    % os.path.basename(path or "bridge.log"))
        n = max(1, min(LOG_MAX_LINES, int(lines_wanted or LOG_DEFAULT_LINES)))
        # 只读最后那几个字节：日志能到几 MB，整份读进来没必要
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                back = min(size, LOG_TAIL_BYTES)
                fh.seek(size - back)
                raw = fh.read()
        except OSError as exc:
            return "❌ 日志读不出来：%s" % exc
        all_lines = raw.decode("utf-8", "replace").splitlines()
        # 往回切多了会切在半行上，第一行大概率是残的，丢掉
        tail = all_lines[-(n + 1):] if len(all_lines) > n else all_lines
        if len(all_lines) > n:
            tail = tail[1:]
        body = self._mask("\n".join(tail))
        if len(body) > LOG_MAX_CHARS:
            body = "（只留了最后一段）\n" + body[-LOG_MAX_CHARS:]
        note = "（日志更大，只读了尾部）" if size > back else ""
        return "📄 bridge.log 最后 %d 行%s\n\n%s" % (
            len(tail), note, body.strip() or "（空的）")

    # ---------- /reload ----------

    def _admin_reload(self, uid):
        """就地重读 config.json。**不退出进程**（理由见模块开头 ①）。"""
        run = self._running
        if run is not None:
            return ("❌ 有任务正在跑（%s），这会儿不换配置。\n"
                    "先 `/stop` 或者等它跑完再 `/reload`。"
                    % self._job_label(run, uid))
        try:
            new = load_config(CONFIG_PATH)
        except (SystemExit, OSError) as exc:
            # SystemExit：文件没了 / JSON 写坏了（load_config 就是拿它报错的）
            # OSError：文件被独占（编辑器或杀软正在写它）、或者 config.json 变成目录了。
            # 后面这条以前没兜住，会一路冒到 poll_once 去，结果是**一个字都不回你**。
            log.warning("/reload 读配置失败：%s", exc)
            return ("❌ config.json 没读成，旧配置留着没动：\n%s\n\n改好再 /reload。"
                    % exc)

        # 白名单守卫**必须排在装配之前**：_build_config_state 里 ClaudeRunner 一看到
        # keep_context=false 就会当场清空 sessions.json（那是盘上副作用）。先拦掉，
        # 「旧配置留着没动」才真的是一个字都没动。
        allowed = allowed_set(new)
        if not allowed:
            return ("❌ 新的白名单是空的，这样一重载谁都执行不了（包括你），"
                    "旧配置留着没动。\n先把 allowed_user_ids 填上再 /reload。")
        if uid not in allowed:
            return ("❌ 新的白名单里没有你（%s），重载之后你就发不动指令了，"
                    "旧配置留着没动。\n把你自己加回 allowed_user_ids 再 /reload。" % uid)

        # 然后才把新的整份算出来。这一步**不碰 self**，抛了也伤不到正在跑的桥
        try:
            state = self._build_config_state(new)
        except SystemExit as exc:            # claude_bin 坏了 / workdir 没了 / 有禁用参数
            log.warning("/reload 装配失败（SystemExit）：%s", exc)
            return "❌ config.json 读到了，但装不起来，旧配置留着没动：\n%s" % exc
        except Exception as exc:             # noqa: BLE001  别的意外，一样不换
            log.exception("/reload 装配失败")
            return ("❌ config.json 装不起来，旧配置留着没动：%s: %s"
                    % (type(exc).__name__, exc))

        # 换不到的东西，分两类如实说，别让人以为生效了
        warn, restart = [], []
        for k in ("bot_token", "account_id", "base_url"):
            if ((self.cfg or {}).get(k) or "") != (new.get(k) or ""):
                warn.append(k)
        for k in ("log_max_mb", "log_backup_count"):
            # 这两个只在 setup_logging 里用过一次，handler 早就装好了，热更换不掉
            if (self.cfg or {}).get(k) != new.get(k):
                restart.append(k)
        if warn:
            log.warning("/reload 里 %s 变了，但这项要重登才有用", "、".join(warn))
        if restart:
            log.warning("/reload 里 %s 变了，但要重启开关才生效", "、".join(restart))

        # 到这儿才换。_apply_config_state 只做 setattr，不会抛
        old_cfg = self.cfg
        self.cfg = new
        try:
            self._apply_config_state(state)
        except Exception:                     # noqa: BLE001  理论上到不了，留个兜底
            self.cfg = old_cfg
            log.exception("/reload 换配置时出错，已回退")
            return "❌ 换配置的时候出错了，已经退回旧配置。细节看 bridge.log。"
        # 定时/提醒整块从关着被打开时，那条心跳线程得补起来
        self._ensure_timer()

        changed = sorted(k for k in set(list(old_cfg) + list(new))
                         if old_cfg.get(k) != new.get(k))
        lines = ["✅ 配置已重读，进程没重启——队列、上下文、定下的会都还在。",
                 "改过的键：%d 个" % len(changed)]
        if old_cfg.get("backend", "claude") != new.get("backend", "claude"):
            lines.append("✅ 模型后端已切换；各后端保留独立上下文。")
        if changed:
            lines.append("　%s" % "、".join(changed[:12]))
        if warn:
            lines.append("⚠️ %s 换过了，但这项要重新登录才生效（跑 login 扫码）。"
                         % "、".join(warn))
        if restart:
            lines.append("⚠️ %s 这次不生效：日志滚动只在启动时读一次"
                         "（setup_logging），要重启开关。" % "、".join(restart))
        if not warn and not restart:
            lines.append("凭据（token / 账号 / 地址）和日志设置都没动。")
        return "\n".join(lines)

    # ---------- /queue ----------

    def _admin_queue(self, uid):
        waiting = self._queue_snapshot()
        if waiting:
            entries, own_position = [], 0
            for index, item in enumerate(waiting, 1):
                entry = '%d. %s' % (index, self._queued_label(item, uid))
                if getattr(item[0], 'from_user_id', None) == uid:
                    own_position += 1
                    ident = TaskQueue.ident(item)
                    if ident:
                        entry += '\n  编号：%s（你的第 %d 条）' % (ident, own_position)
                        entry += '\n取消这一条：\n/queue cancel %s' % ident
                entries.append(entry)
            tail = ('排着的 %d 条：\n%s\n以下格式需填写正文或位置：'
                    '/queue edit 编号 新正文；/queue move 编号 N（你的排队位置）。'
                    '\n要清掉全部，发「/queue clear」。') % (len(waiting), '\n'.join(entries))
        else:
            tail = "排着的：没有"
        cap = ("上限 %d 条，满了就直说不再接。" % self.queue_max) if self.queue_max \
            else "没设上限（config.json 里 queue_max 填个数可以设一个）。"
        return ("📋 队列\n"
                "\n"
                "%s\n"
                "%s\n"
                "%s"
                % (self._running_line(uid, time.time()), tail, cap))

    @staticmethod
    def _queue_management_help():
        return ('❌ 队列操作格式：/queue cancel 完整编号；/queue edit 完整编号 新正文；'
                '/queue move 完整编号 N（你的排队位置，从 1 开始）。'
                '\n发 /queue 复制编号；只操作你尚未开始的任务，附件和电脑操作只能取消或调序。')

    def _admin_queue_manage(self, uid, action, ident, value):
        q = self.queue
        if not isinstance(q, TaskQueue):
            return '❌ 当前队列暂不支持单条操作，请在电脑更新并重启助手。'
        with q.management_lock:
            reservation = q.reserve(uid, ident, freeze_owner=action == 'move')
            if reservation is None:
                return '🔕 找不到你的待执行任务，或它已经开始、正在取消。发 /queue 查看最新队列。'
            _, item = reservation
            msg, body, job = item
            try:
                if action == 'edit' and (job is not None or getattr(msg, 'attachments', None)):
                    return '❌ 只有普通文字任务可以修改正文；任务模板、附件、点击、截图、发文件任务可取消或调序。'
                if action == 'move':
                    with q.mutex:
                        count = sum(getattr(it[0], 'from_user_id', None) == uid for it in q.queue)
                    if value < 1 or value > count:
                        return '❌ 你的排队位置应为 1 到 %d。发 /queue 查看。' % count
                journal = getattr(self, 'journal', None)
                if journal:
                    journal_id = getattr(msg, '_journal_id', None)
                    if not journal_id:
                        return '❌ 这条任务缺少有效记录，单条操作未执行。请检查本机日志。'
                    if action == 'cancel':
                        saved = journal.cancel_queued(uid, journal_id)
                    elif action == 'edit':
                        saved = journal.edit_queued(uid, journal_id, value, self._mask(value))
                    else:
                        saved = journal.inspect_queued(uid, journal_id)
                    if not saved:
                        return '🔕 任务记录已变化，单条操作未执行。发 /queue 查看最新队列。'
                q.finish(reservation, action, body=value if action == 'edit' else None,
                         position=value if action == 'move' else None)
                if action == 'cancel':
                    return '✅ 已取消排队任务：%s。' % ident
                if action == 'edit':
                    control = getattr(msg, '_task_control', None)
                    if control and control.requested.is_set():
                        return '⏹ 正文已保存，但同时收到停止请求，这条任务不会开始。'
                    return '✅ 已修改排队任务：%s。执行时会使用新正文。' % ident
                return '✅ 已将任务 %s 调到你的第 %d 条；其他用户之间的顺序保持不变。' % (ident, value)
            except Exception:
                log.exception('单条队列操作失败 action=%s', action)
                return '❌ 单条操作未完成；任务仍保留在队列，原有停止请求不会撤销。请检查日志后重试。'
            finally:
                q.release(reservation)

    def _cancel_queue_snapshot_locked(self):
        """Freeze the stop boundary while holding queue.mutex; no disk I/O."""
        pending = list(self.queue.queue)
        for msg, _, _ in pending:
            control = getattr(msg, '_task_control', None)
            if control is None:
                control = msg._task_control = TaskControl()
            control.cancel()
        return pending

    def _drain_queue(self, pending=None):
        lock = getattr(self.queue, 'management_lock', None)
        if lock is not None:
            with lock:
                return self._drain_queue_managed(pending)
        return self._drain_queue_managed(pending)

    def _drain_queue_managed(self, pending=None):
        """清掉队列里排着的，返回清掉了多少条。正在跑的那条不在队列里，动不到它。"""
        n = 0
        self._queue_cancel_failed = False
        if pending is None:
            with self.queue.mutex:
                pending = self._cancel_queue_snapshot_locked()
        for removed in pending:
            try:
                if getattr(self, "journal", None) and getattr(removed[0], "_journal_id", None):
                    self.journal.update(removed[0]._journal_id, "cancelled")
            except Exception:
                log.exception("取消任务的记录未能保存")
                self._queue_cancel_failed = True
                # Keep it queued if possible. Its stop request also prevents a
                # concurrent worker from executing after a failed cancellation.
                break
            # Persistence is outside the queue lock. The worker may have taken
            # this already-cancelled task; only its owner then calls task_done.
            n += 1
            with self.queue.mutex:
                index = next((i for i, item in enumerate(self.queue.queue)
                              if item[0] is removed[0]), None)
                if index is None:
                    continue
                del self.queue.queue[index]
                self.queue.not_full.notify()
            self.queue.task_done()
        return n

    def _admin_queue_clear(self):
        n = self._drain_queue()
        if self._queue_cancel_failed:
            return '❌ 已取消 %d 条，其余任务的取消状态无法保存，已保留停止请求。请检查本机日志后重试。' % n
        if not n:
            return "🔕 队列本来就是空的，没清掉什么。"
        return "✅ 清掉了排着的 %d 条。正在跑的那条没动。" % n

    # ---------- 叫停 ----------

    def _stop_running(self, uid, all_of_queue=False):
        """叫停正在跑的那一条。`all_of_queue` 时才连排着的一起清（只有 /stopall 为真）。

        `/stop` 和 `/stopall` 的区别是故意的：前者最小破坏，排着的还留着；后者是「全」
        这个字自己要来的语义。两边的回话都写明到底清了没有，省得你去猜。
        要 `uid` 是为了复用 `_job_label`：别人的任务只报「另一个用户的任务」，
        不把人家那句话念给你听。
        """
        # TaskQueue publishes claims using this same mutex. Each task is either
        # in this fixed snapshot or already visible as the current task.
        with self.queue.mutex:
            run = self._running
            pending = self._cancel_queue_snapshot_locked() if all_of_queue else []
            control = run.get('cancel') if run is not None else None
            repeated = bool(control and control.requested.is_set())
            stopped = control.cancel() if control is not None else False
            if all_of_queue:
                # A generation distinguishes even receives within one Windows clock tick.
                self._inbox_generation = getattr(self, '_inbox_generation', 0) + 1
        if run is not None and control is None:
            stopped = self.runner.cancel()
        inbox_queue = getattr(self,"_inbox_q",None)
        inbox_total = inbox_queue.unfinished_tasks if inbox_queue is not None else 0
        inbox_run = getattr(self,"_inbox_active",None)
        inbox_stopped = bool(inbox_run and inbox_run["cancel"].cancel())
        cleared = self._drain_queue(pending) if all_of_queue else 0
        if all_of_queue:
            for owner, waiting in list(getattr(self, '_inbox_pending', {}).items()):
                self._inbox_receipt(waiting.get('msg'), 'cancelled')
                self._inbox_pending.pop(owner, None)
        if all_of_queue and self._queue_cancel_failed:
            return '❌ 已取消 %d 条排队任务，其余取消状态无法保存，已保留停止请求。请检查本机日志后重试。' % cleared
        if run is None:
            if all_of_queue and inbox_total:
                return "⏹ 已请求停止当前及排队的附件接收；已完成的文件会保留。"
            if inbox_stopped:
                return "⏹ 已请求停止接收附件；完成的文件会保留，未完成文件会清理。"
            inbox = getattr(self, "_inbox_q", None)
            if inbox is not None and inbox.unfinished_tasks:
                return "⏳ 附件接收正在切换任务，请稍后再发 /stop。"
            if cleared:
                return "✅ 现在没有在跑的任务，顺手清掉了排着的 %d 条。" % cleared
            return "🔕 现在没有在跑的任务。"
        if repeated:
            return "⏳ 停止请求已收到，正在等待当前任务退出；已完成的操作不会撤销。"
        if not stopped:
            return ("❌ 没叫停成功：那条任务可能刚好自己跑完了，或者进程句柄已经没了。\n"
                    "发 /status 看看现在还在不在跑。")
        note = ("\n连排着的 %d 条也清了。" % cleared) if cleared else ""
        if all_of_queue and inbox_total:note += "\n同时已请求停止当前及排队的附件接收。"
        elif inbox_stopped:note += "\n同时已请求停止接收附件。"
        left = len(self._queue_snapshot())
        left_note = ("\n排着的 %d 条没动，发 /queue 看、/queue clear 清。" % left) \
            if (left and not all_of_queue) else ""
        return ("⏹ 已请求停止：%s（跑了 %s）%s%s\n执行线程退出后会报告结果；此前的文件改动可能保留。"
                % (_cut(self._job_label(run, uid), 40) or "（空）",
                   _human_secs(time.time() - run["started_at"]),
                   note, left_note))
