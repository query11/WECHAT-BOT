"""Isolated schedule persistence, review races and real queue/receipt integration."""
import copy
import json
import time
import unittest
from unittest.mock import Mock, patch

from tests import selftest_desktop_workflow as fixture
from tests import selftest_template_integration as worker_fixture
from desktop_schedules import schedule_view, validate_plan
from desktop_workflow import workflow_action
from remind import ReminderStore, ScheduleError, _next_daily


class DesktopScheduleTests(unittest.TestCase):
    entry = fixture.DesktopWorkflowTests.entry
    worker_once = worker_fixture.TemplateIntegrationTests.worker_once

    def setUp(self):
        fixture.DesktopWorkflowTests.setUp(self)
        self.b.reminders = ReminderStore(str(self.root/'reminders.json'))
        self.store = self.b.reminders
        self.b.remind_stale_seconds = 3600
        self.b._push = Mock(return_value=True)
        self.plan = dict(kind='task', text='汇总已指定的材料', repeat='once', at=time.time()+3600)

    def call(self, operation, uid='owner', **kwargs):
        return workflow_action(self.b, dict(operation=operation, uid=uid, journal_path=self.b.journal.path, **kwargs))

    def preview(self, **kwargs):
        r = self.call('schedule_preview', plan=kwargs.pop('plan', self.plan), **kwargs)
        self.assertTrue(r['ok'], r)
        return r

    def save(self, **kwargs):
        r = self.call('schedule_save', token=self.preview(**kwargs)['token'])
        self.assertTrue(r['ok'], r)
        return r['schedules']['items'][0]

    def mutate(self, operation, row):
        return self.call('schedule_'+operation, schedule_id=row['id'], expected_version=row['version'])

    def test_preview_and_repeated_save_have_one_durable_plan_no_execution(self):
        p = self.preview()
        self.assertEqual(self.store.all('owner'), [])
        self.assertFalse((self.root/'reminders.json').exists())
        a = self.call('schedule_save', token=p['token'])
        b = self.call('schedule_save', token=p['token'])
        self.assertTrue(a['ok'], a);self.assertEqual(a['schedule_id'], b['schedule_id'])
        self.assertEqual(len(self.store.all('owner')), 1)
        self.assertEqual(ReminderStore(self.store.path).all('owner')[0]['id'], a['schedule_id'])
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.runner.run.assert_not_called();self.b.client.send_text.assert_not_called()

    def test_owner_expiry_and_store_replacement_invalidate_tokens(self):
        p = self.preview();self.b.allowed.add('other')
        self.assertFalse(self.call('schedule_save', uid='other', token=p['token'])['ok'])
        self.b._schedule_plans[p['token']]['created'] -= 901
        self.assertFalse(self.call('schedule_save', token=p['token'])['ok'])
        p = self.preview();self.b.reminders = ReminderStore(self.store.path)
        self.assertFalse(self.call('schedule_save', token=p['token'])['ok'])

    def test_expired_time_requires_review_again(self):
        self.plan['at'] = time.time()+20;p=self.preview()
        with patch('desktop_schedules.time.time', return_value=self.plan['at']):
            self.assertFalse(self.call('schedule_save', token=p['token'])['ok'])
        self.assertEqual(self.store.all('owner'), [])

    def test_edit_pause_stale_delete_and_owner_isolation(self):
        row = self.save();p=self.preview(schedule_id=row['id'], expected_version=row['version'])
        a=self.mutate('pause', row);self.assertTrue(a['ok'], a)
        self.assertFalse(self.call('schedule_save', token=p['token'])['ok'])
        self.assertFalse(self.mutate('delete', row)['ok'])
        self.b.allowed.add('other')
        paused=a['schedules']['items'][0]
        self.assertFalse(self.call('schedule_delete', uid='other', schedule_id=paused['id'], expected_version=paused['version'])['ok'])
        self.plan['text']='更新后的任务'
        edited=self.save(schedule_id=paused['id'], expected_version=paused['version'])
        self.assertTrue(edited['paused']);self.assertEqual(edited['id'], row['id'])
        self.assertEqual(edited['text'], self.plan['text'])
        self.assertTrue(self.mutate('delete', edited)['ok']);self.assertEqual(self.store.all('owner'), [])

    def test_pause_survives_restart_and_daily_resume_skips_backlog(self):
        row=self.save(plan=dict(kind='remind', text='每天核对', repeat='daily', hour=9, minute=0))
        self.mutate('pause', row)
        self.store=ReminderStore(self.store.path);self.b.reminders=self.store
        self.assertEqual(self.store.due(time.time()+4*86400), [])
        item=self.store.all('owner')[0];future=time.time()+4*86400
        self.store.manage('owner', item['id'], self.store.version(item), 'resume', future)
        after=self.store.all('owner')[0]
        self.assertEqual(after['at'], _next_daily(9,0,future,slack=0));self.assertFalse(after['paused'])

    def test_expired_paused_once_requires_edit_before_resume(self):
        row=self.save();self.mutate('pause',row);item=self.store.all('owner')[0]
        with self.assertRaises(ScheduleError):
            self.store.manage('owner',item['id'],self.store.version(item),'resume',item['at']+1)
        self.assertTrue(self.store.all('owner')[0]['paused'])

    def test_due_invalidates_edit_and_is_once_only(self):
        row=self.save(plan=dict(kind='task',text='每天任务',repeat='daily',hour=8,minute=30))
        p=self.preview(plan=dict(kind='task',text='编辑每日',repeat='daily',hour=9,minute=0),schedule_id=row['id'],expected_version=row['version'])
        self.store.due(row['at']+1)
        self.assertFalse(self.call('schedule_save',token=p['token'])['ok'])
        self.assertEqual(self.store.due(row['at']+2),[])

    def test_multiple_same_second_tasks_have_unique_receipts_and_results(self):
        now=time.time()
        for text in ('第一条任务','第二条任务'):
            self.store.add('owner',now-1,'刚才',text,kind='task')
        self.b._remind_tick(now)
        history=schedule_view(self.b,'owner')['history']
        self.assertEqual(len(history),2);self.assertEqual(len({h['task_id'] for h in history}),2)
        self.assertTrue(all(h['state']=='queued' and h['task_id'] for h in history))
        self.assertEqual(self.b.queue.qsize(),2)
        self.b.runner.run.assert_not_called()
        first=self.b.queue.queue[0][0]._journal_id
        self.b.journal.update(first,'completed',summary='模拟处理结束')
        found=next(r for r in schedule_view(self.b,'owner')['history'] if r['task_id']==first)
        self.assertEqual(found['state'],'completed');self.assertIn('模拟',found['detail'])
        self.assertEqual(schedule_view(self.b,'other')['history'],[])

    def test_queued_schedule_runs_through_existing_worker(self):
        now=time.time();self.store.add('owner',now-1,'刚才','输出汇总文字',kind='task')
        self.b._remind_tick(now);self.worker_once()
        self.b.runner.run.assert_called_once()
        row=schedule_view(self.b,'owner')['history'][0]
        self.assertEqual(row['state'],'completed',row)

    def test_reminder_delivery_failure_expiry_and_removed_owner_are_truthful(self):
        now=time.time();self.b._push.return_value=False
        self.store.add('owner',now-1,'刚才','正常提醒')
        self.store.add('owner',now-7200,'两小时前','过期提醒')
        self.store.add('removed',now-1,'刚才','非白名单')
        self.b._remind_tick(now)
        rows={h['text']:h for h in self.store.snapshot('owner')[1]}
        self.assertEqual(rows['正常提醒']['state'],'unconfirmed');self.assertEqual(rows['过期提醒']['state'],'expired')
        self.assertEqual(self.store.snapshot('removed')[1][0]['state'],'skipped')
        self.assertEqual(self.b._push.call_count,2)

    def test_dispatch_exception_does_not_drop_remaining_occurrences(self):
        now=time.time();self.b._push.side_effect=[OSError('fixture'),True]
        self.store.add('owner',now-1,'刚才','提醒一');self.store.add('owner',now-1,'刚才','提醒二')
        with self.assertLogs('bridge.remind',level='ERROR'): self.b._remind_tick(now)
        self.assertEqual({r['state'] for r in self.store.snapshot('owner')[1]}, {'uncertain','sent'})
        self.assertEqual(self.b._push.call_count,2)

    def test_queue_rejection_retries_same_daily_identity(self):
        now=time.time();row,_=self.store.add('owner',now-1,'每天09:00','重复任务',daily=dict(hour=9,minute=0),kind='task')
        with patch.object(self.b,'_enqueue',return_value='队列已满'):
            self.b._remind_tick(now)
        pending=self.store.all('owner')
        self.assertEqual(len(pending),1);self.assertEqual(pending[0]['id'],row['id']);self.assertEqual(pending[0]['daily'],row['daily'])
        self.assertEqual(pending[0]['at'],now+60);self.assertEqual(self.store.snapshot('owner')[1][0]['state'],'retry')
        self.b._remind_tick(now+61)
        self.assertEqual(self.b.queue.qsize(),1)
        self.assertGreater(self.store.all('owner')[0]['at'],now+61)

    def test_retry_cannot_overwrite_pause_or_edit(self):
        now=time.time();row,_=self.store.add('owner',now-1,'每天','每日任务',daily=dict(hour=9,minute=0),kind='task')
        fired=self.store.due(now)[0];current=self.store.all('owner')[0]
        self.store.manage('owner',current['id'],self.store.version(current),'pause')
        self.assertFalse(self.store.retry(fired,now+60));self.assertTrue(self.store.all('owner')[0]['paused'])

    def test_one_shot_retry_retains_identity_and_stopping_does_not_consume(self):
        now=time.time();row,_=self.store.add('owner',now-1,'刚才','一次性任务',kind='task')
        self.b._desktop_stop.set();self.b._remind_tick(now)
        self.assertEqual(len(self.store.all('owner')),1);self.b._desktop_stop.clear()
        with patch.object(self.b,'_enqueue',return_value='队列已满'): self.b._remind_tick(now)
        self.assertEqual(self.store.all('owner')[0]['id'],row['id'])

    def test_post_enqueue_write_failure_links_existing_receipt_after_restart(self):
        now=time.time();self.store.add('owner',now-1,'刚才','执行任务',kind='task')
        with patch.object(self.store,'record_run',side_effect=OSError('disk')), self.assertLogs('bridge.remind',level='ERROR'):
            self.b._remind_tick(now)
        self.b.reminders=ReminderStore(self.store.path)
        self.assertEqual(self.b.reminders.snapshot('owner')[1][0]['state'],'uncertain')
        row=schedule_view(self.b,'owner')['history'][0]
        self.assertEqual(row['state'],'queued');self.assertTrue(row['task_id'])
        self.b._remind_tick(now+1);self.assertEqual(self.b.queue.qsize(),1)

    def test_durable_write_failure_does_not_publish_or_trigger(self):
        self.save();before=copy.deepcopy(self.store.items);disk=(self.root/'reminders.json').read_bytes()
        with patch('remind.os.replace',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.store.due(self.plan['at']+1)
        self.assertEqual(self.store.items,before);self.assertEqual(self.store.history,[])
        self.assertEqual((self.root/'reminders.json').read_bytes(),disk)
        self.assertEqual(list(self.root.glob('reminders.json.*.tmp')),[])

    def test_legacy_id_stable_and_multiple_owners_not_truncated(self):
        raw=[dict(uid=str(i),at=time.time()+3600,label='之后',text='提醒') for i in range(12)]
        (self.root/'reminders.json').write_text(json.dumps(dict(pending=raw)),encoding='utf-8')
        one=ReminderStore(self.store.path,max_pending=1);two=ReminderStore(self.store.path,max_pending=1)
        self.assertEqual(len(one.items),12);self.assertEqual([r['id'] for r in one.items],[r['id'] for r in two.items])
        self.assertFalse(any(r['paused'] for r in one.items));self.assertNotIn('id',json.loads((self.root/'reminders.json').read_text())['pending'][0])

    def test_unreadable_or_disabled_store_does_not_break_other_pages(self):
        (self.root/'reminders.json').write_text('{bad',encoding='utf-8')
        self.b.reminders=ReminderStore(self.store.path)
        r=self.call('view');self.assertTrue(r['ok']);self.assertFalse(r['schedules']['enabled'])
        self.assertFalse(self.call('schedule_preview',plan=self.plan)['ok'])
        self.assertEqual((self.root/'reminders.json').read_text(),'{bad')
        self.b.reminders=None;self.assertTrue(self.call('view')['ok'])

    def test_malformed_history_disables_schedule_only(self):
        for row in ({}, dict(id='a'*32, schedule_id='b'*32, scheduled_at=float('nan'))):
            (self.root/'reminders.json').write_text(json.dumps(dict(pending=[], history=[row])),encoding='utf-8')
            self.b.reminders=ReminderStore(self.store.path)
            r=self.call('view');self.assertTrue(r['ok']);self.assertFalse(r['schedules']['enabled'])

    def test_standby_keeps_existing_scheduled_execution_behavior(self):
        self.b._awake.clear();self.b.standby_enabled=True
        now=time.time();self.store.add('owner',now-1,'刚才','待命中的定时任务',kind='task')
        self.b._remind_tick(now)
        self.assertEqual(self.b.queue.qsize(),1)
        self.assertEqual(schedule_view(self.b,'owner')['history'][0]['state'],'queued')

    def test_validation_duplicates_and_capacity(self):
        for changes in (dict(at=float('nan')),dict(at=True),dict(at=time.time()-1),dict(at=time.time()+8*86400),dict(text=' '*5),dict(text='x'*301),dict(kind='shell'),dict(repeat='weekly'),dict(repeat='daily',hour=24,minute=0),dict(repeat='daily',hour=True,minute=0)):
            with self.subTest(changes=changes), self.assertRaises(ScheduleError):validate_plan(dict(self.plan,**changes))
        self.save();p=self.preview()
        self.assertFalse(self.call('schedule_save',token=p['token'])['ok'])
        self.store.max_pending=1;p=self.preview(plan=dict(self.plan,text='另一件事'))
        self.assertFalse(self.call('schedule_save',token=p['token'])['ok'])

    def test_history_is_bounded_per_owner_and_delete_preserves_receipts(self):
        now=time.time()
        for i in range(55):
            self.store.add('owner',now-1,'刚才',str(i));item=self.store.due(now)[0];self.store.record_run(item,'sent','已发送')
        self.store.add('other',now-1,'刚才','他人');self.store.due(now)
        self.assertEqual(len(self.store.snapshot('owner')[1]),50);self.assertEqual(len(self.store.snapshot('other')[1]),1)
        row=self.save();self.mutate('delete',row)
        self.assertEqual(len(self.store.snapshot('owner')[1]),50)


if __name__ == '__main__':
    unittest.main()
