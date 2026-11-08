"""Setup guide service, desktop binding codes and /version, offline."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import onboarding
import pairing
import updater
from bridge_config import add_allowed_user
from tests import selftest_standby


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = Path(__file__).resolve().parent.parent
        for name in ('config.example.json', 'BOT_MEMORY.example.md', 'VERSION'):
            (self.root / name).write_bytes((repo / name).read_bytes())

    def config(self):
        return json.loads((self.root / 'config.json').read_text(encoding='utf-8-sig'))


class PairingTests(Base):
    def test_code_is_single_use_and_stored_only_as_hash(self):
        code = pairing.create(self.root, now=100)['code']
        self.assertRegex(code, r'^\d{6}$')
        self.assertNotIn(code, (self.root / '.runtime' / 'pairing.json').read_text())
        self.assertTrue(pairing.status(self.root, now=101)['active'])
        self.assertIsNone(pairing.claim(self.root, '你好', 'wxid_a', now=101))
        self.assertEqual(pairing.claim(self.root, '绑定：' + code, 'wxid_owner_1234', now=102), 'paired')
        state = pairing.status(self.root, now=103)
        self.assertTrue(state['paired']); self.assertFalse(state['active']); self.assertNotIn('owner', state['paired_user'][4:-4])
        self.assertIsNone(pairing.claim(self.root, '绑定 ' + code, 'wxid_other', now=104))

    def test_wrong_attempts_and_expiry_end_the_window(self):
        code = pairing.create(self.root, now=0)['code']
        wrong = '%06d' % ((int(code) + 1) % 10 ** 6)
        for _ in range(pairing.MAX_ATTEMPTS - 1):
            self.assertEqual(pairing.claim(self.root, '/bind ' + wrong, 'x', now=1), 'wrong')
        self.assertTrue(pairing.status(self.root, now=1)['active'])
        self.assertEqual(pairing.claim(self.root, '绑定 ' + wrong, 'x', now=1), 'wrong')
        self.assertFalse(pairing.status(self.root, now=1)['active'])
        self.assertIsNone(pairing.claim(self.root, '绑定 ' + code, 'x', now=2))
        code = pairing.create(self.root, now=0)['code']
        self.assertIsNone(pairing.claim(self.root, '绑定 ' + code, 'x', now=pairing.CODE_TTL + 1))


class BridgePairingTests(Base):
    def setUp(self):
        super().setUp()
        (self.root / 'config.json').write_text(json.dumps({'allowed_user_ids': [], 'keep': 1}), encoding='utf-8-sig')
        fixture = selftest_standby.StandbyTests(); fixture.setUp()
        self.fixture, self.b = fixture, fixture.bridge
        self.b.allowed = set(); self.b.cfg = {'allowed_user_ids': []}
        for target, value in (('wechat_bridge.HERE', str(self.root)), ('wechat_bridge.CONFIG_PATH', str(self.root / 'config.json'))):
            p = patch(target, value); p.start(); self.addCleanup(p.stop)

    def last_reply(self):
        return self.b.reply.call_args.args[1]

    def test_binding_code_adds_the_sender_and_takes_effect_immediately(self):
        code = pairing.create(self.root)['code']
        self.fixture.send('绑定 ' + code, uid='wxid_new')
        self.assertIn('绑定成功', self.last_reply())
        self.assertEqual(self.b.allowed, {'wxid_new'})
        self.assertEqual(self.config()['allowed_user_ids'], ['wxid_new']); self.assertEqual(self.config()['keep'], 1)
        self.fixture.send('开始对话', uid='wxid_new')
        self.assertIn('wxid_new', self.b._chat)

    def test_wrong_code_and_no_active_code(self):
        code = pairing.create(self.root)['code']
        self.fixture.send('绑定 ' + ('%06d' % ((int(code) + 7) % 10 ** 6)), uid='wxid_x')
        self.assertIn('绑定码不对', self.last_reply()); self.assertEqual(self.b.allowed, set())
        pairing.cancel(self.root)
        self.fixture.send('绑定 ' + code, uid='wxid_x')
        self.assertIn('你的用户 ID', self.last_reply())

    def test_strangers_get_no_reply_once_someone_is_bound(self):
        self.b.allowed = {'owner'}; self.b.reply.reset_mock()
        pairing.create(self.root)
        self.fixture.send('绑定 000000', uid='stranger')
        self.fixture.send('你好', uid='stranger')
        self.b.reply.assert_not_called()

    def test_add_allowed_user_is_idempotent(self):
        path = self.root / 'config.json'
        self.assertEqual(add_allowed_user('a', path), ['a']); self.assertEqual(add_allowed_user('a', path), ['a'])
        self.assertEqual(add_allowed_user(' b ', path), ['a', 'b'])
        with self.assertRaises(ValueError):
            add_allowed_user('  ', path)


class GuideServiceTests(Base):
    def test_status_before_and_after_init(self):
        state = onboarding.status(self.root)
        self.assertFalse(state['config']['exists']); self.assertFalse(state['completed'])
        self.assertTrue(state['python']['ok']); self.assertEqual(state['version'], (self.root / 'VERSION').read_text().strip())
        work = self.root / 'data' / 'workspace'
        out = onboarding.execute(self.root, {'operation': 'init', 'workdir': str(work), 'inbox': ''})
        self.assertTrue(out['ok'], out)
        cfg = self.config()
        self.assertEqual(cfg['workdir'], str(work)); self.assertEqual(cfg['inbox']['dir'], str(work / 'inbox'))
        self.assertTrue((work / 'inbox').is_dir()); self.assertTrue((self.root / 'BOT_MEMORY.md').exists())
        self.assertEqual(cfg['prefix'], '/cc')   # everything else comes from the example
        self.assertFalse(out['config']['logged_in']); self.assertEqual(out['config']['allowed'], 0)

    def test_init_keeps_existing_settings_and_rejects_unsafe_folders(self):
        (self.root / 'config.json').write_text(json.dumps({'bot_token': 'secret', 'allowed_user_ids': ['me'], 'inbox': {'max_mb': 5}}), encoding='utf-8-sig')
        out = onboarding.execute(self.root, {'operation': 'init', 'workdir': str(self.root / 'w'), 'inbox': str(self.root / 'box')})
        self.assertTrue(out['ok'])
        cfg = self.config()
        self.assertEqual((cfg['bot_token'], cfg['allowed_user_ids'], cfg['inbox']['max_mb']), ('secret', ['me'], 5))
        self.assertNotIn('secret', json.dumps(out))
        for bad in ('', 'relative/folder', '/', 'C:\\'):
            self.assertFalse(onboarding.execute(self.root, {'operation': 'init', 'workdir': bad})['ok'], bad)

    def test_pairing_requires_login_and_completion_marker(self):
        onboarding.execute(self.root, {'operation': 'init', 'workdir': str(self.root / 'w')})
        refused = onboarding.execute(self.root, {'operation': 'pair'})
        self.assertFalse(refused['ok']); self.assertIn('扫码登录', refused['message'])
        cfg = self.config(); cfg['bot_token'] = 'token'
        (self.root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8-sig')
        out = onboarding.execute(self.root, {'operation': 'pair'})
        self.assertRegex(out['code'], r'^\d{6}$'); self.assertTrue(out['pairing']['active'])
        self.assertFalse(onboarding.execute(self.root, {'operation': 'pair_cancel'})['pairing']['active'])
        self.assertTrue(onboarding.execute(self.root, {'operation': 'complete'})['completed'])
        self.assertFalse(onboarding.execute(self.root, {'operation': 'nope'})['ok'])


class VersionCommandTests(Base):
    def test_version_text_without_and_with_a_check(self):
        self.assertIn('尚未检查更新', updater.status_text(self.root))
        (self.root / '.runtime').mkdir()
        (self.root / '.runtime' / 'update.json').write_text(json.dumps({'available': True, 'latest': 'v9.0.0', 'checked_at': 1}), encoding='utf-8')
        text = updater.status_text(self.root)
        self.assertIn('当前版本 v', text); self.assertIn('v9.0.0', text)


if __name__ == '__main__':
    unittest.main()
