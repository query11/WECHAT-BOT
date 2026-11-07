"""Deterministic, offline task handoff and cancellation regressions."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import task_control
from ilink_client import Message
from tests import selftest_standby
from task_journal import TaskJournal


class TaskHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        fixture = selftest_standby.StandbyTests()
        fixture.setUp()
        self.b = fixture.bridge
        self.b.queue = task_control.TaskQueue()
        self.b.queue_max = 0
        self.b.journal = TaskJournal(Path(self.temp.name) / 'tasks.sqlite')
        self.b._mask = str
        self.b._set_stage = Mock()
        self.b.runner = Mock()
        self.b.runner.cancel.return_value = False
        self.executed = []
        self.b._run_task = lambda msg, body: self.executed.append(body)
        for name in ('_run_sendfile', '_run_look', '_run_vision'):
            setattr(self.b, name, Mock())
        self.workers = []
        self.releases = []
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        for event in self.releases:
            event.set()
        for worker in self.workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())

    def enqueue(self, label, job=None):
        msg = Message({'from_user_id': 'owner', 'message_id': label})
        self.assertIsNone(self.b._enqueue((msg, label, job), msg))
        return msg

    def start_worker(self, expected=1):
        completed = []

        def finish():
            completed.append(True)
            if len(completed) == expected:
                raise SystemExit()

        self.b._finish_draining = finish
        worker = threading.Thread(target=self.b._loop_worker, daemon=True)
        self.b._worker = worker
        self.workers.append(worker)
        worker.start()
        return worker

    def block_running(self, fail=False):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.b.journal.update
        blocked = []

        def update(ident, state, **kwargs):
            if state == 'running' and not blocked:
                blocked.append(True)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test barrier timed out')
                if fail:
                    raise sqlite3.OperationalError('fixture write failure')
            return original(ident, state, **kwargs)

        self.b.journal.update = update
        return entered, release

    def assert_finished(self, worker):
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.b.queue.unfinished_tasks, 0)
        self.assertIsNone(self.b._running)

    def test_stopall_during_running_write_cancels_before_every_execution_entry(self):
        # Each iteration is isolated so no prior stop marker can mask the race.
        jobs = (None, {'sendfile': True}, {'look': True}, {'vision': True})
        for index, job in enumerate(jobs):
            with self.subTest(job=job):
                msg = self.enqueue('job-%d' % index, job)
                entered, release = self.block_running()
                worker = self.start_worker()
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.b.queue.qsize(), 0)
                answer = self.b._stop_running('owner', all_of_queue=True)
                release.set()
                self.assert_finished(worker)
                self.assertNotIn('没有在跑的任务', answer)
                self.assertEqual(self.executed, [])
                for name in ('_run_sendfile', '_run_look', '_run_vision'):
                    getattr(self.b, name).assert_not_called()
                state = {r['id']: r['state'] for r in self.b.journal.recent()}
                self.assertEqual(state[msg._journal_id], 'cancelled')

    def test_stop_and_repeated_stop_leave_the_next_queued_task(self):
        first = self.enqueue('first')
        self.enqueue('second')
        entered, release = self.block_running()
        worker = self.start_worker(expected=2)
        self.assertTrue(entered.wait(3))
        first_answer = self.b._stop_running('owner')
        repeated_answer = self.b._stop_running('owner')
        self.assertEqual(self.b.queue.qsize(), 1)
        release.set()
        self.assert_finished(worker)
        self.assertNotIn('没有在跑的任务', first_answer)
        self.assertIn('停止请求已收到', repeated_answer)
        self.assertEqual(self.executed, ['second'])
        state = {r['id']: r['state'] for r in self.b.journal.recent()}
        self.assertEqual(state[first._journal_id], 'cancelled')

    def test_stopall_does_not_cancel_tasks_enqueued_after_its_snapshot(self):
        self.enqueue('old')
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.b.journal.update

        def update(ident, state, **kwargs):
            if state == 'cancelled':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test barrier timed out')
            return original(ident, state, **kwargs)

        self.b.journal.update = update
        answers = []
        stopper = threading.Thread(target=lambda: answers.append(
            self.b._stop_running('owner', all_of_queue=True)))
        self.workers.append(stopper)
        stopper.start()
        self.assertTrue(entered.wait(3))
        # Cancellation persistence must hold neither the task queue nor desktop lock.
        self.enqueue('new')
        release.set()
        stopper.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(self.b.queue.qsize(), 1)
        worker = self.start_worker()
        self.assert_finished(worker)
        self.assertEqual(self.executed, ['new'])

    def test_task_is_cancellable_before_dequeue_returns_to_worker(self):
        self.enqueue('handoff')
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.b.queue.take

        def take(publish):
            item = original(publish)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test barrier timed out')
            return item

        self.b.queue.take = take
        worker = self.start_worker()
        self.assertTrue(entered.wait(3))
        answer = self.b._stop_running('owner')
        release.set()
        self.assert_finished(worker)
        self.assertNotIn('没有在跑的任务', answer)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.b.journal.recent()[0]['state'], 'cancelled')

    def test_desktop_cancel_remains_available_during_running_write(self):
        from bridge_runtime import Runtime
        self.enqueue('desktop-stop')
        entered, release = self.block_running()
        worker = self.start_worker()
        self.assertTrue(entered.wait(3))
        runtime = Runtime.__new__(Runtime)
        runtime.bridge = self.b
        runtime.event = Mock()
        answer = runtime.command('cancel')
        release.set()
        self.assert_finished(worker)
        self.assertIn('已请求停止', answer)
        self.assertEqual(self.executed, [])

    def test_preparation_error_balances_queue_and_next_task_still_runs(self):
        first = self.enqueue('bad-preparation')
        self.enqueue('next')
        original = self.b._job_title
        calls = []

        def title(body, job):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError('fixture preparation failure')
            return original(body, job)

        self.b._job_title = title
        with self.assertLogs('bridge', level='ERROR'):
            worker = self.start_worker(expected=2)
            self.assert_finished(worker)
        self.assertEqual(self.executed, ['next'])
        restarted = TaskJournal(self.b.journal.path)
        self.assertEqual(restarted.claim('owner', first._journal_id), 'bad-preparation')

    def test_worker_may_take_cancelled_queue_item_while_stopall_persists(self):
        self.enqueue('old')
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.b.journal.update
        first_cancel = []

        def update(ident, state, **kwargs):
            if state == 'cancelled' and not first_cancel:
                first_cancel.append(True)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test barrier timed out')
            return original(ident, state, **kwargs)

        self.b.journal.update = update
        answers = []
        stopper = threading.Thread(target=lambda: answers.append(
            self.b._stop_running('owner', all_of_queue=True)), daemon=True)
        self.workers.append(stopper)
        stopper.start()
        self.assertTrue(entered.wait(3))
        self.enqueue('new')
        worker = self.start_worker(expected=2)
        self.assert_finished(worker)
        release.set()
        stopper.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(self.executed, ['new'])
        self.assertIn('清掉了排着的 1 条', answers[0])
        state = {r['label']: r['state'] for r in self.b.journal.recent()}
        self.assertEqual(state, {'old': 'cancelled', 'new': 'completed'})
        self.assertEqual(self.b.queue.unfinished_tasks, 0)

    def test_failed_queue_cancel_keeps_task_and_stop_request_until_retry(self):
        msg = self.enqueue('pending')
        original = self.b.journal.update

        def failed_update(ident, state, **kwargs):
            if state == 'cancelled':
                raise sqlite3.OperationalError('fixture cancel failure')
            return original(ident, state, **kwargs)

        self.b.journal.update = failed_update
        with self.assertLogs('bridge', level='ERROR'):
            answer = self.b._stop_running('owner', all_of_queue=True)
        self.assertIn('取消状态无法保存', answer)
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertEqual(self.b.queue.unfinished_tasks, 1)
        self.assertTrue(msg._task_control.requested.is_set())
        self.b.journal.update = original
        worker = self.start_worker()
        self.assert_finished(worker)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.b.journal.recent()[0]['state'], 'cancelled')

    def test_start_write_failure_after_stop_does_not_execute_or_destroy_payload(self):
        msg = self.enqueue('recoverable')
        entered, release = self.block_running(fail=True)
        with self.assertLogs('bridge', level='ERROR'):
            worker = self.start_worker()
            self.assertTrue(entered.wait(3))
            self.b._stop_running('owner')
            release.set()
            self.assert_finished(worker)
        self.assertEqual(self.executed, [])
        restarted = TaskJournal(self.b.journal.path)
        self.assertEqual(restarted.claim('owner', msg._journal_id), 'recoverable')

    def test_cancel_write_failure_is_reported_without_executing(self):
        self.enqueue('cancel-write-failure')
        entered, release = self.block_running()
        original = self.b.journal.update

        def update(ident, state, **kwargs):
            if state == 'cancelled':
                raise sqlite3.OperationalError('fixture cancel failure')
            return original(ident, state, **kwargs)

        self.b.journal.update = update
        with self.assertLogs('bridge', level='ERROR'):
            worker = self.start_worker()
            self.assertTrue(entered.wait(3))
            self.b._stop_running('owner')
            release.set()
            self.assert_finished(worker)
        self.assertEqual(self.executed, [])
        messages = '\n'.join(call.args[1] for call in self.b.reply.call_args_list)
        self.assertIn('取消状态未能保存', messages)
        self.assertEqual(self.b.journal.recent()[0]['state'], 'running')


if __name__ == '__main__':
    unittest.main()
