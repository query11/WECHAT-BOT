"""Version comparison and the GitHub release updater, offline with a fake GitHub."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import app_version
import updater

REPO = 'owner/WECHAT-BOT'


def make_zip(files, prefix='WECHAT-BOT-1.2.0/', extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, text in files.items():
            z.writestr(prefix + name, text)
        for info, data in (extra or []):
            z.writestr(info, data)
    return buf.getvalue()


PROGRAM = {'VERSION': '1.2.0\n', 'wechat_bridge.py': 'new bridge', 'tray_app.ps1': 'new tray',
           'launch_tray.ps1': 'new launcher', 'updater.py': 'new updater', 'docs/new.md': 'new doc',
           'requirements.txt': 'qrcode\n'}


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {'Content-Length': str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    """Routes requested URLs to canned bytes; records what was requested."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def open(self, request, timeout=None):
        url = request.full_url
        self.seen.append(url)
        if url not in self.routes:
            raise AssertionError('unexpected request ' + url)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return Response(value if isinstance(value, bytes) else json.dumps(value).encode())


def release(tag='v1.2.0', assets=None, body='修复若干问题'):
    return {'tag_name': tag, 'name': 'WECHAT-BOT ' + tag, 'body': body, 'published_at': '2026-12-01T08:00:00Z',
            'html_url': 'https://github.com/%s/releases/tag/%s' % (REPO, tag),
            'zipball_url': 'https://api.github.com/repos/%s/zipball/%s' % (REPO, tag), 'assets': assets or []}


class VersionTests(unittest.TestCase):
    def test_ordering_and_parsing(self):
        self.assertTrue(app_version.newer('v1.2.0', '1.1.9'))
        self.assertTrue(app_version.newer('1.2.0', '1.2.0-beta.2'))
        self.assertTrue(app_version.newer('1.2.0-beta.2', '1.2.0-beta.1'))
        self.assertTrue(app_version.newer('1.2.0-beta.1', '1.2.0-alpha'))
        self.assertFalse(app_version.newer('v1.2.0', '1.2.0'))
        self.assertFalse(app_version.newer('latest', '1.0.0'))
        self.assertIsNone(app_version.parse('1.2'))
        self.assertTrue(app_version.is_prerelease('v2.0.0-rc.1'))
        self.assertEqual(app_version.display('1.0.0'), 'v1.0.0')

    def test_repository_version_file_is_valid(self):
        self.assertIsNotNone(app_version.parse(app_version.read_version()))
        self.assertNotEqual(app_version.read_version(), '0.0.0')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, text in {'VERSION': '1.0.0\n', 'wechat_bridge.py': 'old bridge', 'tray_app.ps1': 'old tray',
                           'launch_tray.ps1': 'old launcher', 'updater.py': 'old updater', 'old_module.py': 'obsolete',
                           'requirements.txt': 'qrcode\n', 'config.json': '{"update":{"repo":"%s"}}' % REPO,
                           'BOT_MEMORY.md': 'my memory', 'local_note.txt': 'not ours'}.items():
            (self.root / name).write_text(text, encoding='utf-8')
        (self.root / '.runtime').mkdir()
        (self.root / '.runtime' / 'tasks.sqlite').write_text('db', encoding='utf-8')


class CheckTests(Base):
    def test_newer_release_is_reported_and_cached(self):
        github = FakeGitHub({'https://api.github.com/repos/%s/releases/latest' % REPO: release()})
        state = updater.check(self.root, opener=github, now=1000)
        self.assertTrue(state['available']); self.assertEqual((state['latest'], state['current']), ('v1.2.0', '1.0.0'))
        self.assertEqual(state['install_kind'], 'archive'); self.assertIsNone(state['asset'])
        again = updater.check(self.root, opener=github, now=1000 + 60)
        self.assertEqual(len(github.seen), 1); self.assertEqual(again['latest'], 'v1.2.0')
        updater.set_preference(self.root, skip='v1.2.0')
        self.assertTrue(updater.check(self.root, opener=github, force=True, now=2000)['skipped'])

    def test_same_version_errors_and_git_checkouts(self):
        github = FakeGitHub({'https://api.github.com/repos/%s/releases/latest' % REPO: release('v1.0.0')})
        self.assertFalse(updater.check(self.root, opener=github, now=1)['available'])
        failing = FakeGitHub({'https://api.github.com/repos/%s/releases/latest' % REPO: OSError('offline')})
        state = updater.check(self.root, opener=failing, force=True, now=2)
        self.assertIn('无法连接', state['error']); self.assertFalse(state['available'])
        (self.root / '.git').mkdir()
        github = FakeGitHub({'https://api.github.com/repos/%s/releases/latest' % REPO: release()})
        state = updater.check(self.root, opener=github, force=True, now=3)
        self.assertEqual(state['install_kind'], 'git')
        with patch('updater.check', return_value=state):
            with self.assertRaises(updater.UpdateError):
                updater.prepare(self.root, opener=github)

    def test_disabled_and_bad_repo_setting(self):
        (self.root / 'config.json').write_text('{"update":{"enabled":false,"repo":"../evil"}}', encoding='utf-8')
        self.assertEqual(updater.update_config(self.root)['repo'], updater.DEFAULT_REPO)
        self.assertTrue(updater.check(self.root, opener=FakeGitHub({}), now=1)['disabled'])

    def test_only_github_https_hosts(self):
        for url in ('http://api.github.com/x', 'https://evil.example/x', 'https://github.com.evil.example/x'):
            with self.assertRaises(updater.UpdateError):
                updater._check_url(url)
        handler = updater._GitHubOnly()
        with self.assertRaises(updater.UpdateError):
            handler.redirect_request(None, None, 302, 'Found', {}, 'https://evil.example/payload.zip')


class ArchiveTests(Base):
    def test_extract_strips_top_folder_and_skips_local_data(self):
        data = make_zip(dict(PROGRAM, **{'config.json': '{"stolen":1}', '.runtime/x.json': '{}', 'keys/a.key': 'k'}))
        path = self.root / 'a.zip'; path.write_bytes(data)
        out = updater.extract(path, self.root / 'out')
        self.assertEqual((out / 'wechat_bridge.py').read_text(), 'new bridge')
        for name in ('config.json', '.runtime/x.json', 'keys/a.key'):
            self.assertFalse((out / name).exists(), name)

    def test_unsafe_members_are_rejected(self):
        link = zipfile.ZipInfo('WECHAT-BOT/link'); link.external_attr = 0o120777 << 16
        for extra in ([(zipfile.ZipInfo('../escape.py'), 'x')], [(zipfile.ZipInfo('/abs.py'), 'x')],
                      [(zipfile.ZipInfo('C:/win.py'), 'x')], [(link, 'target')]):
            path = self.root / 'bad.zip'; path.write_bytes(make_zip({'VERSION': '1'}, extra=extra))
            with self.assertRaises(updater.UpdateError):
                updater.extract(path, self.root / 'out')
        (self.root / 'junk.zip').write_bytes(b'not a zip')
        with self.assertRaises(updater.UpdateError):
            updater.extract(self.root / 'junk.zip', self.root / 'out')


class PrepareApplyTests(Base):
    def github(self, data, digest=None, sums=None, version_tag='v1.2.0'):
        asset_url = 'https://github.com/%s/releases/download/%s/WECHAT-BOT-1.2.0.zip' % (REPO, version_tag)
        assets = [{'name': 'WECHAT-BOT-1.2.0.zip', 'browser_download_url': asset_url, 'size': len(data), 'digest': digest or ''}]
        routes = {'https://api.github.com/repos/%s/releases/latest' % REPO: None, asset_url: data}
        if sums is not None:
            sums_url = 'https://github.com/%s/releases/download/%s/SHA256SUMS.txt' % (REPO, version_tag)
            assets.append({'name': 'SHA256SUMS.txt', 'browser_download_url': sums_url})
            routes[sums_url] = sums.encode()
        routes['https://api.github.com/repos/%s/releases/latest' % REPO] = release(version_tag, assets)
        return FakeGitHub(routes)

    def test_prepare_verifies_checksum_then_apply_replaces_program_files_only(self):
        data = make_zip(PROGRAM)
        sha = hashlib.sha256(data).hexdigest()
        prepared = updater.prepare(self.root, opener=self.github(data, sums='%s  WECHAT-BOT-1.2.0.zip\n' % sha))
        self.assertTrue(prepared['verified']); self.assertEqual(prepared['version'], '1.2.0')
        (self.root / '.runtime' / 'update' / 'manifest.json').write_text(json.dumps({'files': ['old_module.py', 'wechat_bridge.py']}))
        result = updater.apply(self.root, prepared['staging'], restart=False)
        self.assertTrue(result['ok']); self.assertEqual((result['from'], result['to']), ('1.0.0', '1.2.0'))
        self.assertEqual((self.root / 'wechat_bridge.py').read_text(), 'new bridge')
        self.assertEqual((self.root / 'docs' / 'new.md').read_text(), 'new doc')
        self.assertFalse((self.root / 'old_module.py').exists())                 # removed: listed in the old manifest
        self.assertEqual((self.root / 'local_note.txt').read_text(), 'not ours')  # unknown file: kept
        self.assertEqual((self.root / 'BOT_MEMORY.md').read_text(), 'my memory')
        self.assertIn('"repo"', (self.root / 'config.json').read_text())
        self.assertTrue((self.root / '.runtime' / 'tasks.sqlite').exists())
        self.assertEqual((Path(result['backup']) / 'wechat_bridge.py').read_text(), 'old bridge')
        manifest = json.loads((self.root / '.runtime' / 'update' / 'manifest.json').read_text())
        self.assertIn('docs/new.md', manifest['files']); self.assertEqual(manifest['version'], '1.2.0')
        self.assertTrue(json.loads((self.root / '.runtime' / 'update' / 'result.json').read_text())['ok'])

    def test_bad_checksum_version_mismatch_and_missing_files_are_refused(self):
        data = make_zip(PROGRAM)
        with self.assertRaises(updater.UpdateError) as caught:
            updater.prepare(self.root, opener=self.github(data, digest='sha256:' + '0' * 64))
        self.assertIn('校验失败', str(caught.exception))
        with self.assertRaises(updater.UpdateError):
            updater.prepare(self.root, opener=self.github(make_zip(dict(PROGRAM, VERSION='1.1.0'))))
        broken = dict(PROGRAM); broken.pop('tray_app.ps1')
        with self.assertRaises(updater.UpdateError):
            updater.prepare(self.root, opener=self.github(make_zip(broken)))
        self.assertEqual((self.root / 'wechat_bridge.py').read_text(), 'old bridge')

    def test_failure_during_copy_restores_the_previous_version(self):
        data = make_zip(PROGRAM)
        prepared = updater.prepare(self.root, opener=self.github(data, digest='sha256:' + hashlib.sha256(data).hexdigest()))
        real, calls = updater.shutil.copy2, []

        def flaky(src, dst, *a, **k):
            calls.append(dst)
            if len(calls) == 6:
                raise OSError('disk full')
            return real(src, dst, *a, **k)
        with patch('updater.shutil.copy2', side_effect=flaky):
            with self.assertRaises(updater.UpdateError):
                updater.apply(self.root, prepared['staging'], restart=False)
        self.assertEqual((self.root / 'wechat_bridge.py').read_text(), 'old bridge')
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.0\n')
        self.assertFalse((self.root / 'docs' / 'new.md').exists())
        self.assertFalse(json.loads((self.root / '.runtime' / 'update' / 'result.json').read_text())['ok'])

    def test_apply_refuses_foreign_staging_and_running_processes(self):
        with self.assertRaises(updater.UpdateError):
            updater.apply(self.root, self.root, restart=False)
        data = make_zip(PROGRAM)
        prepared = updater.prepare(self.root, opener=self.github(data))
        with self.assertRaises(updater.UpdateError):
            updater.apply(self.root, prepared['staging'], wait_pids=[os.getpid()], restart=False, timeout=0.5)
        self.assertEqual((self.root / 'wechat_bridge.py').read_text(), 'old bridge')


if __name__ == '__main__':
    unittest.main()
