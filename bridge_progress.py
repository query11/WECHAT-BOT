"""Only report observable phases, never guessed model reasoning or percentages."""
import threading
import time

PHASES = {'prepare': '准备任务', 'receive': '接收附件', 'input': '核对输入',
          'model': '模型处理', 'check': '检查结果', 'send': '发送回复', 'finish': '保存记录'}


class TaskProgress:
    """Observed phase transitions timed from dispatch, never from queue admission."""
    def __init__(self):
        self._lock = threading.Lock()
        self._started = self._phase_at = None
        self._phase = None
        self._steps = []

    def enter(self, phase):
        if phase not in PHASES:
            return
        with self._lock:
            now = time.monotonic()
            if self._started is None:
                self._started = now
            if phase != self._phase:
                self._phase, self._phase_at = phase, now
                self._steps = (self._steps + [phase])[-12:]

    def snapshot(self):
        with self._lock:
            if self._started is None:
                return None
            now = time.monotonic()
            return dict(phase=self._phase, label=PHASES[self._phase],
                        elapsed_seconds=int(max(0, now - self._started)),
                        phase_seconds=int(max(0, now - self._phase_at)),
                        steps=[dict(phase=key, label=PHASES[key]) for key in self._steps])


class ProgressMixin:
    def _set_stage(self, msg, stage, notify=False, *, phase=None):
        msg._progress_stage = stage
        progress = getattr(getattr(msg, '_task_control', None), 'progress', None)
        if isinstance(progress, TaskProgress) and phase is not None:
            progress.enter(phase)
        if getattr(msg, '_progress_started', None) is None:
            msg._progress_started = time.monotonic()
        if threading.current_thread() is self._worker and self._running:
            self._running = dict(self._running, stage=stage)
        if notify and self.cfg.get('task_progress', False):
            self.reply(msg, '⏳ ' + stage + '。', result=False)

    def _progress_notice(self, msg):
        control = getattr(msg, "_task_control", None)
        if control is not None and control.requested.is_set():
            return "⏳ 正在停止当前任务，结束后会报告结果。已完成的操作不会撤销。"
        stage = getattr(msg, '_progress_stage', '正在处理任务')
        progress = getattr(control, 'progress', None)
        sample = progress.snapshot() if isinstance(progress, TaskProgress) else None
        elapsed = sample['elapsed_seconds'] if sample else int(max(0, time.monotonic() - getattr(msg, '_progress_started', time.monotonic())))
        return '⏳ %s，已用时 %d 秒。完成后会发回结果；需要停止可发 /stop。' % (stage, elapsed)
