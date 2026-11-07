"""Artifact fault/format regressions using isolated files, no model or WeChat."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import task_artifacts as A


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'allowed'
        self.root.mkdir()
        self.other = self.base / 'other'
        self.other.mkdir()
        self.options = {'here': self.root}

    def write(self, name='result.txt', data=b'actual output', root=None):
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def verify(self, path, **options):
        return A.validate_artifact(str(path), [str(self.root)],
                                   **dict(self.options, **options))

    def collect(self, text, **options):
        return A.collect_artifacts(text, [str(self.root)],
                                   **dict(self.options, **options))

    def mark(self, path, label='汇总表'):
        return A.MARK_ARTIFACT + json.dumps({'path': str(path), 'label': label}, ensure_ascii=False)

    def archive(self, name, members, compression=zipfile.ZIP_DEFLATED):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression) as archive:
            for key, value in members:
                archive.writestr(key, value)
        return self.write(name, buffer.getvalue())

    def test_only_explicit_single_line_declarations_are_records(self):
        path = self.write()
        prose = '完成，文件路径：\n%s\n示例 %s\n[[发文件]]%s' % (path, self.mark(path), path)
        body, records = self.collect(prose)
        self.assertEqual(body, prose)
        self.assertEqual(records, [])
        body, records = self.collect('正文\n  ' + self.mark(path) + ' \n结束')
        self.assertEqual(body, '正文\n结束')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'valid')
        self.assertEqual(records[0]['label'], '汇总表')
        self.assertEqual(records[0]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(records[0]['size'], path.stat().st_size)
        self.assertEqual(records[0]['mtime_ns'], path.stat().st_mtime_ns)
        self.assertEqual(records[0]['path'], str(path))
        json.dumps(records, ensure_ascii=False)

    def test_declaration_missing_does_not_search_or_invent(self):
        self.write()
        with patch.object(A, '_read_version', side_effect=AssertionError('must not discover')):
            self.assertEqual(self.collect('任务做完了'), ('任务做完了', []))
            self.assertEqual(self.collect(A.ARTIFACT_PROMPT)[1], [])

    def test_invalid_markers_are_stripped_with_explicit_receipts(self):
        markers = ['not-json', '[]', '{}', '{"path":12}',
                   '{"path":"a","extra":"x"}', '{"path":"a","path":"b"}',
                   '{"path":"a","label":1}', '{"path":"a","label":"\\n"}',
                   '{"path":"a","label":"' + 'x' * 81 + '"}', 'x' * 4097]
        for marker in markers:
            with self.subTest(marker=marker[:60]):
                body, records = self.collect('visible\n' + A.MARK_ARTIFACT + marker)
                self.assertEqual(body, 'visible')
                self.assertEqual(records[0]['status'], 'invalid')
                self.assertEqual(records[0]['path'], '')
                self.assertNotIn(marker, records[0]['reason'])

    def test_duplicate_and_resource_bounds(self):
        one, two, three = (self.write('%d.txt' % n) for n in range(3))
        body, records = self.collect('\n'.join([self.mark(one), self.mark(one),
                                                self.mark(two), self.mark(three)]), max_files=2)
        self.assertEqual(body, '')
        self.assertEqual([r['status'] for r in records], ['valid', 'valid', 'rejected'])
        self.assertIn('上限', records[-1]['reason'])
        oversized = self.write('big.txt', b'x' * 2000)
        self.assertEqual(self.verify(oversized, max_mb=.001)['status'], 'rejected')
        self.assertEqual(self.verify(oversized, max_mb=0)['status'], 'valid')

    def test_missing_empty_directory_and_permission(self):
        self.assertEqual(self.verify(self.root / 'missing.txt')['status'], 'missing')
        self.assertEqual(self.verify(self.write('empty.txt', b''))['status'], 'invalid')
        self.assertEqual(self.verify(self.root)['status'], 'invalid')
        path = self.write()
        with patch.object(A, '_open_file', side_effect=PermissionError('private secret path')):
            checked = self.verify(path)
        self.assertEqual(checked['status'], 'unreadable')
        self.assertNotIn('private secret', checked['reason'])

    def test_relative_parent_escape_unc_device_ads_ambiguous_names(self):
        actual = self.write()
        escaped = self.write('outside.txt', root=self.other)
        paths = ['result.txt', '../allowed/result.txt', str(escaped),
                 str(self.root / '..' / 'allowed' / 'result.txt'),
                 '\\\\server\\share\\result.txt', '\\\\?\\C:\\result.txt',
                 '\\\\.\\C:\\result.txt', str(actual) + ':secret', str(actual) + ' ',
                 str(actual) + '\x00', str(actual) + '\n']
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.verify(path)['status'], 'rejected')
        # The lexical prefix of an allowed root is not a containment check.
        prefix = self.base / 'allowed-extra'; prefix.mkdir()
        self.assertEqual(self.verify(self.write(root=prefix))['status'], 'rejected')

    def test_private_data_and_custom_protection(self):
        names = ['.env', '.env.production', 'token.key', 'credentials.json',
                 'tasks.sqlite', 'tasks.sqlite-wal', 'deepseek-api.key',
                 'config.json', 'status.json', 'bridge.log.1', 'memory.md',
                 '.runtime/unknown.txt', '.ssh/id_rsa', '.git/objects/a.txt']
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.verify(self.write(name))['status'], 'rejected')
        unrelated = self.write('report/config.json', b'{"project":"example"}', root=self.other)
        checked = A.validate_artifact(str(unrelated), [str(self.other)], here=str(self.root))
        self.assertEqual(checked['status'], 'valid')
        custom = self.write('odd-user-private.txt')
        self.assertEqual(self.verify(custom, protected_paths=[custom])['status'], 'rejected')
        directory = self.root / 'protected'
        self.assertEqual(self.verify(self.write('protected/data.txt'), protected_paths=[directory])['status'], 'rejected')

    def test_hard_link_cannot_hide_private_alias(self):
        original = self.write('config.json', b'{"not":"a real secret"}')
        alias = self.root / 'report.json'
        try:
            os.link(original, alias)
        except OSError as exc:
            self.skipTest('hard links unavailable: %s' % type(exc).__name__)
        self.assertEqual(self.verify(alias)['status'], 'rejected')

    def test_symlink_final_component_and_ancestor(self):
        target = self.write(root=self.other)
        link = self.root / 'linked.txt'
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest('symlinks require Windows privileges')
        self.assertEqual(self.verify(link)['status'], 'rejected')
        directory = self.root / 'linked-dir'
        os.symlink(self.other, directory, target_is_directory=True)
        self.assertEqual(self.verify(directory / target.name)['status'], 'rejected')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_windows_junction_cannot_bypass_boundary(self):
        target = self.write(root=self.other)
        junction = self.root / 'junction'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(self.other)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if result.returncode:
            self.skipTest('junctions unavailable')
        try:
            self.assertEqual(self.verify(junction / target.name)['status'], 'rejected')
            checked = A.validate_artifact(str(junction / target.name), [str(junction)])
            self.assertEqual(checked['status'], 'rejected')
        finally:
            os.rmdir(junction)
        self.assertEqual(target.read_bytes(), b'actual output')

    def test_archive_structure_crc_office_and_no_extraction(self):
        office = self.archive('result.xlsx', [('[Content_Types].xml', '<Types/>'),
                                              ('xl/workbook.xml', '<workbook/>'),
                                              ('xl/worksheets/sheet1.xml', '<worksheet/>')])
        self.assertEqual(self.verify(office)['status'], 'valid')
        self.assertFalse((self.root / 'xl').exists())
        wrong = self.archive('wrong.xlsx', [('content.txt', 'hello')])
        self.assertEqual(self.verify(wrong)['status'], 'invalid')
        corrupt = self.write('corrupt.xlsx', b'PK fake office document')
        self.assertEqual(self.verify(corrupt)['status'], 'invalid')
        plain = self.archive('result.zip', [('safe.txt', 'a real output')], zipfile.ZIP_STORED)
        self.assertEqual(self.verify(plain)['status'], 'valid')
        raw = bytearray(plain.read_bytes())
        offset = raw.find(b'a real output'); raw[offset] ^= 1
        plain.write_bytes(raw)
        self.assertEqual(self.verify(plain)['status'], 'invalid')

    def test_archive_rejects_traversal_credentials_and_links(self):
        for name in ['../outside.txt', 'C:/outside.txt', '/outside.txt',
                     'a:secret', '.runtime/data.txt', '.env', 'config.json']:
            with self.subTest(name=name):
                self.assertEqual(self.verify(self.archive('reject.zip', [(name, 'fake')]))['status'], 'rejected')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            entry = zipfile.ZipInfo('link'); entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, '../outside.txt')
        self.assertEqual(self.verify(self.write('link.zip', raw.getvalue()))['status'], 'rejected')

    def test_archive_bomb_and_entry_count_are_bounded(self):
        bomb = self.archive('bomb.zip', [('large.txt', '0' * 200000)])
        self.assertEqual(self.verify(bomb)['status'], 'rejected')
        many = self.archive('many.zip', [('x%d' % n, 'a') for n in range(A.MAX_ARCHIVE_ENTRIES + 1)])
        self.assertEqual(self.verify(many)['status'], 'invalid')
        expanded = self.archive('expanded.zip', [('data.bin', b'x' * 200000)], zipfile.ZIP_STORED)
        with patch.object(A, 'MAX_ARCHIVE_EXPANDED', 1000):
            self.assertEqual(self.verify(expanded)['status'], 'rejected')

    def test_corrupt_compression_stream_is_an_invalid_receipt(self):
        path = self.archive('corrupt-stream.zip', [('file.txt', 'payload')])
        with patch.object(A.zipfile.ZipFile, 'open', side_effect=zlib.error('invalid stream with private path')):
            record = self.verify(path)
        self.assertEqual(record['status'], 'invalid')
        self.assertNotIn('private path', record['reason'])

    def test_json_text_pdf_and_image_headers(self):
        self.assertEqual(self.verify(self.write('data.json', b'{"ok":true}'))['status'], 'valid')
        self.assertEqual(self.verify(self.write('bad.json', b'{broken}'))['status'], 'invalid')
        self.assertEqual(self.verify(self.write('binary.txt', b'a\x00b'))['status'], 'invalid')
        self.assertEqual(self.verify(self.write('utf16.txt', '中文'.encode('utf-16')))['status'], 'valid')
        self.assertEqual(self.verify(self.write('gbk.csv', '标题,数据\n甲,12'.encode('gb18030')))['status'], 'valid')
        self.assertEqual(self.verify(self.write('report.pdf', b'%PDF-1.7\nplaceholder\n%%EOF'))['status'], 'valid')
        self.assertEqual(self.verify(self.write('fake.pdf', b'not a PDF'))['status'], 'invalid')
        chunk = lambda kind, data: struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))
        self.assertEqual(self.verify(self.write('image.png', png))['status'], 'valid')
        self.assertEqual(self.verify(self.write('fake.png', b'not image'))['status'], 'invalid')
        unknown = self.verify(self.write('data.unknown', b'nonempty bytes'))
        self.assertEqual(unknown['status'], 'valid')
        self.assertEqual(unknown['format'], 'unverified')
        self.assertIn('未校验此文件格式', unknown['reason'])
        self.assertTrue(unknown['sha256'])

    def test_xml_entities_and_damaged_core_structure(self):
        self.assertEqual(self.verify(self.write('figure.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>'))['status'], 'valid')
        self.assertEqual(self.verify(self.write('entity.xml', b'<!DOCTYPE x [<!ENTITY a "123">]><x>&a;</x>'))['status'], 'invalid')
        self.assertEqual(self.verify(self.write('utf16-entity.xml', '<!DOCTYPE x [<!ENTITY a "123">]><x>&a;</x>'.encode('utf-16')))['status'], 'invalid')
        office = self.archive('damaged.docx', [('[Content_Types].xml', '<Types/>'),
                                               ('word/document.xml', '<broken>')])
        self.assertEqual(self.verify(office)['status'], 'invalid')

    def test_revalidate_changed_same_size_hash_and_missing(self):
        path = self.write('version.txt', b'first version')
        record = self.verify(path)
        kwargs = {'here': self.root}
        self.assertEqual(A.revalidate_artifact(record, [self.root], **kwargs)['status'], 'valid')
        previous = path.stat()
        path.write_bytes(b'other version')
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        changed = A.revalidate_artifact(record, [self.root], **kwargs)
        self.assertEqual(changed['status'], 'changed')
        self.assertEqual(changed['sha256'], record['sha256'])
        self.assertEqual(record['status'], 'valid')
        path.unlink()
        self.assertEqual(A.revalidate_artifact(record, [self.root], **kwargs)['status'], 'missing')

    def test_revalidate_mtime_changed_even_when_bytes_identical(self):
        path = self.write()
        record = self.verify(path)
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
        self.assertEqual(A.revalidate_artifact(record, [self.root], **self.options)['status'], 'changed')

    def test_revalidate_original_failed_or_tampered_receipt(self):
        path = self.write()
        record = self.verify(path)
        for invalid in [None, {}, dict(record, status='unsupported'), dict(record, sha256='')]:
            self.assertEqual(A.revalidate_artifact(invalid, [self.root], **self.options)['status'], 'rejected')
        tampered = dict(record, path=str(self.write(root=self.other)))
        self.assertEqual(A.revalidate_artifact(tampered, [self.root], **self.options)['status'], 'rejected')

    def test_read_race_is_explicit_and_not_false_success(self):
        path = self.write()
        original = A._fingerprint
        calls = []
        def altered(info):
            calls.append(info)
            value = original(info)
            return tuple([value[0], value[1] + 1, *value[2:]]) if len(calls) == 3 else value
        with patch.object(A, '_fingerprint', side_effect=altered):
            checked = self.verify(path)
        self.assertEqual(checked['status'], 'changed')

    def test_strip_markers_never_reads_files_or_trusts_declarations(self):
        path = self.root / 'does-not-exist.txt'
        with patch.object(A, '_read_version', side_effect=AssertionError('must not verify failed output')):
            body, declarations = A.strip_artifact_marks('failed result\n' + self.mark(path) + '\n[[产物]]broken')
        self.assertEqual(body, 'failed result')
        self.assertEqual(declarations, [{'path': str(path), 'label': '汇总表'}])

    def test_unknown_format_still_has_version_checked_snapshot(self):
        path = self.write('recorded.bin', b'\x00\x01\xff')
        record = self.verify(path)
        self.assertEqual(record['status'], 'valid')
        self.assertEqual(record['format'], 'unverified')
        with A.verified_artifact_snapshot(record, [self.root], self.root / 'staging', **self.options) as (checked, temporary):
            self.assertEqual(checked['status'], 'valid')
            self.assertEqual(Path(temporary).read_bytes(), b'\x00\x01\xff')

    def test_snapshot_sends_recorded_bytes_and_cleans_on_error(self):
        path = self.write('deliver.txt', b'recorded version')
        record = self.verify(path)
        staging = self.root / '.runtime' / 'artifact-transfers'
        snapshot = None
        with self.assertRaisesRegex(RuntimeError, 'transport failed'):
            with A.verified_artifact_snapshot(record, [self.root], staging, **self.options) as (checked, temporary):
                self.assertEqual(checked['status'], 'valid')
                snapshot = Path(temporary)
                self.assertEqual(snapshot.name, 'deliver.txt')
                path.write_bytes(b'edited after verification')
                self.assertEqual(snapshot.read_bytes(), b'recorded version')
                raise RuntimeError('transport failed')
        self.assertFalse(snapshot.exists())
        self.assertEqual(list(staging.iterdir()), [])

    def test_snapshot_rejects_changed_missing_out_of_scope_or_unwritable(self):
        path = self.write()
        record = self.verify(path)
        outside = self.other / 'staging'
        with A.verified_artifact_snapshot(record, [self.root], outside, **self.options) as (checked, temporary):
            self.assertEqual(checked['status'], 'rejected'); self.assertIsNone(temporary)
        self.assertFalse(outside.exists())
        with patch.object(A.os, 'makedirs', side_effect=PermissionError('private path')):
            with A.verified_artifact_snapshot(record, [self.root], self.root / 'staging', **self.options) as (checked, temporary):
                self.assertEqual(checked['status'], 'unreadable'); self.assertIsNone(temporary)
                self.assertNotIn('private path', checked['reason'])
        path.write_bytes(b'changed version')
        with A.verified_artifact_snapshot(record, [self.root], self.root / 'staging', **self.options) as (checked, temporary):
            self.assertEqual(checked['status'], 'changed'); self.assertIsNone(temporary)


if __name__ == '__main__':
    unittest.main(verbosity=2)
