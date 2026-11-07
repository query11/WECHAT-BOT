"""Actual desktop backend switch, isolated config and sessions, no model requests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import selftest_runtime
from bridge_config import load_config
from claude_runner import ClaudeRunner
from deepseek_runner import DeepSeekRunner
from bridge_runtime import atomic_json

class RuntimeBackendTests(unittest.TestCase):
    def setUp(self):
        fixture=selftest_runtime.RuntimeTests();fixture.setUp()
        self.b=fixture.b;self.rt=fixture.rt;self.temp=fixture.temp
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'.runtime').mkdir()
        self.path=self.root/'config.json'
        self.raw={'workdir':str(self.root),'backend':'claude','keep_context':False,
                  'deepseek':{'model':'deepseek-flash','key_file':'secret.key'}}
        self.path.write_text(json.dumps(self.raw),encoding='utf-8-sig')
        (self.root/'secret.key').write_text('sk-offline-test-placeholder-12345678',encoding='utf-8')
        self.b.cfg=load_config(str(self.path));self.b.backend_name='Claude';self.old=object();self.b.runner=self.old
        for name,value in [('bridge_runtime.CONFIG_PATH',str(self.path)),('wechat_bridge.HERE',str(self.root))]:
            p=patch(name,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'DEEPSEEK_API_KEY':''});p.start();self.addCleanup(p.stop)

    def test_switch_roundtrip_preserves_sessions_and_raw_config(self):
        session=self.root/'sessions.json';session.write_text('{"owner":"old"}',encoding='utf-8')
        ds_session=self.root/'.runtime/deepseek-sessions.json';ds_session.write_text('{"owner":"other"}',encoding='utf-8')
        before=(session.read_bytes(),ds_session.read_bytes())
        self.assertIn('已切换到 DeepSeek',self.rt.command('backend:deepseek'))
        self.assertIsInstance(self.b.runner,DeepSeekRunner);self.assertFalse(self.b.runner.keep_context)
        self.assertEqual(self.rt.snapshot()['backend'],'DeepSeek')
        expected=dict(self.raw,backend='deepseek')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8-sig')),expected)
        self.assertIn('已切换到 Claude',self.rt.command('backend:claude'))
        self.assertIs(type(self.b.runner),ClaudeRunner)
        self.assertEqual(before,(session.read_bytes(),ds_session.read_bytes()))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8-sig')),self.raw)

    def test_busy_queued_dequeued_inbox_and_draining_refused(self):
        before=self.path.read_bytes()
        for queue in (self.b.queue,self.b._inbox_q):
            queue.put('pending')
            self.assertFalse(self.rt.snapshot()['backend_switch_available'])
            self.assertIn('还有任务',self.rt.command('backend:deepseek'))
            queue.get()
            self.assertIn('还有任务',self.rt.command('backend:deepseek'))
            queue.task_done()
        self.b._draining.add('owner')
        self.assertIn('还有任务',self.rt.command('backend:deepseek'))
        self.assertEqual(before,self.path.read_bytes());self.assertIs(self.b.runner,self.old)

    def test_missing_key_and_write_failure_preserve_active_state(self):
        before=self.path.read_bytes()
        with patch('bridge_runtime.os.replace',side_effect=OSError('disk full')):
            self.assertIn('失败',self.rt.command('backend:deepseek'))
        self.assertEqual(before,self.path.read_bytes());self.assertIs(self.b.runner,self.old)
        (self.root/'secret.key').unlink()
        self.assertIn('失败',self.rt.command('backend:deepseek'))
        self.assertEqual(before,self.path.read_bytes());self.assertIs(self.b.runner,self.old)
        self.assertFalse(list(self.root.glob('.backend-ui-*.tmp')))

    def test_unrelated_disk_edit_requires_reload(self):
        self.path.write_text(json.dumps(dict(self.raw,timeout_sec=321)),encoding='utf-8-sig')
        before=self.path.read_bytes()
        self.assertIn('其他未加载',self.rt.command('backend:deepseek'))
        self.assertEqual(before,self.path.read_bytes());self.assertIs(self.b.runner,self.old)

    def test_command_file_reports_actual_switched_backend(self):
        import time
        path=self.root/'command-backend.json'
        atomic_json(path,{'pid':os.getpid(),'at':time.time(),'action':'backend:deepseek'})
        self.rt.tick()
        self.assertIn('DeepSeek',json.loads((self.root/'reply-backend.json').read_text(encoding='utf-8'))['message'])
        self.assertEqual(json.loads((self.root/'status.json').read_text(encoding='utf-8'))['backend'],'DeepSeek')

    def test_profile_reload_applies_only_model_settings(self):
        changed=dict(self.raw,deepseek={'model':'deepseek-v4-pro','key_file':'secret.key'})
        self.path.write_text(json.dumps(changed),encoding='utf-8-sig')
        self.assertIn('已保存并应用',self.rt.command('profile:reload'))
        self.assertEqual(self.b.cfg['deepseek']['model'],'deepseek-v4-pro')
        self.assertEqual(self.b.backend_name,'Claude')
        self.assertFalse(self.b.runner.keep_context)

    def test_profile_reload_busy_and_invalid_keep_live_runner(self):
        self.b.queue.put('task')
        self.assertIn('当前有任务',self.rt.command('profile:reload'))
        self.assertIs(self.b.runner,self.old)
        self.b.queue.get();self.b.queue.task_done()
        raw=dict(self.raw,backend='deepseek');self.path.write_text(json.dumps(raw),encoding='utf-8-sig')
        self.b.cfg=load_config(str(self.path));(self.root/'secret.key').unlink()
        self.assertIn('未能应用',self.rt.command('profile:reload'))
        self.assertIs(self.b.runner,self.old)

    def test_profile_reload_picks_replaced_active_key(self):
        raw=dict(self.raw,backend='deepseek');self.path.write_text(json.dumps(raw),encoding='utf-8-sig')
        self.b.cfg=load_config(str(self.path))
        (self.root/'new.key').write_text('sk-replaced-offline-placeholder-12345678',encoding='utf-8')
        changed=dict(raw,deepseek={'model':'deepseek-flash','key_file':'new.key'})
        self.path.write_text(json.dumps(changed),encoding='utf-8-sig')
        self.assertIn('已保存并应用',self.rt.command('profile:reload'))
        self.assertEqual(self.b.runner._api_key,'sk-replaced-offline-placeholder-12345678')
        self.assertEqual(self.b.backend_name,'DeepSeek')

if __name__=='__main__':unittest.main()
