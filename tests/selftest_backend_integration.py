"""Exercise actual runner selection without starting worker or network clients."""
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bridge_config import DEFAULTS
from wechat_bridge import Bridge
from claude_runner import ClaudeRunner
from deepseek_runner import DeepSeekRunner

class BackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'.runtime').mkdir()
        (self.root/'api.key').write_text('sk-offline-placeholder-123456789',encoding='utf-8')
        self.cfg=dict(DEFAULTS,workdir=str(self.root),deepseek={'key_file':'api.key','model':'deepseek-flash'})
        for key in ('app_launch','media_control','brightness','power_control','screen_control','meeting','remind','inbox'):
            self.cfg[key]={'enable':False,'timer_enable':False}
        self.bridge=Bridge.__new__(Bridge)
        self.scope=patch('wechat_bridge.HERE',str(self.root));self.scope.start();self.addCleanup(self.scope.stop)

    def test_real_factory_switch_keeps_independent_sessions(self):
        old=self.root/'sessions.json';old.write_text(json.dumps({'owner':'old-claude-session'}),encoding='utf-8')
        before=old.read_bytes()
        ds=self.bridge._build_config_state(dict(self.cfg,backend='deepseek'))
        self.assertIsInstance(ds['runner'],DeepSeekRunner)
        self.assertEqual(ds['backend_name'],'DeepSeek')
        self.assertNotEqual(Path(ds['runner'].session_file),old)
        cl=self.bridge._build_config_state(dict(self.cfg,backend='claude'))
        self.assertIs(type(cl['runner']),ClaudeRunner)
        self.assertEqual(old.read_bytes(),before)
        self.assertEqual(cl['runner']._sessions['owner'],'old-claude-session')

    def test_unknown_provider_fails_without_mutating_bridge(self):
        self.bridge.runner='old-runner'
        with self.assertRaises(SystemExit):self.bridge._build_config_state(dict(self.cfg,backend='typo'))
        self.assertEqual(self.bridge.runner,'old-runner')

    def test_missing_key_cannot_silently_fallback(self):
        self.bridge.runner='old-runner'
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
            with self.assertRaises(SystemExit):
                self.bridge._build_config_state(dict(self.cfg,backend='deepseek',deepseek={'key_file':'missing.key'}))
        self.assertEqual(self.bridge.runner,'old-runner')

if __name__=='__main__':unittest.main()
