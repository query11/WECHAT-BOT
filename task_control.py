"""One cancellation request shared by model execution and outgoing attachments."""
import threading
import logging
import queue
import uuid
from claude_runner import TaskCancelled
from bridge_progress import TaskProgress


class TaskQueue(queue.Queue):
    """Claim atomically; reserve queued items while their receipt is saved.

    Management serializes on a separate lock. Reserved items remain in the
    deque and still consume capacity. A reserved head blocks claiming until its
    owner releases it, preserving FIFO even when tasks share desktop or files.
    Neither the queue mutex nor the publication callback performs disk I/O.
    """
    def __init__(self, maxsize=0):
        super().__init__(maxsize)
        self.management_lock = threading.RLock()
        self._reserved = {}

    def _put(self, item):
        if isinstance(item, (tuple, list)) and item:
            msg = item[0]
            if not getattr(msg, '_queue_id', None):
                msg._queue_id = getattr(msg, '_journal_id', None) or uuid.uuid4().hex
        super()._put(item)

    @staticmethod
    def ident(item):
        return getattr(item[0], '_journal_id', None) or getattr(item[0], '_queue_id', None)

    def reserve(self, uid, ident, *, freeze_owner=False):
        """Keep a task unclaimed. Caller holds management_lock until release."""
        with self.mutex:
            item = next((it for it in self.queue if self.ident(it) == ident
                         and getattr(it[0], 'from_user_id', None) == uid), None)
            if item is None:
                return None
            control = getattr(item[0], '_task_control', None)
            if control and control.requested.is_set():
                return None
            related = [it for it in self.queue
                       if it is item or (freeze_owner and
                       getattr(it[0], 'from_user_id', None) == uid)]
            if any(id(it) in self._reserved for it in related):
                return None
            token = object()
            for it in related:
                self._reserved[id(it)] = token
            return token, item

    def release(self, reservation):
        with self.not_empty:
            token, _ = reservation
            for key in [key for key, value in self._reserved.items() if value is token]:
                del self._reserved[key]
            self.not_empty.notify_all()

    def finish(self, reservation, action, *, body=None, position=None):
        """Apply the already-persisted action without releasing ownership."""
        token, item = reservation
        with self.mutex:
            index = next((i for i, it in enumerate(self.queue) if it is item), None)
            if index is None or self._reserved.get(id(item)) is not token:
                raise RuntimeError('queue reservation lost')
            if action == 'cancel':
                control = getattr(item[0], '_task_control', None)
                if control:
                    control.cancel()
                del self.queue[index]
                self.unfinished_tasks -= 1
                if self.unfinished_tasks == 0:
                    self.all_tasks_done.notify_all()
                self.not_full.notify()
            elif action == 'edit':
                replacement = (item[0], body, item[2])
                self.queue[index] = replacement
                # The replacement must stay reserved until the caller releases.
                self._reserved[id(replacement)] = token
            elif action == 'move':
                uid = getattr(item[0], 'from_user_id', None)
                slots = [i for i, it in enumerate(self.queue)
                         if getattr(it[0], 'from_user_id', None) == uid]
                if position < 1 or position > len(slots):
                    raise ValueError('invalid queue position')
                own = [self.queue[i] for i in slots]
                own.pop(next(i for i, it in enumerate(own) if it is item))
                own.insert(position - 1, item)
                for slot, it in zip(slots, own):
                    self.queue[slot] = it
            else:
                raise ValueError(action)

    def take(self, publish):
        with self.not_empty:
            while True:
                if self.queue and id(self.queue[0]) not in self._reserved:
                    break
                self.not_empty.wait()
            item = self._get()
            error = None
            try:
                publish(item)
            except Exception as exc:
                # Return ownership even on preparation failure so the worker
                # can report it and balance unfinished_tasks in its finally.
                error = exc
            self.not_full.notify()
            return item, error


class TaskControl:
    def __init__(self):
        self.progress=TaskProgress()
        self.commit_lock=threading.RLock()
        self.saved_count=0
        self.cleanup_paths=set()
        self.cleanup_failed=False
        self.requested=threading.Event()
        self.finished=threading.Event()
    def cancel(self):
        with self.commit_lock:
            if self.finished.is_set():return False
            self.requested.set();return True
    def check(self):
        if self.requested.is_set():raise TaskCancelled()
    def watch_runner(self,runner):
        while not self.finished.is_set():
            if self.requested.wait(.1):
                while not self.finished.is_set():
                    try:
                        if runner.cancel():return
                    except Exception as exc:
                        logging.getLogger("bridge").warning("停止执行器时异常：%s",type(exc).__name__)
                    self.finished.wait(.05)
                return

class TransferCancelled(TaskCancelled):
    def __init__(self,uncertain=False):
        super().__init__('transfer cancelled')
        self.uncertain=bool(uncertain)
