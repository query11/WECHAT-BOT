"""运行源码身份：证明"后台加载的是不是磁盘上的新版本"能区分三种状态。

覆盖：
- 启动身份固定，之后刷新不会把基线改写成磁盘上的新版本；
- 已加载源码变化能被发现（含未提交的改动）；
- 只提交、源码没变时不误报；
- 配置、文档、日志、未 import 的文件、项目外的文件都不在范围内；
- 没有 Git、Git 查询失败、超时、文件暂时读不到、旧状态缺字段都降级成"未知"，
  绝不默认"已是最新"，也不抛异常打断状态线程。
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import bridge_runtime
from bridge_runtime import (Runtime, git_revision, imported_source_fingerprint)
from tests import selftest_standby


class FingerprintTests(unittest.TestCase):
    """范围函数本身：只统计"已经 import 过的、项目根以内的 .py"。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='身份-测试 theidentity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '项目 root'      # 中文 + 空格路径
        (self.root / 'pkg').mkdir(parents=True)
        self.write('bridge_like.py', 'a = 1\n')
        self.write('pkg/inner.py', 'b = 2\n')
        self.write('notes.md', '# 文档\n')
        self.write('config.json', '{}\n')
        self.write('selftest_like.py', 'c = 3\n')           # 存在，但没有 import
        self.outside = Path(self.temp.name) / 'other.py'
        self.outside.write_text('x = 1\n', encoding='utf-8')

    def write(self, rel, text):
        path = self.root / rel
        path.write_text(text, encoding='utf-8')
        return path

    def modules(self):
        """假 sys.modules：把想考察的文件挂成模块。"""
        return {
            'bridge_like': SimpleNamespace(__file__=str(self.root / 'bridge_like.py')),
            'inner': SimpleNamespace(__file__=str(self.root / 'pkg' / 'inner.py')),
            'notes': SimpleNamespace(__file__=str(self.root / 'notes.md')),
            'config': SimpleNamespace(__file__=str(self.root / 'config.json')),
            'outside': SimpleNamespace(__file__=str(self.outside)),
            'no_file': SimpleNamespace(),
            'not_a_module': None,
        }

    def test_scope_is_only_imported_python_inside_the_project(self):
        found = imported_source_fingerprint(self.root, self.modules())
        self.assertEqual(sorted(found), ['bridge_like.py', 'pkg/inner.py'])
        # 没 import 的文件（哪怕就在项目里）不在范围内
        self.assertNotIn('selftest_like.py', found)
        # 文档和配置永远不在范围内
        self.assertNotIn('notes.md', found)
        self.assertNotIn('config.json', found)

    def test_editing_docs_config_or_unimported_files_changes_nothing(self):
        modules = self.modules()
        before = imported_source_fingerprint(self.root, modules)
        self.write('notes.md', '# 改了内容，长度也不一样了\n')
        self.write('config.json', '{"changed": true, "more": 1}\n')
        self.write('selftest_like.py', 'c = 3000\n')
        self.outside.write_text('x = 100000\n', encoding='utf-8')
        self.assertEqual(before, imported_source_fingerprint(self.root, modules))

    def test_a_new_commit_with_identical_source_is_not_a_change(self):
        # git 变更但不改源码：指纹必须一模一样（提交号本身不足以证明代码变了）
        modules = self.modules()
        before = imported_source_fingerprint(self.root, modules)
        with patch('bridge_runtime.git_revision', return_value='feedbee'):
            self.assertEqual(before, imported_source_fingerprint(self.root, modules))

    def test_editing_an_imported_source_file_is_detected(self):
        modules = self.modules()
        before = imported_source_fingerprint(self.root, modules)
        self.write('pkg/inner.py', 'b = 22222\n')
        after = imported_source_fingerprint(self.root, modules)
        self.assertNotEqual(before['pkg/inner.py'], after['pkg/inner.py'])
        self.assertEqual(before['bridge_like.py'], after['bridge_like.py'])

    def test_deleted_or_unreadable_source_does_not_raise(self):
        modules = self.modules()
        (self.root / 'bridge_like.py').unlink()
        self.assertEqual(imported_source_fingerprint(self.root, modules)['bridge_like.py'],
                         'missing')
        self.write('bridge_like.py', 'a = 1\n')
        real_stat = os.stat

        def deny(path, *args, **kwargs):
            if not args and not kwargs and str(path).endswith('bridge_like.py'):
                raise PermissionError(13, 'permission denied')
            return real_stat(path, *args, **kwargs)

        with patch('bridge_runtime.os.stat', side_effect=deny):
            found = imported_source_fingerprint(self.root, modules)
        self.assertEqual(found['bridge_like.py'], 'unreadable')

    def test_real_scope_stays_inside_the_project(self):
        # 在后台的导入环境验证范围；统一入口自身的 __main__ 位于 tools/，
        # 直接检查测试进程会把验证脚本也算进去，不能据此推断后台加载了它。
        done = subprocess.run(
            [sys.executable, '-c',
             'import json, wechat_bridge; '
             'from bridge_runtime import imported_source_fingerprint; '
             'print(json.dumps(imported_source_fingerprint()))'],
            cwd=Path(bridge_runtime.__file__).resolve().parent,
            capture_output=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', 'replace'))
        found = json.loads(done.stdout.decode('utf-8'))
        self.assertIn('bridge_runtime.py', found)
        self.assertTrue(all(name.endswith('.py') for name in found), sorted(found))
        self.assertFalse([n for n in found if n.startswith('tools/')], sorted(found))
        self.assertFalse([n for n in found if ':' in n or n.startswith('/')], sorted(found))
        self.assertTrue(all('/' in n or '.' in n for n in found))


class GitRevisionTests(unittest.TestCase):
    def test_missing_git_times_out_and_failures_all_degrade_to_none(self):
        with patch('bridge_runtime.subprocess.run', side_effect=FileNotFoundError('no git')):
            self.assertIsNone(git_revision())
        with patch('bridge_runtime.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('git', 1)):
            self.assertIsNone(git_revision())
        with patch('bridge_runtime.subprocess.run',
                   return_value=Mock(returncode=128, stdout=b'')):
            self.assertIsNone(git_revision())
        with patch('bridge_runtime.subprocess.run',
                   return_value=Mock(returncode=0, stdout=b'   \n')):
            self.assertIsNone(git_revision())

    def test_successful_query_returns_the_short_revision(self):
        with patch('bridge_runtime.subprocess.run',
                   return_value=Mock(returncode=0, stdout=b'abc1234\n')):
            self.assertEqual(git_revision(), 'abc1234')

    def test_real_repository_is_readable(self):
        # 本仓库就是 Git 仓库：真实查询要么给出短提交号，要么如实说不知道。
        revision = git_revision()
        self.assertTrue(revision is None or (0 < len(revision) <= 12), revision)


class CodeIdentityStateTests(unittest.TestCase):
    """状态机：基线固定、三种状态、失败降级、刷新有节流。"""

    def setUp(self):
        fixture = selftest_standby.StandbyTests()
        fixture.setUp()
        self.b = fixture.bridge
        self.b._mask = lambda s: s
        self.b._worker = Mock()
        self.b._worker.is_alive.return_value = True
        self.b._started_at = 1
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fingerprint = {'a.py': '1:1', 'b.py': '3:3'}
        self.fingerprint_patch = patch('bridge_runtime.imported_source_fingerprint',
                                       side_effect=lambda *a, **k: dict(self.fingerprint))
        self.fingerprint_patch.start()
        self.addCleanup(self.fingerprint_patch.stop)
        self.git_patch = patch('bridge_runtime.git_revision', return_value='abc1234')
        self.git_patch.start()
        self.addCleanup(self.git_patch.stop)
        self.rt = Runtime(self.b, self.temp.name)

    def recheck(self):
        with patch('bridge_runtime.IDENTITY_REFRESH_SECONDS', 0):
            return self.rt.code_identity()

    def test_startup_state_is_same_and_carries_the_loaded_commit(self):
        result = self.rt.code_identity()
        self.assertEqual(result['state'], 'same')
        self.assertEqual(result['message'], '')
        self.assertEqual(result['commit'], 'abc1234')

    def test_source_change_is_reported_without_rewriting_the_baseline(self):
        self.assertEqual(self.rt.code_identity()['state'], 'same')
        self.fingerprint['a.py'] = '99:99'
        result = self.recheck()
        self.assertEqual(result['state'], 'changed')
        self.assertEqual(result['message'], '代码已更新，重启后生效')
        # 基线仍是启动时那一份：磁盘上的新版本不会把它顶掉
        self.assertEqual(self.rt._identity_baseline['a.py'], '1:1')
        # 提示只播报一次，不刷屏
        self.assertEqual(len([e for e in self.rt.events if '代码已更新' in e]), 1)
        self.recheck()
        self.assertEqual(len([e for e in self.rt.events if '代码已更新' in e]), 1)

    def test_a_new_commit_alone_does_not_claim_the_code_changed(self):
        with patch('bridge_runtime.git_revision', return_value='feedbee'):
            result = self.recheck()
        self.assertEqual(result['state'], 'same')
        self.assertEqual(result['commit'], 'abc1234')   # 显示的是启动时加载的那一版

    def test_unreadable_scope_reports_unknown_not_up_to_date(self):
        with patch('bridge_runtime.imported_source_fingerprint',
                   side_effect=OSError('disk gone')):
            result = self.recheck()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['message'], '')
        self.assertIn('无法读取', result['detail'])

    def test_empty_scope_reports_unknown_not_up_to_date(self):
        self.rt._identity_baseline = {}
        result = self.recheck()
        self.assertEqual(result['state'], 'unknown')
        self.assertNotEqual(result['state'], 'same')

    def test_a_temporarily_unreadable_source_reports_unknown(self):
        self.fingerprint['b.py'] = 'unreadable'
        result = self.recheck()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['message'], '')
        self.assertIn('读不到', result['detail'])

    def test_late_module_stays_unknown_without_replacing_the_startup_baseline(self):
        baseline = dict(self.rt._identity_baseline)
        self.fingerprint['late.py'] = '5:5'
        for _ in range(2):
            result = self.recheck()
            self.assertEqual(result['state'], 'unknown')
            self.assertIn('加载时版本', result['detail'])
        self.assertEqual(self.rt._identity_baseline, baseline)
        # 模块从 sys.modules 中移除，不代表对它的函数引用已不再被使用。
        del self.fingerprint['late.py']
        self.assertEqual(self.recheck()['state'], 'unknown')

    def test_known_change_remains_visible_with_unverified_or_unreadable_sources(self):
        self.fingerprint['late.py'] = '5:5'
        self.fingerprint['a.py'] = '99:99'
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                if unreadable:
                    self.fingerprint['b.py'] = 'unreadable'
                self.assertEqual(self.recheck()['state'], 'changed')

    def test_unreadable_startup_fingerprint_cannot_become_a_verified_baseline(self):
        self.fingerprint['a.py'] = 'unreadable'
        rt = Runtime(self.b, self.temp.name)
        self.fingerprint['a.py'] = '9:9'
        with patch('bridge_runtime.IDENTITY_REFRESH_SECONDS', 0):
            self.assertEqual(rt.code_identity()['state'], 'unknown')
            self.assertEqual(rt.code_identity()['state'], 'unknown')

    def test_real_late_import_edited_before_poll_does_not_claim_same(self):
        root = Path(self.temp.name) / 'source'
        root.mkdir()
        anchor = root / 'anchor.py'
        anchor.write_text('value = 0\n', encoding='utf-8')
        modules = {'anchor': SimpleNamespace(__file__=str(anchor))}

        def load_late():
            spec = importlib.util.spec_from_file_location('identity_late_fixture', root / 'late.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules['late'] = module
            return module

        with patch('bridge_runtime.imported_source_fingerprint',
                   side_effect=lambda: imported_source_fingerprint(root, modules)), \
             patch('bridge_runtime.IDENTITY_REFRESH_SECONDS', 0):
            rt = Runtime(self.b, self.temp.name)
            self.assertEqual(rt.code_identity()['state'], 'same')
            (root / 'late.py').write_text('value = 1\n', encoding='utf-8')
            loaded = load_late()
            (root / 'late.py').write_text('value = 123456\n', encoding='utf-8')
            self.assertEqual(loaded.value, 1)
            self.assertEqual(rt.code_identity()['state'], 'unknown')
            self.assertEqual(rt.code_identity()['state'], 'unknown')
            self.assertNotIn('late.py', rt._identity_baseline)
            # 重新加载源码后再建立 Runtime，等价于重启时的加载顺序。
            self.assertEqual(load_late().value, 123456)
            restarted = Runtime(self.b, self.temp.name)
            self.assertEqual(restarted.code_identity()['state'], 'same')

    def test_refresh_is_throttled_so_polling_does_not_rescan_the_project(self):
        calls = []

        def counting(*args, **kwargs):
            calls.append(1)
            return dict(self.fingerprint)

        with patch('bridge_runtime.imported_source_fingerprint', side_effect=counting):
            self.rt.code_identity()
            self.rt.code_identity()
            self.rt.code_identity()
        self.assertEqual(len(calls), 1)

    def test_status_snapshot_exposes_code_identity_for_the_tray(self):
        data = self.rt.snapshot()
        self.assertIn('code_identity', data)
        self.assertEqual(data['code_identity']['state'], 'same')
        self.assertEqual(data['code_identity']['commit'], 'abc1234')
        self.fingerprint['a.py'] = '99:99'
        changed = self.recheck()
        self.assertEqual(changed['state'], 'changed')
        self.assertTrue(bridge_runtime.IDENTITY_REFRESH_SECONDS > 0)


if __name__ == '__main__':
    unittest.main()
