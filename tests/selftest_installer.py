"""Exercise cleanup refusal before any synthetic user files can be deleted."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == 'nt', 'Windows uninstall helper')
class UninstallSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'install with spaces'
        self.desktop = self.root / 'desktop'
        self.desktop.mkdir(parents=True)
        for name in ('uninstall.ps1', 'uninstall_safety.ps1'):
            shutil.copyfile(ROOT / 'desktop' / name, self.desktop / name)
        for name in ('toggle.ps1', 'install_autostart.ps1'):
            (self.desktop / name).write_text('exit 0', encoding='ascii')
        self.memory = self.root / 'BOT_MEMORY.md'
        self.memory.write_text('keep me', encoding='utf-8')

    def run_helper(self, *args):
        return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(self.desktop / 'uninstall.ps1'), '-Quiet', *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40).returncode

    def config(self, path):
        (self.root / 'config.json').write_text(json.dumps({'workdir': str(path)}), encoding='utf-8')

    def test_stop_failure_blocks_cleanup(self):
        (self.desktop / 'toggle.ps1').write_text('exit 9', encoding='ascii')
        self.assertNotEqual(self.run_helper('-RemoveData'), 0)
        self.assertEqual(self.memory.read_text(), 'keep me')

    def test_work_folder_under_managed_directory_blocks_cleanup(self):
        work = self.root / '.runtime' / 'user-work'
        work.mkdir(parents=True)
        (work / 'notes.txt').write_text('user notes')
        self.config(work)
        self.assertNotEqual(self.run_helper('-RemoveData'), 0)
        self.assertEqual((work / 'notes.txt').read_text(), 'user notes')
        self.assertTrue(self.memory.exists())

    def test_external_work_survives_explicit_data_removal(self):
        work = Path(self.temp.name) / 'external'
        work.mkdir()
        (work / 'notes.txt').write_text('user notes')
        self.config(work)
        self.assertEqual(self.run_helper('-RemoveData'), 0)
        self.assertFalse(self.memory.exists())
        self.assertEqual((work / 'notes.txt').read_text(), 'user notes')

    def test_junction_blocks_cleanup(self):
        work = Path(self.temp.name) / 'external'
        work.mkdir()
        (work / 'notes.txt').write_text('user notes')
        link = self.root / '.runtime'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(work)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        self.assertNotEqual(self.run_helper('-RemoveData'), 0)
        self.assertEqual((work / 'notes.txt').read_text(), 'user notes')
        self.assertTrue(self.memory.exists())


if __name__ == '__main__':
    unittest.main()
