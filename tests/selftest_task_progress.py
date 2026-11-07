"""Observable progress through actual bridge hooks; no models or transports."""
import threading
import unittest
from unittest.mock import Mock, patch
from bridge_progress import TaskProgress, ProgressMixin
from task_control import TaskControl
from ilink_client import Message
from tests import selftest_standby
from tests import selftest_batch_integration


class ProgressTests(unittest.TestCase):
    def test_dispatch_clock_excludes_queue_wait_and_wall_clock_jumps(self):
        with patch('bridge_progress.time.monotonic', return_value=10):
            control = TaskControl()
        self.assertIsNone(control.progress.snapshot())
        with patch('bridge_progress.time.monotonic', return_value=100):
            control.progress.enter('prepare')
        with patch('bridge_progress.time.monotonic', return_value=110):
            control.progress.enter('model')
        with patch('bridge_progress.time.monotonic', return_value=115), patch('bridge_progress.time.time', return_value=-99999):
            sample = control.progress.snapshot()
        self.assertEqual((sample['elapsed_seconds'], sample['phase_seconds']), (15, 5))
        self.assertNotIn('percent', sample)

    def test_repeated_phase_does_not_reset_elapsed_and_only_observed_steps_listed(self):
        p = TaskProgress()
        with patch('bridge_progress.time.monotonic', return_value=5):
            p.enter('send')
        with patch('bridge_progress.time.monotonic', return_value=8):
            p.enter('send'); p.enter('unknown')
            sample = p.snapshot()
        self.assertEqual(sample['phase_seconds'], 3)
        self.assertEqual([s['phase'] for s in sample['steps']], ['send'])

    def test_batch_enters_model_after_frozen_input_then_checks_results(self):
        fixture = selftest_batch_integration.BatchIntegrationTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        b=fixture.b;b._worker=threading.current_thread()
        b._set_stage=ProgressMixin._set_stage.__get__(b)
        msg=fixture.message();msg._task_control=TaskControl()
        b._running={'label':'fixture', 'cancel':msg._task_control}
        phases=[]
        b.runner.run.side_effect=lambda *args: phases.append(msg._task_control.progress.snapshot()['phase']) or '已处理'
        b._run_batch(msg,'汇总',fixture.ready())
        self.assertEqual(phases,['model'])
        self.assertEqual([s['phase'] for s in msg._task_control.progress.snapshot()['steps']], ['input','model','check'])
        self.assertEqual(b._running['stage'],'正在核验声明文件和已配置的结果条件')

    def test_notification_does_not_fake_send_phase(self):
        f=selftest_standby.StandbyTests();f.setUp();b=f.bridge
        b._worker=threading.current_thread();b.cfg['task_progress']=True
        msg=Message({'from_user_id':'owner'});msg._task_control=TaskControl();b._running={'label':'fixture'}
        b._set_stage(msg,'模型正在处理',notify=True,phase='model')
        self.assertEqual(msg._task_control.progress.snapshot()['phase'],'model')
        b.reply.assert_called_once_with(msg,'⏳ 模型正在处理。',result=False)

    def test_runtime_uses_active_task_then_inbox_and_stops_clock_when_idle(self):
        from tests import selftest_runtime
        fixture=selftest_runtime.RuntimeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        b,rt=fixture.b,fixture.rt
        task,inbox=TaskControl(),TaskControl();task.progress.enter('model');inbox.progress.enter('receive')
        b._running={'label':'task','cancel':task};b._inbox_active={'cancel':inbox}
        self.assertEqual(rt.snapshot()['task_progress']['phase'],'model')
        task.cancel();self.assertTrue(rt.snapshot()['task_progress']['stopping'])
        b._running=None
        self.assertEqual(rt.snapshot()['task_progress']['phase'],'receive')
        b._inbox_active=None
        self.assertIsNone(rt.snapshot()['task_progress'])


if __name__ == '__main__':
    unittest.main()
