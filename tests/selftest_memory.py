import tempfile
import unittest
import sys
from pathlib import Path
from claude_runner import ClaudeRunner


class MemoryTests(unittest.TestCase):
    def test_memory_survives_new_session_and_refreshes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'memory.md'
            path.write_text('桌面：D:\\OneDrive\\Desktop', encoding='utf-8')
            runner = ClaudeRunner(claude_bin=sys.executable, workdir=root,
                session_file=str(Path(root)/'sessions.json'), memory_file=str(path))
            for session in (None, 'existing-session'):
                prompt = runner._build_cmd(session, '查找 PPT')[-1]
                self.assertIn('D:\\OneDrive\\Desktop', prompt)
                self.assertTrue(prompt.endswith('查找 PPT'))
            path.write_text('更新后的固定记忆', encoding='utf-8')
            self.assertIn('更新后的固定记忆', runner._build_cmd(None, '请求')[-1])
            path.unlink()
            self.assertEqual(runner._build_cmd(None, '请求')[-1], '请求')


if __name__ == '__main__':
    unittest.main()
