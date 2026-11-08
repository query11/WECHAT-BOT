"""Saved Anthropic-compatible services (api_profiles): editor service, runner and switching.

DPAPI is replaced by a reversible fake so the key paths run on any OS; no network calls.
"""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_profiles
import backend_profile as profile
import key_vault
from bridge_config import DEFAULTS, load_config
from claude_api_runner import ProfileRunner
from tests import selftest_runtime
from tools.set_backend import set_backend
from wechat_bridge import Bridge

KEY = 'kimi-offline-placeholder-123456789'


def fake_crypt(data, decrypt=False, label=''):
    return bytes(b ^ 0x5A for b in data)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / '.runtime').mkdir()
        self.path = self.root / 'config.json'
        self.write({'backend': 'claude', 'workdir': str(self.root), 'unrelated': {'keep': 1}})
        for target, value in (('key_vault._crypt', fake_crypt), ('wechat_bridge.HERE', str(self.root))):
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}); p.start(); self.addCleanup(p.stop)

    def write(self, raw):
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8-sig')

    def raw(self):
        return json.loads(self.path.read_text(encoding='utf-8-sig'))

    def save(self, **fields):
        request = dict(operation='save', provider='profile', preset='kimi', name='Kimi',
                       base_url='https://api.moonshot.cn/anthropic', model='kimi-k3', api_key=KEY,
                       revision=profile.view(self.root)['revision'])
        request.update(fields)
        return profile.execute(self.root, request)


class PresetTests(unittest.TestCase):
    def test_presets_are_https_with_valid_suggestions(self):
        names = set()
        for item in api_profiles.PRESETS:
            self.assertNotIn(item['id'], api_profiles.RESERVED)
            self.assertEqual(api_profiles.validate_url(item['base_url']), item['base_url'])
            self.assertTrue(item['models'])
            for model in item['models']:
                self.assertEqual(api_profiles.clean_model(model), model)
            names.add(item['name'])
        self.assertEqual(len(names), len(api_profiles.PRESETS))

    def test_malformed_and_duplicate_entries_are_ignored(self):
        good = {'id': 'kimi', 'name': 'Kimi', 'base_url': 'https://a.example/anthropic', 'model': 'm', 'key_file': 'a.key'}
        cfg = {'api_profiles': [good, dict(good, name='Twice'), {'id': 'claude', 'name': 'x', 'base_url': 'https://a.example', 'model': 'm'},
                                {'id': 'bad', 'name': 'B', 'base_url': 'http://plain.example', 'model': 'm'},
                                {'id': 'evil', 'name': 'E', 'base_url': 'https://a.example', 'model': 'm', 'key_file': 'config.json'}, 'junk']}
        self.assertEqual([p['name'] for p in api_profiles.profiles(cfg)], ['Kimi'])
        self.assertEqual(api_profiles.backend_ids(cfg), ['claude', 'deepseek', 'kimi'])


class EditorServiceTests(Base):
    def test_add_preset_encrypts_key_and_view_never_exposes_it(self):
        out = self.save()
        self.assertTrue(out['ok'], out); self.assertEqual(out['saved_id'], 'kimi')
        entry = self.raw()['api_profiles'][0]
        self.assertEqual((entry['id'], entry['preset'], entry['model']), ('kimi', 'kimi', 'kimi-k3'))
        self.assertTrue(entry['key_file'].startswith('.runtime/api-kimi-'))
        self.assertTrue(key_vault.is_encrypted(self.root / entry['key_file']))
        view = profile.view(self.root)
        self.assertTrue(view['profiles'][0]['configured'])
        self.assertNotIn(KEY, json.dumps(view)); self.assertNotIn(KEY, self.path.read_text(encoding='utf-8-sig'))
        self.assertEqual(self.raw()['unrelated'], {'keep': 1})
        self.assertEqual([p['id'] for p in view['presets']], list(api_profiles.PRESET_IDS))

    def test_second_preset_and_custom_get_unique_ids_and_names(self):
        self.save()
        second = self.save(name='Kimi 备用')
        custom = self.save(preset='custom', name='我的中转', base_url='https://relay.example/anthropic', model='claude-x')
        ids = [e['id'] for e in self.raw()['api_profiles']]
        self.assertEqual(ids[0], 'kimi'); self.assertTrue(ids[1].startswith('kimi-')); self.assertTrue(ids[2].startswith('custom-'))
        self.assertEqual(len(set(ids)), 3); self.assertTrue(second['ok'] and custom['ok'])
        for name in ('kimi', 'Claude', 'deepseek'):
            out = self.save(name=name)
            self.assertFalse(out['ok']); self.assertIn('同名', out['message'])

    def test_edit_keeps_key_and_rejects_unsafe_input_without_writing(self):
        self.save(); key_file = self.raw()['api_profiles'][0]['key_file']
        out = self.save(profile_id='kimi', model='kimi-k2.6', api_key='', vision=True)
        self.assertTrue(out['ok'], out)
        entry = self.raw()['api_profiles'][0]
        self.assertEqual((entry['model'], entry['key_file'], entry['vision']), ('kimi-k2.6', key_file, True))
        before = self.path.read_bytes()
        for bad in ({'base_url': 'http://api.moonshot.cn/anthropic'}, {'base_url': 'https://u:p@host.example'},
                    {'model': 'bad model'}, {'name': ''}, {'profile_id': 'missing'}, {'preset': 'unknown', 'profile_id': ''}):
            self.assertFalse(self.save(**bad)['ok'], bad)
        self.assertFalse(self.save(profile_id='', api_key='')['ok'])   # a new profile needs a key
        self.assertEqual(self.path.read_bytes(), before)

    def test_profile_limit(self):
        self.write(dict(self.raw(), api_profiles=[{'id': 'p%d' % i, 'name': 'P%d' % i, 'base_url': 'https://a.example',
                                                     'model': 'm', 'key_file': ''} for i in range(api_profiles.MAX_PROFILES)]))
        out = self.save(name='One more'); self.assertFalse(out['ok']); self.assertIn('最多', out['message'])

    def test_delete_removes_entry_and_key_but_not_the_active_backend(self):
        self.save(); self.save(preset='custom', name='中转', base_url='https://relay.example', model='m')
        kimi, relay = self.raw()['api_profiles']
        self.write(dict(self.raw(), backend='kimi'))
        refused = profile.execute(self.root, {'operation': 'delete', 'profile_id': 'kimi', 'revision': profile.view(self.root)['revision']})
        self.assertFalse(refused['ok']); self.assertIn('正在使用', refused['message'])
        out = profile.execute(self.root, {'operation': 'delete', 'profile_id': relay['id'], 'revision': profile.view(self.root)['revision']})
        self.assertTrue(out['ok'], out)
        self.assertEqual([e['id'] for e in self.raw()['api_profiles']], ['kimi'])
        self.assertFalse((self.root / relay['key_file']).exists()); self.assertTrue((self.root / kimi['key_file']).exists())
        stale = profile.execute(self.root, {'operation': 'delete', 'profile_id': 'kimi', 'revision': 'old'})
        self.assertFalse(stale['ok'])

    def test_connection_uses_the_profile_address_and_does_not_save(self):
        before = self.path.read_bytes()

        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with patch('backend_profile.urllib.request.OpenerDirector.open',
                   return_value=Response(b'{"content":[{"type":"text","text":"CONNECTION_OK"}]}')) as opened:
            out = profile.execute(self.root, dict(operation='test', provider='profile', preset='qwen', name='千问',
                                                  base_url='https://dashscope.aliyuncs.com/apps/anthropic',
                                                  model='qwen3.7-plus', api_key=KEY))
        self.assertTrue(out['ok'], out)
        self.assertEqual(opened.call_args.args[0].full_url, 'https://dashscope.aliyuncs.com/apps/anthropic/v1/messages')
        self.assertEqual(self.path.read_bytes(), before)


class RunnerAndSwitchTests(Base):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.save(vision=True)['ok'])
        self.cfg = dict(DEFAULTS, **self.raw())
        for key in ('app_launch', 'media_control', 'brightness', 'power_control', 'screen_control', 'meeting', 'remind', 'inbox'):
            self.cfg[key] = {'enable': False, 'timer_enable': False}

    def test_runner_is_isolated_per_profile(self):
        bridge = Bridge.__new__(Bridge)
        runner, name = bridge._build_runner(dict(self.cfg, backend='kimi'))
        self.assertIsInstance(runner, ProfileRunner); self.assertEqual(name, 'Kimi')
        self.assertTrue(runner.supports_vision)
        env = runner._process_options()['env']
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://api.moonshot.cn/anthropic')
        self.assertEqual((env['ANTHROPIC_MODEL'], env['ANTHROPIC_AUTH_TOKEN']), ('kimi-k3', KEY))
        self.assertEqual(Path(env['CLAUDE_CONFIG_DIR']), self.root / '.runtime' / 'api-cli' / 'kimi')
        self.assertEqual(Path(runner.session_file), self.root / '.runtime' / 'api-sessions-kimi.json')
        self.assertNotIn(KEY, runner.redact('error ' + KEY))
        with self.assertRaises(SystemExit):
            bridge._build_runner(dict(self.cfg, backend='nope'))
        unkeyed = dict(self.cfg, api_profiles=[dict(self.cfg['api_profiles'][0], key_file='')], backend='kimi')
        with self.assertRaises(SystemExit) as caught:
            bridge._build_runner(unkeyed)
        self.assertIn('Kimi', str(caught.exception))

    def test_desktop_switch_to_profile_and_back(self):
        fixture = selftest_runtime.RuntimeTests(); fixture.setUp(); self.addCleanup(fixture.temp.cleanup)
        b, rt = fixture.b, fixture.rt
        b.cfg = load_config(str(self.path)); b.backend_name = 'Claude'
        with patch('bridge_runtime.CONFIG_PATH', str(self.path)):
            self.assertIn('已切换到 Kimi', rt.command('backend:kimi'))
            snap = rt.snapshot()
            self.assertEqual((snap['backend'], snap['backend_id']), ('Kimi', 'kimi'))
            self.assertIn({'id': 'kimi', 'name': 'Kimi', 'model': 'kimi-k3'}, snap['backends'])
            self.assertEqual(self.raw()['backend'], 'kimi')
            self.assertIn('不支持', rt.command('backend:custom-unknown'))
            self.assertIn('已切换到 Claude', rt.command('backend:claude'))

    def test_cli_accepts_saved_profile_only_with_a_key(self):
        set_backend('kimi', self.path); self.assertEqual(self.raw()['backend'], 'kimi')
        with self.assertRaises(ValueError):
            set_backend('custom-missing', self.path)
        self.write(dict(self.raw(), backend='claude', api_profiles=[dict(self.raw()['api_profiles'][0], key_file='')]))
        with self.assertRaises(ValueError):
            set_backend('kimi', self.path)
        self.assertEqual(self.raw()['backend'], 'claude')


if __name__ == '__main__':
    unittest.main()
