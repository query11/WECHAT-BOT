import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from model_health import ModelHealth
from claude_runner import ClaudeRunner,TaskCancelled

class ModelHealthTests(unittest.TestCase):
    def test_unknown_until_actual_response_and_cancel_retains_previous(self):
        h=ModelHealth();self.assertEqual(h.snapshot()['state'],'unknown')
        h.begin();self.assertEqual(h.snapshot()['state'],'running');h.cancelled();self.assertEqual(h.snapshot()['state'],'unknown')
        h.finish('done',{'modelUsage':{'model':{}}},0);before=h.snapshot();self.assertEqual(before['state'],'available')
        h.begin();h.cancelled();self.assertEqual(h.snapshot()['last_success'],before['last_success'])
    def test_permission_or_task_failure_is_not_api_outage(self):
        h=ModelHealth();h.finish('❌ API 403 returned by a local command',{'is_error':False,'modelUsage':{'model':{}},'permission_denials':[{}]},0)
        self.assertEqual(h.snapshot()['state'],'available')
    def test_api_failures_are_classified_without_raw_secrets(self):
        for code,kind in [(401,'auth'),(402,'quota'),(429,'rate_limit'),(503,'service')]:
            h=ModelHealth();h.finish('API Error: %d secret-token'%code,{'is_error':True},1)
            self.assertEqual(h.snapshot()['error_kind'],kind);self.assertNotIn('secret-token',json.dumps(h.snapshot()))
    def test_local_failure_does_not_claim_remote_down(self):
        h=ModelHealth();h.finish('missing executable',returncode=1,local_error=True)
        self.assertEqual(h.snapshot()['state'],'unconfirmed');self.assertEqual(h.snapshot()['error_kind'],'local')
    def test_real_runner_contract_updates_health(self):
        with tempfile.TemporaryDirectory() as root:
            runner=ClaudeRunner(workdir=root,session_file=str(Path(root)/'sessions.json'))
            proc=Mock();proc.returncode=0;proc.communicate.return_value=(json.dumps({'result':'done','modelUsage':{'model':{'outputTokens':1}}}).encode(),b'')
            with patch('claude_runner.subprocess.Popen',return_value=proc):self.assertEqual(runner.run('owner','request'),'done')
            self.assertEqual(runner.health.snapshot()['state'],'available')
            with patch.object(runner,'_run_once',side_effect=TaskCancelled):
                with self.assertRaises(TaskCancelled):runner.run('owner','request')
            self.assertEqual(runner.health.snapshot()['state'],'available')

    def test_wechat_can_remain_online_while_model_reports_error(self):
        from tests import selftest_runtime
        from types import SimpleNamespace
        fixture=selftest_runtime.RuntimeTests();fixture.setUp();self.addCleanup(fixture.temp.cleanup)
        health=ModelHealth();health.finish('API Error: 401',{'is_error':True},1)
        fixture.b.runner=SimpleNamespace(health=health,model='demo-model')
        fixture.rt.connected();snapshot=fixture.rt.snapshot()
        self.assertEqual(snapshot['state'],'standby')
        self.assertEqual(snapshot['model_health']['state'],'error')
        self.assertEqual(snapshot['model_name'],'demo-model')

if __name__=='__main__':unittest.main()
