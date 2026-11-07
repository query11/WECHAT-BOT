"""Real task/batch stores and control paths; transport/model are fixture mocks."""
import json
import os
import time
import unittest
from unittest.mock import Mock

from tests import selftest_batch_integration as fixtures
from attachment_batches import BatchError, collect_input_records
from bridge_admin import is_control_message
from bridge_runtime import Runtime, atomic_json
from desktop_history import history_action
from task_control import TaskQueue
from task_templates import TaskTemplateStore


class WorkflowTests(unittest.TestCase):
    setUp = fixtures.BatchIntegrationTests.setUp
    message = fixtures.BatchIntegrationTests.message
    entry = fixtures.BatchIntegrationTests.entry
    replies = fixtures.BatchIntegrationTests.replies
    process_download = fixtures.BatchIntegrationTests.process_download

    def command(self, text, uid='owner'):
        msg = self.message(text, uid)
        self.b.handle(msg)
        return msg

    def collected(self):
        self.command('接下来这几份文件一起处理')
        batch = self.b.batch_store.active('owner')
        self.process_download([self.entry()])
        return self.b.batch_store.get('owner', batch['id'])

    def test_conversation_requires_review_then_queues_once(self):
        batch = self.collected()
        self.command('发完了，汇总附件')
        self.assertEqual(self.b.queue.qsize(), 0)
        msg = self.command('确认执行这批附件')
        self.assertEqual(self.b.queue.qsize(), 1, self.replies())
        self.command('确认执行这批附件')
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertEqual(self.b.journal.inputs('owner', msg._journal_id)['id'], batch['id'])
        self.b.runner.run.assert_not_called()

    def test_new_file_after_review_requires_another_review(self):
        self.collected()
        self.command('发完了，汇总附件')
        self.process_download([self.entry('later.txt')])
        self.command('确认执行')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertIn('清单已经变化', self.replies())
        self.command('发完了，汇总附件')
        self.command('确认执行')
        self.assertEqual(len(self.b.queue.queue[0][2]['batch']['files']), 2)

    def test_pending_download_cannot_confirm(self):
        batch = self.collected()
        self.command('发完了，汇总附件')
        self.b.batch_store.begin_intake('owner', batch['id'], 'pending')
        self.command('确认执行')
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_review_expires_and_is_owner_scoped(self):
        self.collected()
        self.command('发完了，汇总附件')
        self.b.allowed.add('other')
        self.command('确认执行', 'other')
        self.assertEqual(self.b.queue.qsize(), 0)
        with self.b.batch_store.connect() as db:
            db.execute('UPDATE attachment_batch_dialogs SET created=?', (time.time() - 901,))
        self.command('确认执行')
        self.assertEqual(self.b.queue.qsize(), 0)
        self.assertIn('超过 15 分钟', self.replies())

    def test_template_is_pinned_at_review_even_if_deleted(self):
        self.b.template_store = TaskTemplateStore(self.b.journal.path)
        self.b.template_store.save('owner', '周报', dict(inputs='附件', steps='汇总', output_dir='工作目录', output_format='TXT'))
        self.collected()
        self.command('发完了，按周报模板整理，本周的数据')
        self.b.template_store.delete('owner', '周报')
        self.command('确认执行')
        job = self.b.queue.queue[0][2]
        self.assertEqual(job['template']['revision'], 1)
        self.assertEqual(self.b.queue.queue[0][1], '本周的数据')

    def test_queue_full_keeps_review_for_explicit_retry(self):
        self.collected()
        self.command('发完了，汇总附件')
        self.b.queue = TaskQueue(maxsize=1)
        self.b.queue.put((self.message(), 'busy', None))
        self.command('确认执行')
        self.assertEqual(self.b.queue.qsize(), 1)
        self.b.queue.get_nowait(); self.b.queue.task_done()
        self.command('确认执行')
        self.assertEqual(self.b.queue.qsize(), 1)

    def test_standby_and_unauthorized_cannot_create_or_confirm(self):
        self.b._awake.clear()
        self.command('开始收集附件')
        self.assertIsNone(self.b.batch_store.active('owner'))
        self.command('开始收集附件', 'outsider')
        self.assertIsNone(self.b.batch_store.active('outsider'))

    def test_commands_are_exact_and_recognized_as_controls(self):
        for body in ('开始收集附件', '发完了，整理', '确认执行这批附件'):
            self.assertTrue(is_control_message(body))
            self.assertTrue(is_control_message('/cc ' + body, '/cc'))
        for body in ('帮我解释确认执行是什么意思', '开始收集附件后打开微信'):
            self.assertFalse(self.b._batch_command(self.message(body), body))

    def test_repair_retains_successful_files_and_deduplication_receipt(self):
        batch = self.collected()
        store = self.b.batch_store
        store.begin_intake('owner', batch['id'], 'broken')
        actual = store.finish_intake('owner', batch['id'], 'broken', [], failed=1)
        failure = actual['failures'][0]['id']
        self.process_download([self.entry('replacement.txt')])
        self.command('/batch dismiss %s %s' % (batch['id'], failure))
        actual = store.get('owner', batch['id'])
        self.assertEqual([r['name'] for r in actual['files']], ['one.txt', 'replacement.txt'])
        self.assertEqual(actual['failed'], 0)
        self.assertFalse(store.begin_intake('owner', batch['id'], 'broken'))
        self.assertTrue((self.inbox / 'one.txt').exists())
        self.assertEqual(store.seal('owner', batch['id'])['state'], 'ready')

    def test_remove_renumbers_and_invalidates_review_without_deleting_files(self):
        batch = self.collected()
        self.process_download([self.entry('two.txt')])
        self.command('发完了，汇总附件')
        self.command('/batch remove %s 1' % batch['id'])
        actual = self.b.batch_store.get('owner', batch['id'])
        self.assertEqual([(r['index'], r['name']) for r in actual['files']], [(1, 'two.txt')])
        self.assertTrue((self.inbox / 'one.txt').exists())
        self.command('确认执行')
        self.assertEqual(self.b.queue.qsize(), 0)

    def test_repair_rejects_other_owner_pending_and_sealed(self):
        batch = self.collected()
        store = self.b.batch_store
        with self.assertRaises(BatchError):
            store.repair('other', batch['id'], 'remove', 1)
        store.begin_intake('owner', batch['id'], 'pending')
        with self.assertRaises(BatchError):
            store.repair('owner', batch['id'], 'remove', 1)
        store.finish_intake('owner', batch['id'], 'pending', [], failed=1)
        failure = store.get('owner', batch['id'])['failures'][0]['id']
        store.repair('owner', batch['id'], 'dismiss', failure)
        store.seal('owner', batch['id'])
        with self.assertRaises(BatchError):
            store.repair('owner', batch['id'], 'remove', 1)

    def test_partial_record_verification_keeps_good_input(self):
        self.command('开始收集附件')
        good, bad = self.entry(), self.entry('missing.txt')
        (self.inbox / 'missing.txt').unlink()
        msg = self.message()
        self.b._batch_begin_inbox(msg, 'owner')
        msg._batch_intake_entries = [good, bad]
        self.b._batch_finish_inbox(msg, 'completed')
        actual = self.b.batch_store.active('owner')
        self.assertEqual([r['name'] for r in actual['files']], ['one.txt'])
        self.assertGreater(actual['failed'], 0)

    def test_new_collection_does_not_confirm_old_plan(self):
        batch = self.collected()
        self.command('发完了，旧任务')
        self.b.batch_store.seal('owner', batch['id'])
        self.command('开始收集附件 新任务')
        self.command('确认执行')
        self.assertEqual(self.b.queue.qsize(), 0)


class DesktopActionTests(unittest.TestCase):
    setUp = fixtures.BatchIntegrationTests.setUp
    message = fixtures.BatchIntegrationTests.message

    def task(self, state='recoverable', uid='owner'):
        ident, _ = self.b.journal.add(uid, 'test-' + state, 'task', '合成任务', payload='合成任务')
        self.b.journal.update(ident, state)
        return ident

    def action(self, operation, ident, **changes):
        request = dict(kind='history', operation=operation, task_id=ident, journal_path=self.b.journal.path)
        request.update(changes)
        return history_action(self.b, request)

    def state(self, ident):
        with self.b.journal.connect(read_only=True) as db:
            return db.execute('SELECT state,resolution FROM tasks WHERE id=?', (ident,)).fetchone()

    def test_resume_requires_current_reply_context_and_is_once_only(self):
        self.b.cfg['task_progress'] = True
        ident = self.task()
        self.assertIn('回复通道', self.action('resume', ident))
        self.assertEqual(self.state(ident)[0], 'recoverable')
        self.b._last_ctx['owner'] = ('owner', 'fake-context')
        self.assertIn('加入队列', self.action('resume', ident))
        self.assertEqual(self.b.queue.qsize(), 1)
        self.action('resume', ident)
        self.assertEqual(self.b.queue.qsize(), 1)
        self.assertEqual(self.b.queue.queue[0][0].context_token, 'fake-context')
        self.b.reply.assert_not_called()  # desktop control heartbeat never waits for network acceptance notices

    def test_cancel_uses_live_queue_and_durable_state(self):
        msg = self.message('queued task')
        self.b._enqueue((msg, 'queued task', None), msg)
        self.assertIn('已取消', self.action('cancel_queued', msg._journal_id))
        self.assertEqual(self.b.queue.unfinished_tasks, 0)
        self.assertEqual(self.state(msg._journal_id)[0], 'cancelled')

    def test_resolve_preserves_execution_and_rejects_running(self):
        ident = self.task('uncertain')
        self.action('checked', ident)
        self.assertEqual(self.state(ident), ('uncertain', 'checked'))
        running = self.task('running')
        self.action('archived', running)
        self.assertEqual(self.state(running), ('running', ''))

    def test_wrong_source_or_disallowed_owner_cannot_mutate(self):
        ident = self.task('uncertain')
        self.action('checked', ident, journal_path=str(self.root / 'other.sqlite'))
        self.assertEqual(self.state(ident)[1], '')
        other = self.task('interrupted', 'other')
        self.action('checked', other)
        self.assertEqual(self.state(other)[1], '')

    def test_old_runtime_request_never_mutates_task(self):
        ident = self.task('uncertain')
        self.b._worker = Mock(); self.b._worker.is_alive.return_value = True
        self.b._started_at = time.time()
        runtime = Runtime(self.b, self.root / '.runtime')
        action = dict(kind='history', operation='checked', task_id=ident, journal_path=self.b.journal.path)
        atomic_json(runtime.root / 'command-test.json', dict(pid=os.getpid() + 1, at=time.time(), action=action))
        runtime.tick()
        self.assertEqual(self.state(ident)[1], '')
        self.assertIn('过期', json.loads((runtime.root / 'reply-test.json').read_text(encoding='utf-8'))['message'])
        atomic_json(runtime.root / 'command-current.json', dict(pid=os.getpid(), at=time.time(), action=action))
        runtime.tick()
        self.assertEqual(self.state(ident)[1], 'checked')


if __name__ == '__main__':
    unittest.main()
