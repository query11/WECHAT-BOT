"""Exercise the actual hook in isolated repositories, never the user's index."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
MARKER = 'wechat-bridge pre-commit hook'
TOOL_FILES = ('pre-commit.ps1', 'pre_commit.py', 'install_hooks.ps1')
LOCAL_ENV = frozenset({
    'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
    'GIT_PREFIX', 'GIT_NAMESPACE', 'PYTHONPATH',
})
# A commit made by a fixture must never depend on the machine's git identity.
IDENTITY = ('-c', 'commit.gpgSign=false', '-c', 'user.name=Hook Test',
            '-c', 'user.email=hook-test@example.invalid')


def decode_output(data):
    """PowerShell writes redirected stdout as UTF-8 (with BOM) on Windows."""
    for encoding in ('utf-8-sig', 'utf-8', 'cp936', 'cp1252'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def real_repo_hook_state():
    """(path, sha256 or None) of the real repository's hook; read-only."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in LOCAL_ENV}
    result = subprocess.run(
        ['git', '-C', str(ROOT), 'rev-parse', '--path-format=absolute', '--git-path', 'hooks'],
        env=env, capture_output=True)
    if result.returncode != 0:
        return (None, None)
    hook = Path(os.fsdecode(result.stdout).strip()) / 'pre-commit'
    if not hook.is_file():
        return (hook, None)
    return (hook, hashlib.sha256(hook.read_bytes()).hexdigest())


@unittest.skipUnless(os.name == 'nt' and shutil.which('git'), 'Windows Git hook integration')
class HookFixtureBase(unittest.TestCase):
    """One throwaway repository per test; the subclasses pick the directory name."""

    # Subclasses that install hooks use a Chinese name with a space, because both
    # powershell -File and the sh shim have to survive it.
    REPO_DIRNAME = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wechat-hook-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base if self.REPO_DIRNAME is None else self.base / self.REPO_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)
        # Git exports these when running a real hook; they must not address the
        # original repository while a fixture creates its own commits and index.
        self.env = {k: v for k, v in os.environ.items() if k.upper() not in LOCAL_ENV}
        self.git('init', '-q')
        (self.root / 'tools').mkdir()
        for name in TOOL_FILES:
            shutil.copy2(ROOT / 'tools' / name, self.root / 'tools' / name)

    def run_command(self, *args, cwd=None):
        return subprocess.run(args, cwd=str(cwd or self.root), env=self.env,
                              capture_output=True, timeout=120)

    def git(self, *args, cwd=None):
        result = self.run_command('git', *args, cwd=cwd)
        self.assertEqual(result.returncode, 0, decode_output(result.stderr))
        return result.stdout

    def git_text(self, *args, cwd=None):
        return decode_output(self.git(*args, cwd=cwd)).strip()

    def selection(self):
        return self.run_command(sys.executable, str(self.root / 'tools' / 'pre_commit.py')).returncode

    def stage(self, name):
        (self.root / name).write_text('fixture', encoding='utf-8')
        self.git('add', '--', name)

    def commit_fixture(self):
        self.git(*IDENTITY, '-c', 'core.hooksPath=' + str(self.root / 'unused-hooks'),
                 'commit', '-qm', 'fixture')

    def hook(self):
        return self.run_command('powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                '-File', str(self.root / 'tools' / 'pre-commit.ps1'))


class HookTests(HookFixtureBase):
    """Path classification and failure propagation."""

    def test_unicode_code_path_runs_checks_and_propagates_failure(self):
        self.stage('中文.py')
        self.assertEqual(self.selection(), 0)
        (self.root / 'unittest.py').write_text(
            "from pathlib import Path\nPath('checks-ran').touch()\nraise SystemExit(1)\n", encoding='ascii')
        result = self.hook()
        self.assertTrue((self.root / 'checks-ran').exists())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(b'fast checks FAILED', result.stdout)

    def test_code_deletion_mixed_with_docs_runs_checks(self):
        self.stage('obsolete.py')
        self.commit_fixture()
        (self.root / 'obsolete.py').unlink()
        self.git('add', '-u')
        self.stage('README.md')
        self.assertEqual(self.selection(), 0)

    def test_code_renamed_to_docs_still_runs_checks(self):
        self.stage('old.py')
        self.commit_fixture()
        self.git('mv', 'old.py', 'notes.md')
        self.assertEqual(self.selection(), 0)

    def test_docs_only_skip_does_not_run_tests(self):
        self.stage('使用说明.md')
        self.assertEqual(self.selection(), 1)
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(b'no code or config changes staged', result.stdout)

    def test_manual_run_without_staged_files_runs_checks(self):
        self.assertEqual(self.selection(), 0)


class HookInstallTests(HookFixtureBase):
    """Install with the real installer, then commit through the installed hook."""

    REPO_DIRNAME = '中文 目录'

    @classmethod
    def setUpClass(cls):
        # Safety net: no test may write into the real repository's hooks.
        cls.real_hook_state = real_repo_hook_state()

    @classmethod
    def tearDownClass(cls):
        if real_repo_hook_state() != cls.real_hook_state:
            raise AssertionError('the real repository hook changed during the tests')

    # -- helpers ---------------------------------------------------------

    def initial_commit(self):
        """A real commit: "git worktree add" needs one, and so does a HEAD to move."""
        self.git('add', '--', 'tools')
        self.git(*IDENTITY, 'commit', '-qm', 'tools')


    def installer(self, *args, cwd=None, repo=None):
        target = Path(repo or self.root)
        return self.run_command('powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                '-File', str(target / 'tools' / 'install_hooks.ps1'),
                                *args, cwd=cwd)

    def install(self, *args, cwd=None, repo=None):
        result = self.installer(*args, cwd=cwd, repo=repo)
        self.assertEqual(result.returncode, 0,
                         decode_output(result.stdout) + decode_output(result.stderr))
        return result

    def output_of(self, result):
        return decode_output(result.stdout) + decode_output(result.stderr)

    def effective_hooks_dir(self, cwd=None):
        """Where Git will really look for hooks; asked of Git, never guessed."""
        result = self.run_command('git', 'rev-parse', '--path-format=absolute',
                                  '--git-path', 'hooks', cwd=cwd)
        if result.returncode != 0:
            result = self.run_command('git', 'rev-parse', '--git-path', 'hooks', cwd=cwd)
            self.assertEqual(result.returncode, 0, decode_output(result.stderr))
        path = Path(os.fsdecode(result.stdout).strip())
        if not path.is_absolute():
            path = Path(cwd or self.root) / path
        return path.resolve()

    def assert_shim(self, path):
        """The generated sh shim is usable: LF, no BOM, no machine path."""
        self.assertTrue(path.is_file(), 'no hook installed at %s' % path)
        raw = path.read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'), 'the shim starts with a BOM')
        self.assertNotIn(b'\r', raw, 'the shim must be LF-only')
        text = raw.decode('utf-8')
        self.assertIn(MARKER, text)
        self.assertIn('tools/pre-commit.ps1', text)
        self.assertIn('--show-toplevel', text)
        self.assertNotIn(str(self.base), text, 'the shim hardcodes a machine path')

    def shadow_the_test_run(self, cwd=None):
        """Make the hook's own test run fail at once, while still proving it ran."""
        target = Path(cwd or self.root)
        (target / 'unittest.py').write_text(
            "from pathlib import Path\nPath('checks-ran').touch()\nraise SystemExit(1)\n",
            encoding='ascii')
        (target / 'fixture.py').write_text('fixture\n', encoding='ascii')
        self.git('add', '--', 'fixture.py', cwd=target)

    def head_of(self, cwd):
        """The current commit, or None when the repository has no commit yet."""
        result = self.run_command('git', 'rev-parse', '--verify', 'HEAD', cwd=cwd)
        return decode_output(result.stdout).strip() if result.returncode == 0 else None

    def assert_commit_blocked_by_hook(self, cwd=None):
        target = Path(cwd or self.root)
        before = self.head_of(target)
        result = self.run_command('git', *IDENTITY, 'commit', '-qm', 'fixture', cwd=target)
        self.assertNotEqual(result.returncode, 0,
                            'a failing check must block the commit: ' + self.output_of(result))
        self.assertEqual(self.head_of(target), before,
                         'the commit was created although the check failed')
        self.assertTrue((target / 'checks-ran').is_file(),
                        'the hook never reached the tests in ' + str(target))
        return result

    def remove_worktree(self, path):
        """Cleanup for a linked worktree: never leave a registration behind."""
        if not (self.root / '.git').exists():
            return
        result = self.run_command('git', 'worktree', 'remove', '--force', str(path), cwd=self.root)
        if result.returncode != 0:
            self.run_command('git', 'worktree', 'prune', cwd=self.root)
        listing = self.git_text('worktree', 'list', cwd=self.root)
        self.assertNotIn(str(path), listing, 'a worktree registration was left behind')

    # -- scenarios -------------------------------------------------------

    def test_normal_repository_installs_the_hook_git_consults(self):
        result = self.install()
        hook = self.root / '.git' / 'hooks' / 'pre-commit'
        self.assert_shim(hook)
        self.assertEqual(self.effective_hooks_dir(), hook.parent)
        self.assertIn(str(hook), self.output_of(result))
        self.shadow_the_test_run()
        blocked = self.assert_commit_blocked_by_hook()
        self.assertIn('fast checks FAILED', self.output_of(blocked))

    def test_linked_worktree_uses_the_shared_hooks_directory(self):
        self.initial_commit()
        worktree = self.base / 'linked worktree'
        self.git('worktree', 'add', '-q', '-b', 'hook-test-branch', str(worktree))
        self.addCleanup(self.remove_worktree, worktree)
        self.assertTrue((worktree / '.git').is_file(), 'a linked worktree has a .git file')
        result = self.install(cwd=worktree, repo=worktree)
        # Git consults the shared hooks directory, not one next to the .git file.
        hook = self.root / '.git' / 'hooks' / 'pre-commit'
        self.assert_shim(hook)
        self.assertIn(str(hook), self.output_of(result))
        self.assertEqual(self.effective_hooks_dir(cwd=worktree), hook.parent)
        self.shadow_the_test_run(cwd=worktree)
        self.assert_commit_blocked_by_hook(cwd=worktree)

    def test_relative_core_hooks_path(self):
        self.git('config', 'core.hooksPath', 'myhooks')
        self.install()
        hook = self.root / 'myhooks' / 'pre-commit'
        self.assert_shim(hook)
        self.assertEqual(self.effective_hooks_dir(), hook.parent)
        self.shadow_the_test_run()
        self.assert_commit_blocked_by_hook()

    def test_absolute_core_hooks_path(self):
        hooks = self.base / 'absolute hooks'
        self.git('config', 'core.hooksPath', str(hooks))
        self.install()
        hook = hooks / 'pre-commit'
        self.assert_shim(hook)
        self.assertEqual(self.effective_hooks_dir(), hook.parent)
        self.assertFalse((self.root / '.git' / 'hooks' / 'pre-commit').exists())
        self.shadow_the_test_run()
        self.assert_commit_blocked_by_hook()

    def test_installer_from_an_unrelated_cwd_is_repeatable(self):
        caller = self.base / 'unrelated caller'
        self.run_command('git', 'init', '-q', str(caller))
        self.install(cwd=caller)
        hook = self.root / '.git' / 'hooks' / 'pre-commit'
        self.assert_shim(hook)
        self.assertFalse((caller / '.git' / 'hooks' / 'pre-commit').exists(),
                         'the installer wrote into the current directory repository')
        first = hook.read_bytes()
        again = self.install()
        self.assertEqual(hook.read_bytes(), first, 'a second install changed the hook')
        self.assertIn(str(hook), self.output_of(again))

    def test_foreign_hook_is_protected_and_only_replaced_with_force(self):
        hook = self.root / '.git' / 'hooks' / 'pre-commit'
        hook.parent.mkdir(parents=True, exist_ok=True)
        foreign = '#!/bin/sh\n# third-party hook\nexit 0\n'
        hook.write_text(foreign, encoding='utf-8', newline='')
        refused = self.installer()
        self.assertNotEqual(refused.returncode, 0, 'a foreign hook must not be replaced')
        self.assertEqual(hook.read_text(encoding='utf-8'), foreign, 'the foreign hook was rewritten')
        output = self.output_of(refused)
        self.assertIn(str(hook), output)
        self.assertIn('not installed by this project', output)
        self.install('-Force')
        self.assert_shim(hook)

    def test_installer_refuses_a_directory_no_repository_owns(self):
        plain = self.base / 'not a repository'
        (plain / 'tools').mkdir(parents=True)
        shutil.copy2(ROOT / 'tools' / 'install_hooks.ps1', plain / 'tools' / 'install_hooks.ps1')
        result = self.run_command('powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(plain / 'tools' / 'install_hooks.ps1'), cwd=plain)
        self.assertNotEqual(result.returncode, 0, self.output_of(result))
        self.assertIn('refusing to guess', self.output_of(result))
        self.assertFalse((plain / '.git').exists())


if __name__ == '__main__':
    unittest.main()
