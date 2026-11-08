import threading
import time
import unittest
from unittest.mock import Mock
from task_control import TaskControl,TransferCancelled
from tests import selftest_result_feedback
from ilink_client import ILinkError

class TaskControlTests(unittest.TestCase):
    def test_cancel_waits_for_model_process_start_race(self):
        control=TaskControl();ready=threading.Event();called=threading.Event()
        runner=Mock()
        def cancel():
            if ready.is_set():called.set();return True
            return False
        runner.cancel.side_effect=cancel
        worker=threading.Thread(target=control.watch_runner,args=(runner,));worker.start()
        control.cancel();time.sleep(.1);self.assertFalse(called.is_set())
        ready.set();self.assertTrue(called.wait(1));control.finished.set();worker.join(1)
        self.assertFalse(worker.is_alive());self.assertFalse(control.cancel())
    def test_prior_unconfirmed_file_survives_later_cancel(self):
        fixture=selftest_result_feedback.ResultFeedbackTests();fixture.setUp();self.addCleanup(fixture.temp.cleanup)
        fixture.msg._task_control=TaskControl()
        fixture.b.client.send_file.side_effect=[ILinkError('lost response'),TransferCancelled(False)]
        with self.assertLogs('bridge',level='ERROR'):
            with self.assertRaises(TransferCancelled):fixture.b._send_files(fixture.msg,[str(fixture.path),str(fixture.path)])
        self.assertEqual(fixture.msg._receipt_outcome,'uncertain')
    def test_failed_model_reply_is_short_and_single_error_marker(self):
        fixture=selftest_result_feedback.ResultFeedbackTests();fixture.setUp();self.addCleanup(fixture.temp.cleanup)
        b=fixture.b;b.enable_typing=False;b.notice_after=0;b.runner=Mock()
        b.runner.run.return_value='❌ 有操作未获执行许可。\n❌ requires approval: Start-Process example\n'+'重复解释'*200
        with self.assertLogs('bridge',level='ERROR'):
            b._run_task(fixture.msg,'test')
        text=fixture.replies()
        self.assertEqual(text.count('❌'),1);self.assertLessEqual(len(text),300)
        self.assertEqual(fixture.msg._receipt_outcome,'failed')

    def test_old_cancel_watcher_finishes_before_next_task(self):
        import queue
        from tests import selftest_standby
        from ilink_client import Message
        first_started=threading.Event();finish_first=threading.Event()
        cancel_entered=threading.Event();finish_cancel=threading.Event();second_started=threading.Event()
        fixture=selftest_standby.StandbyTests();fixture.setUp();b=fixture.bridge
        class TwoQueue(queue.Queue):
            count=0
            def get(self,*args,**kwargs):
                if self.count==2:raise SystemExit()
                self.count+=1;return super().get(*args,**kwargs)
        b.queue=TwoQueue();b.runner=Mock();b.enable_typing=False;b.notice_after=0
        b.send_file_limit=3;b.send_file_enable=True;b._set_stage=Mock()
        def run(uid,prompt):
            # _run_task prepends inbox context and ARTIFACT_PROMPT; the task body is the suffix.
            if prompt.endswith('first'):first_started.set();finish_first.wait(5)
            else:second_started.set()
            return 'done'
        def cancel():cancel_entered.set();finish_cancel.wait(5);return True
        b.runner.run.side_effect=run;b.runner.cancel.side_effect=cancel
        for label in ('first','second'):
            b.queue.put((Message({'from_user_id':'owner','message_id':label}),label,None))
        worker=threading.Thread(target=b._loop_worker);b._worker=worker;worker.start()
        try:
            self.assertTrue(first_started.wait(2));b._stop_running('owner')
            self.assertTrue(cancel_entered.wait(2));finish_first.set();time.sleep(.15)
            self.assertFalse(second_started.is_set())
            finish_cancel.set();self.assertTrue(second_started.wait(2))
        finally:
            finish_first.set();finish_cancel.set();worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_runtime_and_notice_report_stopping(self):
        from tests import selftest_runtime
        from ilink_client import Message
        fixture=selftest_runtime.RuntimeTests();fixture.setUp();self.addCleanup(fixture.temp.cleanup)
        control=TaskControl();fixture.b._running={'label':'upload','stage':'uploading','cancel':control}
        self.assertEqual(fixture.rt.snapshot()['stage'],'uploading')
        control.cancel();self.assertEqual(fixture.rt.snapshot()['stage'],'正在停止当前任务')
        msg=Message({});msg._task_control=control
        self.assertIn('正在停止',fixture.b._progress_notice(msg))
    def test_failed_reply_does_not_add_body_to_default_logs(self):
        fixture=selftest_result_feedback.ResultFeedbackTests();fixture.setUp();self.addCleanup(fixture.temp.cleanup)
        b=fixture.b;b.enable_typing=False;b.notice_after=0;b.runner=Mock()
        b.runner.run.return_value='❌ private-body-fixture-12345'
        with self.assertLogs('bridge',level='ERROR') as logs:b._run_task(fixture.msg,'request')
        self.assertNotIn('private-body-fixture-12345',' '.join(logs.output))

if __name__=='__main__':unittest.main()
