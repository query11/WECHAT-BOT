"""Isolated preference validation/persistence checks; never discovers local apps."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from app_preferences import load_preferences, save_preferences, catalog_from_launcher, _write_lock


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'app_preferences.json'
        self.launcher = SimpleNamespace(
            _apps=[('微信', 'wx.lnk'), ('微信助手', 'helper.lnk'), ('Editor', 'editor.lnk')],
            _identities={'wx.lnk': 'wx.exe', 'helper.lnk': 'helper.exe', 'editor.lnk': 'editor.exe'},
            _base_names={'wx.lnk': '微信', 'helper.lnk': '微信助手', 'editor.lnk': 'editor'})

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, aliases=None, favorites=None, revision=0):
        return save_preferences(self.path, {'favorites': favorites or [], 'aliases': aliases or {}}, self.launcher, revision)

    def test_round_trip_and_priority(self):
        self.save({'编辑器': 'editor.exe'}, ['editor.exe'])
        prefs = load_preferences(self.path)
        self.assertEqual(prefs['revision'], 1)
        record = catalog_from_launcher(self.launcher, prefs)[0]
        self.assertEqual(record['id'], 'editor.exe')
        self.assertEqual(record['aliases'], ['编辑器'])

    def test_revision_conflict_does_not_write(self):
        self.save()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.save({'编辑器': 'editor.exe'})
        self.assertEqual(before, self.path.read_bytes())

    def test_alias_attacks_and_reserved_names(self):
        for alias in ['微信', 'wechat', '微信客户端', 'Editor', 'edge', '打开;cmd', 'x/y', r'x\y', 'a:b', 'a&b', 'a'*21, '']:
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                self.save({alias: 'helper.exe'})
        self.save({'微信': 'wx.exe'})

    def test_normalized_duplicate(self):
        with self.assertRaises(ValueError):
            self.save({'my editor': 'editor.exe', 'my-editor': 'wx.exe'})

    def test_unknown_target_and_stale_display(self):
        with self.assertRaises(ValueError):
            self.save({'新应用': 'arbitrary.exe'})
        with self.assertRaises(ValueError):
            self.save(favorites=['arbitrary.exe'])
        rows = catalog_from_launcher(self.launcher, {'favorites': ['stale'], 'aliases': {'旧应用': 'stale'}})
        stale = next(r for r in rows if r['id'] == 'stale')
        self.assertFalse(stale['available'])

    def test_corrupt_file_fail_closed(self):
        self.path.write_text('{broken', encoding='utf-8')
        self.assertEqual(load_preferences(self.path)['aliases'], {})

    def test_stale_lock_file_does_not_block(self):
        Path(str(self.path)+'.lock').touch()
        self.save()
        self.assertTrue(self.path.exists())

    def test_non_routable_alias_is_rejected(self):
        with self.assertRaises(ValueError):self.save({'文档工具':'editor.exe'})

    def test_concurrent_lock_blocks_write(self):
        with _write_lock(self.path):
            with self.assertRaises(ValueError):
                self.save()
        self.assertFalse(self.path.exists())


class LiveLauncherPreferencesTests(unittest.TestCase):
    def test_live_alias_reload_and_stale_target_fail_closed(self):
        from app_launcher import AppLauncher
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);exe=root/'Editor.exe';exe.write_bytes(b'fixture')
            prefs=root/'prefs.json';opened=[]
            launcher=AppLauncher(dirs=[str(root)],opener=opened.append,preferences_file=str(prefs))
            identity=launcher._identities[str(exe)]
            save_preferences(prefs,{'favorites':[identity],'aliases':{'写作工具':identity}},launcher,0)
            self.assertIn('常用应用',launcher.describe())
            self.assertTrue(launcher.open('写作工具')[1]);self.assertEqual(opened,[str(exe)])
            exe.unlink();launcher.refresh()
            text,handled=launcher.open('写作工具');self.assertTrue(handled);self.assertIn('没找到',text)
            self.assertEqual(opened,[str(exe)])
    def test_tampered_reserved_alias_cannot_launch_helper(self):
        import json
        from app_launcher import AppLauncher
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);exe=root/'微信助手.exe';exe.write_bytes(b'fixture');prefs=root/'prefs.json';opened=[]
            launcher=AppLauncher(dirs=[str(root)],opener=opened.append,preferences_file=str(prefs))
            prefs.write_text(json.dumps({'version':1,'revision':1,'favorites':[],'aliases':{'微信':launcher._identities[str(exe)]}}),encoding='utf-8')
            self.assertTrue(launcher.open('微信')[1]);self.assertFalse(opened)

if __name__ == '__main__':
    unittest.main()
