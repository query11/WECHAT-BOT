"""Isolated discovery regressions: no real shortcuts, launches, or taskkill."""
import tempfile
import unittest
from pathlib import Path

from app_launcher import AppLauncher


class AppDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.desktop = self.root / 'Desktop'
        self.menu = self.root / 'Programs'
        self.desktop.mkdir()
        self.menu.mkdir()
        self.targets = {}
        self.opened = []
        self.closed = []

    def executable(self, relative):
        path = self.root / 'installed' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture, never executed')
        return path

    def shortcut(self, directory, relative, target):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture, resolved by injected resolver')
        self.targets[str(path)] = str(target)
        return str(path)

    def launcher(self):
        return AppLauncher(
            dirs=[str(self.desktop)], start_menu_dirs=[str(self.menu)],
            validate_shortcuts=True,
            resolver=lambda paths: {p: self.targets[p] for p in paths if p in self.targets},
            opener=self.opened.append,
            closer=lambda exe: (self.closed.append(exe), (0, 'fixture'))[1])

    def test_stale_chinese_shortcut_does_not_hide_valid_weixin(self):
        expected = self.shortcut(self.desktop, 'Weixin.lnk', self.executable('Weixin.exe'))
        self.shortcut(self.desktop, '微信助手.lnk', self.executable('powershell.exe'))
        self.shortcut(self.menu, '微信/微信.lnk', self.root / 'missing' / 'Weixin.exe')
        launcher = self.launcher()
        for query in ('微信', 'WeChat', 'Weixin', '微信客户端', '微信电脑版'):
            with self.subTest(query=query):
                reply, handled = launcher.open(query)
                self.assertTrue(handled)
                self.assertIn('已请求打开', reply)
                self.assertEqual(self.opened[-1], expected)
        launcher.close('微信')
        self.assertEqual(self.closed, ['Weixin.exe'])

    def test_missing_client_never_falls_back_to_helper(self):
        self.shortcut(self.desktop, '微信助手.lnk', self.executable('powershell.exe'))
        launcher = self.launcher()
        for query in ('微信', 'wechat', 'weixin'):
            self.assertEqual(launcher.match(query), [])
            for action in (launcher.open, launcher.close):
                reply, handled = action(query)
                self.assertTrue(handled)
                self.assertIn('没找到', reply)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.closed, [])

    def test_nested_office_entries_have_aliases(self):
        expected = {}
        for name in ('Word', 'Excel', 'PowerPoint'):
            expected[name] = self.shortcut(self.menu, 'Office/Nested/Microsoft ' + name + '.lnk',
                                           self.executable(name + '.exe'))
        launcher = self.launcher()
        for query, name in (('微软Word', 'Word'), ('Excel表格', 'Excel'), ('PPT', 'PowerPoint')):
            launcher.open(query)
            self.assertEqual(self.opened[-1], expected[name])

    def test_menu_filters_startup_uninstall_help_and_non_apps(self):
        app = self.executable('app.exe')
        self.shortcut(self.menu, 'Startup/Auto.lnk', app)
        self.shortcut(self.menu, '启动/自动.lnk', app)
        self.shortcut(self.menu, '卸载工具.lnk', app)
        self.shortcut(self.menu, 'Product Help.lnk', app)
        self.shortcut(self.menu, 'Product/Remove.lnk', self.executable('uninstall.exe'))
        self.shortcut(self.menu, 'Product/Console.lnk', self.executable('powershell.exe'))
        self.shortcut(self.menu, 'Product/Notes.lnk', self.executable('notes.txt'))
        self.assertEqual(self.launcher().names(), [])

    def test_same_target_duplicates_are_one_match(self):
        app = self.executable('Word.exe')
        first = self.shortcut(self.desktop, 'Word.lnk', app)
        self.shortcut(self.menu, 'Office/Word.lnk', app)
        self.shortcut(self.menu, 'Office/Microsoft Word.lnk', app)
        launcher = self.launcher()
        self.assertEqual(launcher.match('微软Word'), [('Word', first)])
        launcher.open('微软Word')
        self.assertEqual(self.opened, [first])

    def test_same_name_different_targets_requires_disambiguation(self):
        self.shortcut(self.desktop, 'Editor.lnk', self.executable('first/editor.exe'))
        second = self.shortcut(self.menu, 'Suite/Editor.lnk', self.executable('second/editor.exe'))
        launcher = self.launcher()
        self.assertEqual(len(launcher.match('Editor')), 2)
        self.assertEqual(launcher.names(), ['Editor（1）', 'Editor（2）'])
        for action in (launcher.open, launcher.close):
            reply, handled = action('Editor')
            self.assertTrue(handled)
            self.assertIn('说全一点', reply)
            self.assertIn('Editor（1）', reply)
            self.assertIn('Editor（2）', reply)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.closed, [])
        reply, handled = launcher.open('Editor（2）')
        self.assertTrue(handled)
        self.assertIn('已请求打开', reply)
        self.assertEqual(self.opened, [second])
        self.assertEqual(self.closed, [])

    def test_direct_startup_root_is_excluded(self):
        self.shortcut(self.menu, 'Startup/Auto.lnk', self.executable('app.exe'))
        launcher = AppLauncher(
            dirs=[], start_menu_dirs=[str(self.menu / 'Startup')],
            validate_shortcuts=True,
            resolver=lambda paths: {p: self.targets[p] for p in paths},
            opener=self.opened.append)
        self.assertEqual(launcher.names(), [])
        self.assertEqual(self.opened, [])

    def test_invalid_cached_process_name_never_reaches_closer(self):
        path = self.shortcut(self.desktop, 'Editor.lnk', self.executable('editor.exe'))
        launcher = self.launcher()
        for target in ('C:\\Apps\\*.exe', 'C:\\Apps\\editor?.exe', 'C:\\Apps\\notes.txt'):
            with self.subTest(target=target):
                launcher._targets[path] = {'target': target, 'mtime': launcher._mtime(path)}
                reply, handled = launcher.close('Editor')
                self.assertTrue(handled)
                self.assertTrue(reply.startswith('❌'))
        self.assertEqual(self.closed, [])

    def test_typo_is_not_guessed_and_helper_close_is_protected(self):
        self.shortcut(self.desktop, 'Visual Studio Code.lnk', self.executable('Code.exe'))
        self.shortcut(self.desktop, '微信助手.lnk', self.executable('powershell.exe'))
        launcher = self.launcher()
        self.assertEqual(launcher.match('Visual Studio Codx'), [])
        reply, handled = launcher.close('微信助手')
        self.assertTrue(handled)
        self.assertIn('不能关', reply)
        self.assertEqual(self.closed, [])


if __name__ == '__main__':
    unittest.main()
