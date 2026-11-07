# -*- coding: utf-8 -*-
"""把一条微信指令交给本机的 Claude Code CLI 无头模式执行，并拿回文本结果。

要点：
  - 用 argv 传 prompt，绝不拼 shell 字符串（消息内容来自外部，shell=True 等于命令注入）
  - 按 UserID 记住 session_id，用 --resume 续接上下文，落盘以便重启后仍连着
  - 上下文有保鲜期（context_ttl_minutes）：隔太久的追问不再续，免得旧会话被无限期留着
  - keep_context=false 时干脆每条都是新会话，sessions.json 里什么都不留
  - 超时强杀，并把 stderr 摘要回传微信，避免"发出去就没动静"
"""

import json
import locale
import logging
import os
import shutil
import subprocess
import threading
import time
from model_health import ModelHealth
from runner_result import RunnerResult, result_failed

log = logging.getLogger("claude.runner")

# 这几个开关等于把整台机器的命令行交出去，无人值守场景下绝不能开
BANNED = ("--dangerously-skip-permissions", "bypassPermissions")

# sessions.json 里放时间戳的那个键。用户 ID 形如 xxx@im.wechat，不会撞上
STAMP_KEY = "__stamps__"


class TaskCancelled(Exception):
    """这条任务被「叫停」了（微信里发的 /stop，见 bridge_admin）。

    用抛异常而不是返回一句话：叫停和「跑完了但有错误」是两件不同的事，混在返回值里
    上层就只能靠比对文字来判断，那是最脆的写法。
    """


def kill_tree(pid, run=subprocess.run):
    """连子进程一起杀掉，返回 True/False，**不抛**。

    claude 在 Windows 上常常是个包装器（claude.cmd → cmd.exe → node.exe），只杀直接
    子进程会留下孤儿 node.exe 接着读写你的文件。`taskkill /T` 是整棵树杀法。
    **返回码要看**：taskkill 找不到进程、或者权限不够时会返回非 0，那时候「发过信号」
    不等于「已经死了」——不能拿它去报「已叫停」。杀不干净不该把这条路自己弄崩，
    所以一律吞掉异常，让上层退回 `proc.kill()`。
    """
    try:
        done = run(["taskkill", "/T", "/F", "/PID", str(pid)],
                   capture_output=True, timeout=15)
    except Exception:                         # noqa: BLE001
        return False
    return getattr(done, "returncode", 0) == 0


def _decode(raw):
    """CLI 输出一般是 UTF-8；Windows 上有些工具的 stderr 是本地编码，退一步再试。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _parse_cli_result(stdout, backend_name="Claude"):
    """Select one final result for text, session and minimal health diagnostics.

    The last typed or untyped final result wins; trailing assistant/system
    events and bare text notices never replace it. A bare legacy text object
    is accepted only when no final result event was returned.
    """
    raw = (stdout or "").strip()
    events = []
    try:
        value = json.loads(raw)
        events.extend(value if isinstance(value, list) else [value])
    except ValueError:
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            events.extend(value if isinstance(value, list) else [value])

    final, legacy_text = [], []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" or (not event.get("type") and (
                "result" in event or event.get("is_error") is True)):
            final.append(event)
        elif not event.get("type") and "text" in event:
            legacy_text.append(event)
    candidates = final or legacy_text
    if not candidates:
        # A wrapper message or incomplete event stream is not completion proof.
        detail = "\n" + raw[-3000:] if raw and not events else ""
        return RunnerResult("❌ 未获得有效的模型执行结果，不能确认任务完成。" + detail, 'failed'), None, None

    result = candidates[-1]
    text = result.get("result") or result.get("text") or ""
    if not isinstance(text, str):
        return RunnerResult("❌ 模型执行结果格式无效，不能确认任务完成。", 'failed'), None, None
    if not text:
        errors = result.get("errors")
        if isinstance(errors, list):
            text = "\n".join(value for value in errors if isinstance(value, str))

    # Health only needs evidence of a response and denied operations. Do not
    # retain the full result, provider/model keys, tool input or arbitrary notes.
    denials = result.get("permission_denials")
    diagnostics = {
        "is_error": result.get("is_error") is True,
        "modelUsage": bool(result.get("modelUsage")),
        "usage": bool(result.get("usage")),
        "permission_denials": len(denials) if isinstance(denials, list) else int(bool(denials)),
    }
    for key in ("duration_ms", "num_turns"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            diagnostics[key] = value
    if diagnostics["is_error"]:
        text = "❌ %s 报告任务失败：\n%s" % (backend_name, text or "没有返回错误说明。")
    if diagnostics["permission_denials"]:
        text = "❌ 有操作未获执行许可，不能视为全部完成。\n" + text
    session_id = result.get("session_id")
    state = 'failed' if diagnostics['is_error'] or diagnostics['permission_denials'] else 'completed'
    return (RunnerResult(text or "✅ 执行完成，但没有文本输出。", state),
            session_id if isinstance(session_id, str) else None, diagnostics)


class ClaudeRunner(object):
    backend_name = "Claude"

    def _process_options(self):
        """Backend subclasses may isolate child environment without changing the host."""
        return {}

    def __init__(self, claude_bin="claude", workdir=None, timeout_sec=600,
                 extra_args=None, session_file=None, keep_context=True,
                 context_ttl_minutes=120, memory_file=None):
        # claude_bin 也可以写成数组，比如 ["node", "C:\\...\\cli.js"]，
        # 用来绕开 .cmd 包装器或应用执行别名跑不起来的情况
        if isinstance(claude_bin, (list, tuple)):
            parts = [str(x) for x in claude_bin if str(x).strip()]
            if not parts:
                raise SystemExit("config.json 里 claude_bin 是空的。")
        else:
            parts = [str(claude_bin)]
        parts[0] = shutil.which(parts[0]) or parts[0]
        self.launch = parts
        self.claude_bin = parts[0]
        self.memory_file = memory_file
        self.workdir = os.path.abspath(workdir or os.getcwd())
        self.timeout_sec = int(timeout_sec)
        self.extra_args = [str(a) for a in (extra_args or [])]
        self.session_file = session_file or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sessions.json")
        self.keep_context = bool(keep_context)
        try:
            self.context_ttl = max(0.0, float(context_ttl_minutes)) * 60.0
        except (TypeError, ValueError):
            self.context_ttl = 120 * 60.0
        self._check_boundary()
        # 叫停用：跟 session 那把锁分开，免得 /stop 去等一个正在往盘上写会话的持锁者
        self._plock = threading.Lock()
        self._proc = None            # 正在跑的那个进程（Popen 对象）
        self._cancelled = False      # 这一轮有没有被叫停
        self._killer = kill_tree     # 可以换掉；自测里拿假的替它，绝不真杀进程
        self._lock = threading.Lock()
        self._stamps = {}
        self._sessions = self._load_sessions()
        self.health = ModelHealth()
        self.last_result = {}
        if not self.keep_context and self._sessions:

            # 关掉续接就别把旧的 session_id 继续留在盘上
            self._sessions = {}
            try:
                self._save_sessions()
            except (IOError, OSError):
                pass


    def _check_boundary(self):
        """workdir 是唯一的权限边界，启动时就把它验死，别等出事再说。"""
        if not os.path.isdir(self.workdir):
            raise SystemExit("workdir 不存在或不是目录：%s" % self.workdir)
        joined = " ".join(self.extra_args)
        for bad in BANNED:
            if bad in joined:
                raise SystemExit(
                    "claude_extra_args 里有 %s，拒绝启动。\n"
                    "微信那头没法弹确认框，但这个开关等于把整台机器交出去。"
                    "用 --permission-mode acceptEdits 就够无人值守了。" % bad)
        for i, a in enumerate(self.extra_args):
            if a == "--add-dir" or a.startswith("--add-dir"):
                where = self.extra_args[i + 1] if a == "--add-dir" and \
                    i + 1 < len(self.extra_args) else a
                log.warning("claude_extra_args 里有 --add-dir（%s）：工作目录之外也能读写了，"
                            "权限边界已被主动放宽。这是配置里显式写的，不是意外。", where)


    # ---------- session 持久化 ----------

    def _load_sessions(self):
        """文件里除了 user→session_id，还夹一个 __stamps__ 记最后一次用的时间。"""
        try:
            with open(self.session_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (IOError, OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        stamps = data.pop(STAMP_KEY, None)
        if isinstance(stamps, dict):
            for k, v in stamps.items():
                try:
                    self._stamps[k] = float(v)
                except (TypeError, ValueError):
                    continue
        return dict((k, v) for k, v in data.items() if isinstance(v, str))

    def _save_sessions(self):
        out = dict(self._sessions)
        out[STAMP_KEY] = dict((k, round(v, 3)) for k, v in self._stamps.items()
                              if k in self._sessions)
        tmp = self.session_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.session_file)

    def reset_session(self, user_id):
        with self._lock:
            existed = self._sessions.pop(user_id, None)
            self._stamps.pop(user_id, None)
            self._save_sessions()
        return existed is not None

    def _pick_session(self, user_id, now=None):
        """决定这条要不要续上一段：关了续接、或者隔得太久，就当新的一段。"""
        if not self.keep_context:
            return None
        sid = self._sessions.get(user_id)
        if not sid:
            return None
        if self.context_ttl <= 0:
            return sid
        now = time.time() if now is None else now
        last = self._stamps.get(user_id)
        if last is None:
            # 老版本存下来的（没有时间戳）当作过期，重新开一段更安全
            self.reset_session(user_id)
            return None
        if now - last > self.context_ttl:
            log.info("上下文隔了 %.0f 分钟没用，按新的一段来 user=%s",
                     (now - last) / 60.0, user_id)
            self.reset_session(user_id)
            return None
        return sid

    # ---------- 执行 ----------

    def _build_cmd(self, session_id, prompt):
        if self.memory_file:
            try:
                with open(self.memory_file, "r", encoding="utf-8-sig") as fh:
                    memory = fh.read(12000).strip()
                if memory:
                    prompt = "【本机固定记忆，由用户维护】\n" + memory + "\n【当前请求】\n" + prompt
            except (OSError, UnicodeError) as exc:
                log.warning("读取本机固定记忆失败：%s", type(exc).__name__)
        cmd = list(self.launch) + ["-p"]
        if session_id:
            cmd += ["--resume", session_id]
        cmd += self.extra_args
        # --output-format 放在 extra_args 之后：万一 extra_args 末尾是 --add-dir 这种
        # 「可以收多个值」的参数，中间夹一个别的参数才能保证它不会把 prompt 也当成目录吃掉
        cmd += ["--output-format", "json"]
        cmd.append(prompt)
        return cmd

    def run(self, user_id, prompt):
        self.last_result = {}
        self._health_details = None
        self._health_code = None
        self.health.begin()
        try:
            result = self._run_once(user_id, prompt)
        except TaskCancelled:
            self.health.cancelled()
            raise
        except Exception:
            self.health.finish(details=self._health_details,returncode=self._health_code,local_error=True)
            raise
        self.health.finish(result,self._health_details,self._health_code,local_error=result_failed(result) and self._health_details is None)
        return result

    def _run_once(self, user_id, prompt):
        """同步执行一条指令，返回给用户看的纯文本。

        用 Popen 而不是 subprocess.run：**得攥着进程句柄**，不然「叫停」无从下手——
        run 是阻塞的，外面没有任何东西能碰到那个进程。超时的口径和以前一模一样
        （到点杀整棵树，回那一句「超过 N 秒仍未结束」）。
        """
        session_id = self._pick_session(user_id)
        cmd = self._build_cmd(session_id, prompt)

        self._cancelled = False          # 上一轮的叫停标记不带过来
        log.info("执行: user=%s resume=%s cwd=%s", user_id, bool(session_id), self.workdir)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,          # 关键：prompt 来自外部，绝不能过 shell
                **self._process_options(),
            )
        except FileNotFoundError:
            return RunnerResult("❌ 找不到 claude 命令。请先装好 Claude Code CLI，"
                    "或在 config.json 里把 claude_bin 填成 claude.cmd 的绝对路径。", 'failed')
        with self._plock:
            self._proc = proc
        try:
            try:
                out, err = proc.communicate(timeout=self.timeout_sec)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                out, err = proc.communicate()
                # 刚好在超时那一刻被叫停：那句话该说「已叫停」而不是「超时」
                if self._cancelled:
                    raise TaskCancelled()
                return RunnerResult("❌ 任务超过 %d 秒仍未结束，已中止。可以拆小一点再发一次。"
                        % self.timeout_sec, 'failed')
        finally:
            with self._plock:
                self._proc = None
        # 被叫停的一律抛出去，不当成正常完成——把半截输出当结果发回微信比不发更糟
        if self._cancelled:
            raise TaskCancelled()

        self._health_code = proc.returncode
        text, new_session = self._read_result(_decode(out))

        if new_session and self.keep_context:
            with self._lock:
                self._sessions[user_id] = new_session
                self._stamps[user_id] = time.time()
                self._save_sessions()
        elif session_id and proc.returncode != 0:

            # 大概率是 session 失效了，清掉，下一条重新开
            self.reset_session(user_id)

        if proc.returncode != 0:
            err_text = _decode(err).strip()
            detail = text if self._health_details is not None else (err_text or text)
            return RunnerResult("❌ 执行失败（返回码 %d）：\n%s" % (
                proc.returncode, detail[-1200:] or "无输出"), 'failed')
        return text or RunnerResult("✅ 执行完成，但没有文本输出。")

    # ---------- 叫停 ----------

    def cancel(self):
        """叫停正在跑的那条。返回 True = 确实有一条在跑，信号已经发出去了。

        两步的顺序要紧：**先记「已叫停」再杀**。反过来的话，万一杀得慢一点、
        任务自己先跑完了，run() 那边就会把它当正常完成，把半截结果发回微信。
        """
        with self._plock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False                 # 没有在跑的，或者刚好已经结束了
        self._cancelled = True
        self._kill(proc)
        return True

    def _kill(self, proc):
        """杀整棵树。taskkill 不行就退回直接 kill——叫停这条路不许自己抛异常。"""
        try:
            if self._killer(proc.pid):
                return
        except Exception:                     # noqa: BLE001
            log.warning("叫停用的杀进程器本身抛了，退回 proc.kill()", exc_info=True)
        try:
            proc.kill()
        except Exception:                     # noqa: BLE001
            log.warning("proc.kill() 也没成功 pid=%s", proc.pid, exc_info=True)

    @staticmethod
    def _parse(stdout):
        """Keep the legacy (text, session_id) helper interface."""
        text, session_id, _ = _parse_cli_result(stdout)
        return text, session_id

    def _read_result(self, stdout):
        text, session_id, diagnostics = _parse_cli_result(stdout, self.backend_name)
        self._health_details = diagnostics
        self.last_result = diagnostics or {}
        return text, session_id
