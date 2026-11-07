"""Offline queue management: durable changes, ownership and claim/stop races."""
import queue
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge_admin import AdminMixin, is_control_message, parse_admin
from task_control import TaskControl, TaskQueue
from task_journal import TaskJournal


class Harness(AdminMixin):
    def __init__(self, journal):
        self.queue = TaskQueue()
        self.queue_max = 0
        self.journal = journal
        self._running = None
        self._inbox_q = queue.Queue()
        self._inbox_active = None
        self.cfg = {}


class QueueManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = TaskJournal(Path(self.temp.name) / 'tasks.sqlite')
        self.b = Harness(self.journal)
        self.threads, self.releases, self.errors = [], [], []
        self.addCleanup(self.finish_threads)

    def finish_threads(self):
        for event in self.releases:
            event.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'fixture thread did not finish')
        self.assertEqual(self.errors, [])

    def background(self, function):
        def run():
            try:
                function()
            except BaseException as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def enqueue(self, body, uid='owner', job=None, attachments=None):
        msg = SimpleNamespace(from_user_id=uid, attachments=attachments or [],
                              _task_control=TaskControl())
        if self.b.journal:
            msg._journal_id, _ = self.journal.add(uid, body, 'task', body,
                payload=body if job is None and not attachments else None)
        self.b.queue.put_nowait((msg, body, job))
        return msg

    def command(self, action, msg, value='', uid='owner'):
        return self.b._try_admin(uid, '/queue %s %s %s' %
                                 (action, TaskQueue.ident((msg, '', None)), value))

    def receipt(self, msg):
        with self.journal.connect(read_only=True) as db:
            row = db.execute('SELECT state,payload,label FROM tasks WHERE id=?',
                             (msg._journal_id,)).fetchone()
        return row

    def block_method(self, name, *, fail=False):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = getattr(self.journal, name)

        def method(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture barrier timed out')
            if fail:
                raise sqlite3.OperationalError('fixture write failure')
            return original(*args, **kwargs)
        setattr(self.journal, name, method)
        return entered, release

    def test_parser_preserves_edit_case_newlines_and_rejects_ambiguous_commands(self):
        ident = 'a' * 32
        self.assertEqual(parse_admin('/queue edit %s New BODY\nline two' % ident),
                         ('queue_manage', ('edit', ident, 'New BODY\nline two')))
        self.assertEqual(parse_admin('/QUEUE CANCEL ' + ident.upper()),
                         ('queue_manage', ('cancel', ident, None)))
        self.assertEqual(parse_admin('/队列 move %s 2' % ident),
                         ('queue_manage', ('move', ident, 2)))
        for command in ('/queue cancel', '/queue edit ' + ident, '/queue move ' + ident + ' 0',
                        '/queue move ' + ident + ' 2 extra', '/queue cancel ' + ident + ' extra',
                        '/queue cancel 1', '/queue cancel ' + ident[:8], '/queue typo', '/queue clear typo'):
            with self.subTest(command=command):
                self.assertEqual(parse_admin(command), ('queue_help', None))
                self.assertTrue(is_control_message(command))
                self.assertTrue(is_control_message('/cc ' + command, '/cc'))
        for body in ('/queues', '取消第三件事后整理周报', '正文里包含 /queue cancel'):
            self.assertIsNone(parse_admin(body))

    def test_artifact_queries_are_controls_with_and_without_prefix(self):
        for body in ('/artifacts', '/getfile abc 2', '查看刚才的产物',
                     '把刚才的产物发给我', '把刚才生成的文件发给我'):
            self.assertTrue(is_control_message(body))
            self.assertTrue(is_control_message('/cc ' + body, '/cc'))

    def test_queue_has_stable_numbers_hides_other_users_text_and_ids(self):
        mine = self.enqueue('my request')
        other = self.enqueue('secret foreign request', uid='other')
        self.enqueue('second request')
        first = self.b._admin_queue('owner')
        self.assertIn(mine._journal_id, first)
        self.assertIn('你的第 2 条', first)
        self.assertNotIn(other._journal_id, first)
        self.assertNotIn('secret foreign request', first)
        self.assertIn('/queue edit', first)
        self.assertEqual(first, self.b._admin_queue('owner'))

    def test_cancel_each_job_kind_and_balance_capacity_and_join(self):
        for index, job in enumerate((None, {'sendfile': ['p']}, {'look': True}, {'query': 'click'})):
            with self.subTest(job=job):
                msg = self.enqueue('job %d' % index, job=job)
                self.assertTrue(self.command('cancel', msg).startswith('✅'))
                self.assertEqual(self.receipt(msg), ('cancelled', None, 'job %d' % index))
                self.assertEqual(self.b.queue.unfinished_tasks, 0)
                self.assertTrue(self.b.queue.empty())
                self.b.queue.join()

    def test_only_owner_can_cancel_edit_or_move(self):
        msg = self.enqueue('original')
        for action, value in (('cancel', ''), ('edit', 'replacement'), ('move', '1')):
            self.assertTrue(self.command(action, msg, value, uid='other').startswith('🔕'))
        self.assertEqual(self.b.queue.unfinished_tasks, 1)
        self.assertEqual(self.b._queue_snapshot()[0][1], 'original')
        self.assertEqual(self.receipt(msg), ('queued', 'original', 'original'))

    def test_edit_changes_execution_payload_label_and_recovery_payload(self):
        msg = self.enqueue('original')
        self.assertTrue(self.command('edit', msg, 'New BODY\nline two').startswith('✅'))
        self.assertEqual(self.b._queue_snapshot()[0][1], 'New BODY\nline two')
        self.assertEqual(self.receipt(msg), ('queued', 'New BODY\nline two', 'New BODY\nline two'))
        item, error = self.b.queue.take(lambda item: None)
        self.assertIsNone(error)
        self.assertEqual(item[1], 'New BODY\nline two')
        self.b.queue.task_done()
        reopened = TaskJournal(self.journal.path)
        self.assertEqual(reopened.claim('owner', msg._journal_id), 'New BODY\nline two')

    def test_edit_rejects_attachments_and_every_special_job(self):
        for index, (job, attachments) in enumerate(((None, ['image']),
                ({'sendfile': ['p']}, None), ({'look': True}, None), ({'query': 'click'}, None))):
            msg = self.enqueue('restricted %d' % index, job=job, attachments=attachments)
            self.assertTrue(self.command('edit', msg, 'replacement').startswith('❌'))
            self.assertEqual(self.receipt(msg)[1], None)
            self.assertTrue(self.command('cancel', msg).startswith('✅'))

    def test_move_preserves_other_user_slots_and_balances_counts(self):
        first = self.enqueue('first')
        foreign = self.enqueue('foreign', uid='other')
        second = self.enqueue('second', job={'look': True})
        third = self.enqueue('third')
        self.assertTrue(self.command('move', third, '1').startswith('✅'))
        self.assertEqual([it[0] for it in self.b._queue_snapshot()], [third, foreign, first, second])
        self.assertTrue(self.command('move', third, '3').startswith('✅'))
        self.assertEqual([it[0] for it in self.b._queue_snapshot()], [first, foreign, second, third])
        self.assertEqual(self.b.queue.unfinished_tasks, 4)
        self.assertTrue(self.command('move', first, '4').startswith('❌'))
        self.assertEqual(self.b._queue_snapshot()[0][0], first)

    def test_failed_cancel_edit_or_move_never_loses_item_or_changes_payload(self):
        msg = self.enqueue('original')
        for action, value, name in (('cancel', '', 'cancel_queued'), ('edit', 'replacement', 'edit_queued'),
                                    ('move', '1', 'inspect_queued')):
            with patch.object(self.journal, name, side_effect=sqlite3.OperationalError('fixture')):
                with self.assertLogs('bridge', level='ERROR'):
                    self.assertTrue(self.command(action, msg, value).startswith('❌'))
            self.assertEqual(self.b._queue_snapshot()[0][1], 'original')
            self.assertEqual(self.receipt(msg), ('queued', 'original', 'original'))
            self.assertEqual(self.b.queue.unfinished_tasks, 1)
            self.assertFalse(msg._task_control.requested.is_set())
            self.assertFalse(self.b.queue._reserved)

    def test_changed_durable_state_refuses_queue_mutation(self):
        msg = self.enqueue('original')
        self.journal.update(msg._journal_id, 'running')
        for action, value in (('cancel', ''), ('edit', 'replacement'), ('move', '1')):
            self.assertTrue(self.command(action, msg, value).startswith('🔕'))
        self.assertEqual(self.b.queue.unfinished_tasks, 1)
        self.assertEqual(self.b._queue_snapshot()[0][1], 'original')

    def test_already_claimed_or_stop_requested_tasks_are_refused(self):
        msg = self.enqueue('original')
        self.b.queue.take(lambda item: None)
        for action, value in (('cancel', ''), ('edit', 'replacement'), ('move', '1')):
            self.assertTrue(self.command(action, msg, value).startswith('🔕'))
        self.b.queue.task_done()
        stopped = self.enqueue('stop requested')
        stopped._task_control.cancel()
        self.assertTrue(self.command('edit', stopped, 'replacement').startswith('🔕'))
        self.assertEqual(self.b._queue_snapshot()[0][1], 'stop requested')

    def test_disabled_journal_still_has_stable_ids_and_can_edit_move_cancel(self):
        self.b.journal = None
        first, second = self.enqueue('first'), self.enqueue('second')
        self.assertRegex(first._queue_id, r'^[0-9a-f]{32}$')
        self.assertIn(first._queue_id, self.b._admin_queue('owner'))
        self.assertTrue(self.command('edit', first, 'replacement').startswith('✅'))
        self.assertTrue(self.command('move', second, '1').startswith('✅'))
        self.assertEqual([it[1] for it in self.b._queue_snapshot()], ['second', 'replacement'])
        for msg in (first, second):
            self.assertTrue(self.command('cancel', msg).startswith('✅'))
        self.assertEqual(self.b.queue.unfinished_tasks, 0)

    def test_slow_edit_releases_mutex_keeps_capacity_and_preserves_global_fifo(self):
        msg = self.enqueue('first')
        self.enqueue('other task', uid='other')
        self.b.queue.maxsize = 2
        entered, release = self.block_method('edit_queued')
        answers = []
        manager = self.background(lambda: answers.append(self.command('edit', msg, 'replacement')))
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.b.queue.qsize(), 2)
        with self.assertRaises(queue.Full):
            self.b.queue.put_nowait((SimpleNamespace(from_user_id='new'), 'new task', None))
        claimed, finished = [], threading.Event()
        def take():
            item, error = self.b.queue.take(lambda item: None)
            claimed.append((item[1], error))
            self.b.queue.task_done()
            finished.set()
        worker = self.background(take)
        self.assertFalse(finished.wait(.1))
        # qsize/full/put above still acquire queue.mutex during the slow write.
        release.set()
        manager.join(2)
        worker.join(2)
        self.assertFalse(manager.is_alive())
        self.assertTrue(answers[0].startswith('✅'))
        self.assertEqual(claimed, [('replacement', None)])
        self.enqueue('new task', uid='other')
        self.assertEqual([it[1] for it in self.b._queue_snapshot()], ['other task', 'new task'])
        self.assertEqual(self.b.queue.unfinished_tasks, 2)

    def test_worker_cannot_claim_cancel_reserved_item_but_wakes_after_failure(self):
        msg = self.enqueue('original')
        entered, release = self.block_method('cancel_queued', fail=True)
        answers, claimed, worker_finished = [], [], threading.Event()
        with self.assertLogs('bridge', level='ERROR'):
            manager = self.background(lambda: answers.append(self.command('cancel', msg)))
            self.assertTrue(entered.wait(2))
            def take():
                item, error = self.b.queue.take(lambda item: None)
                claimed.append(item[1])
                self.b.queue.task_done()
                worker_finished.set()
            worker = self.background(take)
            self.assertFalse(worker_finished.wait(.1))
            release.set()
            manager.join(2)
            worker.join(2)
        self.assertTrue(answers[0].startswith('❌'))
        self.assertEqual(claimed, ['original'])
        self.assertEqual(self.b.queue.unfinished_tasks, 0)

    def test_edit_keeps_later_same_owner_dependency_behind_reserved_task(self):
        msg = self.enqueue('create input')
        self.enqueue('use input')
        entered, release = self.block_method('edit_queued')
        answers, taken, finished = [], [], threading.Event()
        manager = self.background(lambda: answers.append(self.command('edit', msg, 'create new input')))
        self.assertTrue(entered.wait(2))
        def take():
            item, _ = self.b.queue.take(lambda item: None)
            taken.append(item[1])
            self.b.queue.task_done()
            finished.set()
        worker = self.background(take)
        self.assertFalse(finished.wait(.1))
        release.set()
        manager.join(2)
        worker.join(2)
        self.assertTrue(answers[0].startswith('✅'))
        self.assertEqual(taken, ['create new input'])
        self.assertEqual(self.b._queue_snapshot()[0][1], 'use input')
        self.assertEqual(self.b.queue.unfinished_tasks, 1)

    def test_move_blocks_same_owner_new_arrivals_until_order_commit(self):
        self.enqueue('first')
        second = self.enqueue('second')
        entered, release = self.block_method('inspect_queued')
        answers, taken, finished = [], [], threading.Event()
        manager = self.background(lambda: answers.append(self.command('move', second, '1')))
        self.assertTrue(entered.wait(2))
        self.enqueue('late dependency')
        def take():
            item, _ = self.b.queue.take(lambda item: None)
            taken.append(item[1])
            self.b.queue.task_done()
            finished.set()
        worker = self.background(take)
        self.assertFalse(finished.wait(.1))
        release.set()
        manager.join(2)
        worker.join(2)
        self.assertTrue(answers[0].startswith('✅'))
        self.assertEqual(taken, ['second'])
        self.assertEqual([it[1] for it in self.b._queue_snapshot()], ['first', 'late dependency'])

    def test_move_preserves_global_fifo_during_validation(self):
        first = self.enqueue('first')
        second = self.enqueue('second')
        self.enqueue('foreign', uid='other')
        entered, release = self.block_method('inspect_queued')
        answers = []
        manager = self.background(lambda: answers.append(self.command('move', second, '1')))
        self.assertTrue(entered.wait(2))
        taken, finished = [], threading.Event()
        def take():
            item, _ = self.b.queue.take(lambda item: None)
            taken.append(item[1])
            self.b.queue.task_done()
            finished.set()
        worker = self.background(take)
        self.assertFalse(finished.wait(.1))
        release.set()
        manager.join(2)
        worker.join(2)
        self.assertTrue(answers[0].startswith('✅'))
        self.assertEqual(taken, ['second'])
        self.assertEqual([it[1] for it in self.b._queue_snapshot()], ['first', 'foreign'])

    def test_stopall_during_edit_cancels_replacement_and_keeps_new_task(self):
        msg = self.enqueue('original')
        entered, release = self.block_method('edit_queued')
        edited, stopped = [], []
        manager = self.background(lambda: edited.append(self.command('edit', msg, 'replacement')))
        self.assertTrue(entered.wait(2))
        stopper = self.background(lambda: stopped.append(self.b._stop_running('owner', all_of_queue=True)))
        self.assertTrue(msg._task_control.requested.wait(2))
        self.assertTrue(stopper.is_alive())
        late = self.enqueue('later task')
        release.set()
        manager.join(2)
        stopper.join(2)
        self.assertFalse(manager.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertTrue(edited[0].startswith('⏹'))
        self.assertEqual(self.receipt(msg)[0], 'cancelled')
        self.assertEqual([it[0] for it in self.b._queue_snapshot()], [late])
        self.assertEqual(self.b.queue.unfinished_tasks, 1)
        self.assertFalse(late._task_control.requested.is_set())

    def test_stopall_after_failed_edit_preserves_cancel_request_and_cancels_original(self):
        msg = self.enqueue('original')
        entered, release = self.block_method('edit_queued', fail=True)
        edited, stopped = [], []
        with self.assertLogs('bridge', level='ERROR'):
            manager = self.background(lambda: edited.append(self.command('edit', msg, 'replacement')))
            self.assertTrue(entered.wait(2))
            stopper = self.background(lambda: stopped.append(self.b._stop_running('owner', all_of_queue=True)))
            self.assertTrue(msg._task_control.requested.wait(2))
            release.set()
            manager.join(2)
            stopper.join(2)
        self.assertTrue(edited[0].startswith('❌'))
        self.assertEqual(self.receipt(msg)[0], 'cancelled')
        self.assertEqual(self.b.queue.unfinished_tasks, 0)
        self.assertTrue(msg._task_control.requested.is_set())


if __name__ == '__main__':
    unittest.main()
