import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from app_launcher import AppLauncher,parse_command,installed_browser_apps
from wechat_bridge import Bridge

class EdgeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.exe=self.root/'msedge.exe';self.exe.write_bytes(b'fixture')
        self.opened=[]
        self.launcher=AppLauncher(dirs=[],installed_apps={'Microsoft Edge':str(self.exe)},opener=self.opened.append)
    def test_common_names_use_fixed_executable_without_desktop_icon(self):
        for phrase in ('打开edge浏览器','打开 Edge 浏览器','打开edge','启动微软Edge浏览器','打开 Microsoft Edge'):
            action,query=parse_command(phrase);self.assertEqual(action,'open')
            text,handled=self.launcher.open(query);self.assertTrue(handled);self.assertIn('已请求打开 Microsoft Edge',text)
        self.assertEqual(self.opened,[str(self.exe)]*5)
    def test_unknown_and_compound_requests_are_not_reinterpreted(self):
        self.assertEqual(parse_command('打开edge浏览器，然后删除文件'),(None,None))
        self.assertEqual(self.launcher.match('未知浏览器'),[])
        self.assertEqual(self.launcher.match('edge --user-data-dir C:\fake'),[])
        self.assertFalse(self.opened)
    def test_desktop_exact_name_keeps_priority(self):
        shortcut=self.root/'Edge.lnk';shortcut.write_bytes(b'fixture')
        lc=AppLauncher(dirs=[str(self.root)],installed_apps={'Microsoft Edge':str(self.exe)},opener=self.opened.append)
        lc.open('edge浏览器');self.assertEqual(self.opened,[str(shortcut)])
    def test_removed_installed_app_is_not_opened(self):
        self.exe.unlink();self.launcher.refresh()
        reply,handled=self.launcher.open('edge浏览器')
        self.assertTrue(handled);self.assertIn('没找到',reply);self.assertFalse(self.opened)
    def test_gate_returns_before_file_or_model_route(self):
        b=Bridge.__new__(Bridge);b.launcher=self.launcher;b.media=None;b.runner=Mock()
        for name in ('_try_meeting','_try_remind','_try_task','_try_volume','_try_brightness','_try_power','_try_send_file','_try_click'):
            setattr(b,name,Mock(return_value=None))
        b._peek=lambda *args:'test'
        text=b._dispatch_gated(Mock(),'owner','打开edge浏览器')
        self.assertIn('已请求打开 Microsoft Edge',text)
        b._try_send_file.assert_not_called();b.runner.run.assert_not_called()
    def test_no_installed_executable_does_not_create_a_candidate(self):
        with patch('app_launcher.os.path.isfile',return_value=False):self.assertEqual(installed_browser_apps(),{})

if __name__=='__main__':unittest.main()
