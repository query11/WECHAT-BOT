"""Local desktop status and control. No network endpoint or credentials exposed."""
import json
import logging
import os
import subprocess
import sys
import threading
import time
import tempfile
from bridge_config import CONFIG_PATH, load_config
import api_profiles
from pathlib import Path
from runtime_messages import explain_error, task_event
from desktop_history import DIRECT_ACTIONS, history_action
from desktop_templates import template_action
from desktop_workflow import workflow_action, overview
from bridge_progress import TaskProgress

ROOT = Path(__file__).resolve().parent / '.runtime'

# 已经正常通信后，短暂中断先不惊动界面：单次连接重置往往 20 秒内自愈。
# 只有中断持续超过这个秒数，才升级为界面可见的故障提示。
#
# 连接健康判定一律用**单调时钟的经过时长**（time.monotonic）：手动改时间或系统
# 对时把墙上时钟往前/往后拨，都不会让故障提前升级、也不会让静默期被拉长。
# Windows 上 time.monotonic() 基于系统单调时钟，包含休眠时间，所以休眠/恢复的
# 口径与改动前的墙上时钟一致；本次只消除时钟跳变，没有重写跨进程存活判定。
# 对外仍要显示真实时间的字段（heartbeat / last_success / started）继续是 Unix
# 时间戳；单调读数只在本进程内部相减，不落盘、不跨重启比较。
RECONNECT_NOTICE_SECONDS = 30       # 已连上后，中断持续多久才升级为界面故障
COMMUNICATION_FRESH_SECONDS = 100   # 多久没有成功通信就不再算“在线”
PERSISTENT_FAULT_SECONDS = 180      # 持续故障多久升级为红色错误
HISTORY_REFRESH_SECONDS = 2
HISTORY_READ_TIMEOUT_SECONDS = 0.05  # 每次查询最多等待 SQLite 锁 50ms，不挤占心跳。
HISTORY_DIAGNOSTIC_SECONDS = 60


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


# ---------- 运行源码身份：判断"后台加载的是不是磁盘上的新版本" ----------
# 本项目真的出现过：代码改完也提交了，后台进程还在跑升级前的代码，界面上看不出来。
#
# 检测范围 = **本进程已经 import 过的、项目根目录以内的 .py 文件**：
#   - 已经加载进内存的模块在磁盘上变了 => 内存里还是旧代码，重启才生效；
#   - 还没 import 的模块将来第一次 import 时会读到新内容，不算过期；
#   - 于是日志、配置、聊天数据、普通文档、测试、tools/ 和本机其它目录都不在
#     范围内，也不需要遍历整个项目，更不碰项目以外的任何路径。
# 启动时把这份指纹钉住当基线，之后刷新只做比较，绝不把基线改写成磁盘上的新版本。
# 启动后才加载的模块没有加载时指纹，轮询无法补证：持续报未知，重启后重新建立基线。
HERE = Path(__file__).resolve().parent
GIT_TIMEOUT_SECONDS = 3
IDENTITY_REFRESH_SECONDS = 60


def imported_source_fingerprint(root=HERE, modules=None):
    """{项目内相对路径: '大小:修改时间'}，只含本进程已经 import 的 .py 文件。

    读不到的单个文件不会抛出去：记成 ``unreadable``，由调用方降级成"版本状态未知"。
    """
    root = os.path.realpath(str(root))
    result = {}
    source = sys.modules if modules is None else modules
    for module in list(source.values()):
        try:
            path = getattr(module, '__file__', None)
        except Exception:
            continue
        if not path:
            continue
        try:
            real = os.path.realpath(path)
        except OSError:
            continue
        if not real.startswith(root + os.sep) or not real.endswith('.py'):
            continue
        name = os.path.relpath(real, root).replace(os.sep, '/')
        try:
            info = os.stat(real)
        except FileNotFoundError:
            result[name] = 'missing'
        except OSError:
            result[name] = 'unreadable'
        else:
            result[name] = '%d:%d' % (info.st_size, info.st_mtime_ns)
    return result


def git_revision(root=HERE, timeout=GIT_TIMEOUT_SECONDS):
    """HEAD 短提交号，只用来给诊断文字添一句。没装 Git、超时或查询失败一律返回 None。"""
    try:
        done = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    text = done.stdout.decode('utf-8', 'replace').strip()
    return text or None


def health(connection, last_success_mono, worker_alive, now_mono):
    """上一次成功通信距今多久才算“在线”。

    ``last_success_mono`` 与 ``now_mono`` 都是 ``time.monotonic()`` 的读数，
    ``None`` 表示从未成功通信。这里**不接受** Unix 时间戳：两者不能相减。
    """
    if not worker_alive:
        return 'error', '任务线程已停止'
    if connection == 'error':
        return 'error', '连接被拒绝，需要处理'
    if (connection != 'online' or last_success_mono is None
            or now_mono - last_success_mono > COMMUNICATION_FRESH_SECONDS):
        return 'reconnecting', '正在连接或重连'
    return 'online', '连接正常'


class Runtime:
    def __init__(self, bridge, root=ROOT):
        self.bridge = bridge
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self._connection_lock = threading.RLock()
        self._pending_error = None
        self.connection = 'connecting'
        self.last_success = 0             # Unix 时间戳，Python/PowerShell 都按它显示时间
        self._last_success_mono = None    # 未开始标记，不用 0 顶替：单调时钟可以合法地从 0 开始
        self.error = ''
        self._outage_since_mono = None    # None = 当前没有正在计时的中断
        self.last_error_kind = ''
        self.events = []
        self.closed = threading.Event()
        self._history_lock = threading.Lock()
        self._history_source = None
        self._history_checked = None
        self._history_warning = None
        self._history_good = ''
        self._history_text = ''
        self._history_failed = False
        self._desktop_view_lock = threading.Lock()
        self._desktop_view_checked = 0
        self._desktop_view = {}
        # ---------- 运行源码身份（见 imported_source_fingerprint 的说明）----------
        self._identity_lock = threading.Lock()
        self._identity_baseline = imported_source_fingerprint()  # 启动时钉住，之后只比较
        self._identity_unverified = set()  # 后加载模块的源码身份不能用第一次轮询倒推
        self._identity_cache = {'state': 'unknown', 'message': '', 'commit': git_revision(),
                                'detail': '还没有检查运行源码指纹'}
        self._identity_checked_mono = None
        self._identity_notified = False
        self.event('后台启动，等待微信连接')

    def event(self, text):
        with self._connection_lock:
            self.events.append(time.strftime('%H:%M:%S') + '  ' + text)
            self.events = self.events[-30:]

    def connected(self):
        with self._connection_lock:
            self._promote_outage()
            if self.connection != 'online':
                self.event('微信通信恢复正常')
            self.connection = 'online'
            self.last_success = time.time()          # Unix 时间戳：界面按本地时间显示
            self._last_success_mono = time.monotonic()  # 同一个时刻的单调读数，只在本进程内相减
            self._outage_since_mono = None
            self.last_error_kind = ''
            self.error = ''
            self._pending_error = None

    def failed(self, error, fatal=False):
        with self._connection_lock:
            now = time.monotonic()
            if self._outage_since_mono is None:
                self._outage_since_mono = now
            detail = explain_error(error)
            if (not fatal and self.connection == 'online'
                    and now - self._outage_since_mono < RECONNECT_NOTICE_SECONDS):
                self._pending_error = detail
                return
            self._report_failure(detail, fatal)

    def _promote_outage(self):
        # 静默期到点由定时刷新就地升级，不必等第二次失败回调。用单调时钟算经过时长。
        if self._outage_since_mono is None or self._pending_error is None:
            return
        if (self.connection == 'online'
                and time.monotonic() - self._outage_since_mono >= RECONNECT_NOTICE_SECONDS):
            self._report_failure(self._pending_error)

    def _report_failure(self, detail, fatal=False):
        self._pending_error = None
        self.connection = 'error' if fatal else 'reconnecting'
        title, advice = detail
        self.error = title + '。' + advice
        if title != self.last_error_kind:
            self.event(self.error)
        self.last_error_kind = title

    def snapshot(self):
        b = self.bridge
        now = time.time()
        with self._connection_lock:
            self._promote_outage()
            last_success, error, events = self.last_success, self.error, list(self.events)
            now_mono = time.monotonic()
            state, message = health(self.connection, self._last_success_mono,
                                    b._worker.is_alive(), now_mono)
            if (state == 'reconnecting' and self._outage_since_mono is not None
                    and now_mono - self._outage_since_mono >= PERSISTENT_FAULT_SECONDS):
                state, message = 'error', '微信连接中断已超过 3 分钟'
        if state == 'online':
            state = 'working' if (b._awake or not b.standby_enabled) else 'standby'
            message = '助手已唤醒' if state == 'working' else '在线待机'
        if b._draining and state in ('working', 'standby'):
            state, message = 'draining', '正在收尾，完成后自动待机'
        if b._desktop_stop.is_set():
            state, message = 'stopping', '正在停止，等待当前轮询结束'
        receiving_blocked = bool(getattr(b, '_receiving_blocked', False))
        if receiving_blocked and state in ('working', 'standby'):
            message = '任务接收暂缓，请检查本机存储'
            error = (error + '\n' if error else '') + '任务接收记录未能保存，未确认批次尚未提交游标；停止和状态查询仍可使用。'
        run = b._running or {}
        inbox = getattr(b, '_inbox_active', None)
        if not run and inbox:
            run = {'label':'接收附件','stage':'正在下载、解密并保存附件','cancel':inbox['cancel']}
        progress = getattr(run.get('cancel'), 'progress', None)
        progress = progress.snapshot() if isinstance(progress, TaskProgress) else None
        if progress:
            progress['stopping'] = bool(run['cancel'].requested.is_set())
            progress['source'] = 'task' if b._running else 'inbox'
        model = getattr(getattr(b, 'runner', None), 'health', None)
        model_state = model.snapshot() if model is not None else {'state':'unknown','label':'尚无模型调用记录','last_checked':0,'last_success':0,'detail':''}
        if not isinstance(model_state, dict):
            model_state = {'state':'unknown','label':'尚无模型调用记录','last_checked':0,'last_success':0,'detail':''}
        return dict(pid=os.getpid(), heartbeat=now, state=state, message=message,
                    code_identity=self.code_identity(),
                    started=b._started_at, last_success=last_success,
                    backend=getattr(b, "backend_name", "Claude"),
                    backend_id=str((getattr(b, 'cfg', None) or {}).get('backend', 'claude')).lower(),
                    backends=api_profiles.backend_list(getattr(b, 'cfg', None) or {}),
                    model_health=model_state,
                    receiving_blocked=receiving_blocked,
                    model_name=b._mask(str(getattr(getattr(b,"runner",None),"model","默认模型")))[:80],
                    backend_switch_available=bool(b._worker.is_alive() and not (
                        b._desktop_stop.is_set() or b._draining or b._running or
                        b.queue.unfinished_tasks or b._inbox_q.unfinished_tasks)),
                    task=b._mask(str(run.get('label', '暂无执行中的任务')))[:160],
                    stage=('正在停止当前任务' if run.get('cancel') is not None and run['cancel'].requested.is_set() else run.get('stage', '')),
                    task_progress=progress,
                    queued=b.queue.qsize(), downloads=b._inbox_q.unfinished_tasks,
                    error=error, events=events,
                    journal_source=self.journal_source(),
                    history_actions=['result', 'resend', 'resolve', 'artifacts', 'getfile'],
                    history_direct_actions=list(DIRECT_ACTIONS),
                    template_editor=True,
                    desktop_workflow=True,
                    desktop_overview=self.desktop_snapshot(),
                    history=self.history_snapshot())

    def desktop_snapshot(self):
        if not self._desktop_view_lock.acquire(blocking=False):
            return self._desktop_view
        try:
            if time.monotonic() - self._desktop_view_checked >= 5:
                self._desktop_view_checked = time.monotonic()
                self._desktop_view = overview(self.bridge)
            return self._desktop_view
        finally:
            self._desktop_view_lock.release()

    def journal_source(self):
        journal = getattr(self.bridge, 'journal', None)
        path = getattr(journal, 'path', '') if journal is not None else ''
        return {'enabled': journal is not None,
                'path': str(Path(path).resolve()) if isinstance(path, (str, Path)) and path else ''}

    def history_snapshot(self):
        """Two short read-only queries, cached independently from connection health."""
        journal = getattr(self.bridge, 'journal', None)
        # A simultaneous final snapshot should use the existing cache, not queue reads.
        if not self._history_lock.acquire(blocking=False):
            return self._history_text or '任务历史正在刷新，请稍后查看。'
        try:
            if journal is not self._history_source:
                self._history_source = journal
                self._history_checked = self._history_warning = None
                self._history_good = self._history_text = ''
                self._history_failed = False
            if journal is None:
                return ''
            now = time.monotonic()
            if self._history_checked is not None and now - self._history_checked < HISTORY_REFRESH_SECONDS:
                return self._history_text
            self._history_checked = now
            try:
                text = journal.describe(timeout=HISTORY_READ_TIMEOUT_SECONDS, read_only=True)
            except Exception as exc:
                self._history_failed = True
                self._history_text = '任务历史暂时无法读取，请稍后刷新。'
                if self._history_good:
                    self._history_text += '\n以下为上次读取的记录，尚未刷新：\n' + self._history_good
                if self._history_warning is None or now - self._history_warning >= HISTORY_DIAGNOSTIC_SECONDS:
                    logging.getLogger('bridge').warning(
                        '任务历史读取失败（%s）；实时状态继续刷新，请稍后检查任务记录。', type(exc).__name__)
                    self._history_warning = now
            else:
                if self._history_failed:
                    self.event('任务历史读取已恢复')
                self._history_failed = False
                self._history_good = self._history_text = text
            return self._history_text
        finally:
            self._history_lock.release()

    def code_identity(self):
        """启动时钉住的运行源码身份 vs 现在磁盘上的样子。

        基线一旦定下就不再改写，所以磁盘上的新版本只会被报成"已更新"，
        不会把"启动时加载的版本"悄悄顶掉。刷新有间隔，不跟着 0.5 秒轮询遍历项目。
        """
        with self._identity_lock:
            now = time.monotonic()
            if (self._identity_checked_mono is not None
                    and now - self._identity_checked_mono < IDENTITY_REFRESH_SECONDS):
                return dict(self._identity_cache)
            self._identity_checked_mono = now
        try:
            current = imported_source_fingerprint()
        except Exception:
            current = None
        with self._identity_lock:
            if current is None or not self._identity_baseline:
                # 读不到就报"未知"：绝不默认"已是最新版"，也不让状态线程退出。
                self._identity_cache.update(state='unknown', message='',
                                            detail='无法读取运行源码指纹')
                return dict(self._identity_cache)
            changed = unreadable = False
            for name, fingerprint in current.items():
                base = self._identity_baseline.get(name)
                if base is None:
                    # 导入与首次轮询之间可能已改过源码，不能把当前磁盘状态当加载基线。
                    # 保留未知标记，后续轮询或 sys.modules 移除也不能悄悄变回 same。
                    self._identity_unverified.add(name)
                    continue
                if fingerprint == 'unreadable' or base in ('unreadable', 'missing'):
                    unreadable = True
                elif fingerprint != base:
                    changed = True
            # 已确认的源码变化优先，其他未知模块不能遮掉明确的重启提示。
            if changed:
                self._identity_cache.update(state='changed', message='代码已更新，重启后生效',
                                            detail='运行源码与启动时不一致')
            elif unreadable:
                self._identity_cache.update(state='unknown', message='',
                                            detail='部分运行源码无法确认加载时指纹或暂时读不到，版本状态未知')
            elif self._identity_unverified:
                self._identity_cache.update(state='unknown', message='',
                                            detail='启动后加载了新模块，无法确认其加载时版本；重启后台后重新确认')
            else:
                self._identity_cache.update(state='same', message='',
                                            detail='运行源码与启动时一致')
            if self._identity_cache['state'] == 'changed' and not self._identity_notified:
                self._identity_notified = True
                self.event('代码已更新，重启后生效')
            return dict(self._identity_cache)

    def command(self, action):
        b = self.bridge
        if isinstance(action, dict):
            if action.get('kind') == 'workflow':
                result = workflow_action(b, action)
                self._desktop_view_checked = 0
                return result
            if action.get('kind') == 'templates':
                return template_action(b, action)
            if action.get('kind') != 'history':
                return '不支持的操作'
            return history_action(b, action)
        with b._desktop_lock:
            if b._desktop_stop.is_set():
                return '后台正在停止，请稍候'
            busy = b.queue.unfinished_tasks or b._inbox_q.unfinished_tasks or b._running
            if action == 'shutdown' and busy:
                return '还有任务或附件在处理，请完成后重试；必要时先停止当前任务'
            if isinstance(action, str) and action.startswith('backend:'):
                result = self._switch_backend(action.split(':', 1)[1])
            elif action == 'profile:reload':
                result = self._reload_profile()
            elif action == 'wake':
                if not b.allowed:
                    return '尚未配置用户白名单，无法唤醒'
                b._draining.clear()
                b._awake.update(b.allowed)
                if b.chat_mode_enable:
                    for uid in b.allowed:
                        b._chat[uid] = time.time()
                result = '助手已唤醒，可以在微信发任务'
            elif action in ('standby', 'shutdown'):
                if action == "standby" and busy:
                    b._draining.update(b.allowed)
                b.standby_enabled = True
                b._awake.clear()
                b._chat.clear()
                for waiting in list(b._inbox_pending.values()):
                    receipt = getattr(b, '_inbox_receipt', None)
                    if callable(receipt):
                        receipt(waiting.get('msg'), 'cancelled')
                for pending in (b._pending, b._pending_meeting, b._await_code, b._inbox_pending):
                    pending.clear()
                if action == 'shutdown':
                    b._desktop_stop.set()
                result = '正在停止后台' if action == 'shutdown' else ('已结束对话，任务收尾后自动待机' if busy else '已进入待机，已有提醒继续通知')
            elif action == 'cancel':
                result = b._stop_running(next(iter(b.allowed), ''), False)
            else:
                return '不支持的操作'
            self.event(result)
            return result

    def _switch_backend(self, backend):
        """Called with the desktop lock: prepare, persist, then publish runner."""
        b = self.bridge
        if backend not in api_profiles.backend_ids(b.cfg):
            return '不支持的模型后端；新保存的接口请先在后端配置中保存并应用'
        if b._draining or b._running or b.queue.unfinished_tasks or b._inbox_q.unfinished_tasks:
            return '还有任务或附件在处理，请空闲后再切换后端'
        if not b._worker.is_alive():
            return '任务线程未就绪，请先重启后台'
        path = Path(CONFIG_PATH)
        temp = None
        try:
            original = path.read_bytes()
            raw = json.loads(original.decode('utf-8-sig'))
            if not isinstance(raw, dict):
                return '配置格式无效，后端未切换'
            effective = load_config(str(path))
            disk_compare = {k: v for k, v in effective.items() if k != 'backend'}
            live_compare = {k: v for k, v in b.cfg.items() if k != 'backend'}
            if disk_compare != live_compare:
                return '配置文件还有其他未加载的修改，请先重读配置再切换后端'
            selected = dict(b.cfg, backend=backend)
            # Constructors may clear history when keep_context=False; preparation
            # must have no persistent side effect before configuration is saved.
            prepared, name = b._build_runner(dict(selected, keep_context=True))
            prepared.keep_context = bool(selected.get('keep_context', True))
            raw['backend'] = backend
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig',
                    dir=str(path.parent), prefix='.backend-ui-', suffix='.tmp', delete=False) as fh:
                temp = Path(fh.name)
                json.dump(raw, fh, ensure_ascii=False, indent=2)
                fh.write('\n'); fh.flush(); os.fsync(fh.fileno())
            if path.read_bytes() != original:
                return '配置刚被其他程序修改，请重读配置后再切换'
            os.replace(temp, path)
            temp = None
        except (OSError, ValueError, TypeError, SystemExit):
            return '后端切换失败，请检查本机模型配置和密钥文件；原后端保持不变'
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        b.runner, b.backend_name, b.cfg = prepared, name, selected
        return '已切换到 %s，立即生效；各后端对话分别保留' % name

    def _reload_profile(self):
        b=self.bridge
        if b._draining or b._running or b.queue.unfinished_tasks or b._inbox_q.unfinished_tasks:
            return '配置已保存；当前有任务或附件，请空闲后再次保存并应用'
        if not b._worker.is_alive():
            return '配置已保存；任务线程未就绪，请重启后台应用'
        try:
            new=load_config(CONFIG_PATH)
            ignored=('deepseek','claude_api','api_profiles')
            if {k:v for k,v in new.items() if k not in ignored}!={k:v for k,v in b.cfg.items() if k not in ignored}:
                return '配置已保存；存在其他未加载的修改，请重读配置后应用'
            runner,name=b._build_runner(dict(new,keep_context=True))
            runner.keep_context=bool(new.get('keep_context',True))
        except (OSError,ValueError,TypeError,SystemExit):
            return '配置已保存但未能应用，原后端仍在运行；请检查配置或恢复原值'
        b.runner,b.backend_name,b.cfg=runner,name,new
        return '后端配置已保存并应用；当前使用 '+name

    def tick(self):
        for path in list(self.root.glob('command-*.json'))[:10]:
            try:
                request = json.loads(path.read_text(encoding='utf-8-sig'))
                if request.get('pid') != os.getpid() or time.time() - request.get('at', 0) > 20:
                    result = '请求已过期，请重新操作'
                else:
                    result = self.command(request.get('action'))
                response = {'message': result.get('message', ''), 'data': result} if isinstance(result, dict) else {'message': result}
                atomic_json(self.root / ('reply-' + path.name[8:]), response)
            finally:
                path.unlink(missing_ok=True)
        self.bridge._finish_draining()
        atomic_json(self.root / 'status.json', self.snapshot())

    def loop(self):
        while not self.closed.wait(0.5):
            try:
                self.tick()
            except Exception:
                # Status UI failure must not kill message processing.
                pass

    def start(self):
        owner = self
        class Recent(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING or record.getMessage().startswith(('完成 user=', '任务被叫停')):
                    
                    if record.getMessage().startswith(('收消息失败', '凭据被拒')):
                        return  # failed() already supplied the readable explanation
                    owner.event(task_event(record.getMessage()))
        self.handler = Recent()
        logging.getLogger('bridge').addHandler(self.handler)
        threading.Thread(target=self.loop, daemon=True).start()

    def finish(self):
        self.closed.set()
        if hasattr(self, 'handler'):
            logging.getLogger('bridge').removeHandler(self.handler)
        data = self.snapshot()
        data.update(state='stopped' if self.bridge._desktop_stop.is_set() else 'error',
                    message='后台已停止' if self.bridge._desktop_stop.is_set() else '后台已退出，请查看日志')
        atomic_json(self.root / 'status.json', data)
