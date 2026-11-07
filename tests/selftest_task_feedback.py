"""Pure formatting checks: no credentials, network, or real side effects."""
import unittest

from task_feedback import compact_failure


class TaskFeedbackTests(unittest.TestCase):
    def assert_reply(self, reply):
        self.assertTrue(reply.startswith('❌ '))
        self.assertEqual(reply.count('❌'), 1)
        self.assertLessEqual(len(reply), 300)
        self.assertIn('\n', reply)

    def test_partial_permission_does_not_claim_rollback_or_total_failure(self):
        for value in ('permission_denials: [screenshot]', 'approval required',
                      '❌❌ 有操作未获执行许可\n❌ 截图失败\n浏览器已打开'):
            with self.subTest(value=value):
                reply = compact_failure(value)
                self.assert_reply(reply)
                self.assertIn('任务未全部完成', reply)
                self.assertIn('已完成的操作不会自动撤销', reply)
                self.assertIn('/apps', reply)
                self.assertNotIn('操作未执行', reply)
                self.assertNotIn('✅', reply)

    def test_auth_codes_and_words(self):
        for value in ('HTTP 401 Unauthorized', 'HTTP 403 Forbidden', 'invalid_api_key', '认证失败'):
            reply = compact_failure(value, '测试服务')
            self.assert_reply(reply)
            self.assertIn('测试服务认证或访问权限异常', reply)

    def test_quota_rate_and_timeout(self):
        for value, expected in (('HTTP 402', '额度或余额不足'), ('余额不足', '额度或余额不足'),
                                ('insufficient_quota', '额度或余额不足'), ('HTTP 429', '请求过于频繁'),
                                ('rate_limit_exceeded', '请求过于频繁'), ('ReadTimeout', '响应超时'),
                                ('request timed out', '响应超时'), ('上传超时', '响应超时')):
            with self.subTest(value=value):
                reply = compact_failure(value)
                self.assert_reply(reply)
                self.assertIn(expected, reply)

    def test_generic_retains_first_concrete_cause(self):
        reply = compact_failure('❌ **请求失败**\n❌ Error: database is locked\n'
                                'Traceback (most recent call last):\n'
                                '  File "private.py", line 2\nRuntimeError: wrapper failed')
        self.assert_reply(reply)
        self.assertIn('database is locked', reply)
        self.assertNotIn('Traceback', reply)
        self.assertNotIn('private.py', reply)
        self.assertNotIn('wrapper failed', reply)

    def test_json_error_is_extracted_without_dump(self):
        reply = compact_failure('{"error": {"message": "invalid field: destination"}, "debug": {"large": 123}}')
        self.assert_reply(reply)
        self.assertIn('invalid field: destination', reply)
        self.assertNotIn('debug', reply)
        self.assertNotIn('{', reply)

    def test_empty_denial_list_does_not_override_actual_reason(self):
        reply = compact_failure('{"permission_denials": [], "error": {"message": "database is locked"}}')
        self.assertIn('database is locked', reply)
        self.assertNotIn('缺少执行权限', reply)

    def test_file_markers_never_leak_local_paths(self):
        for marker in ('[[发文件]] D:\\private\\report.docx',
                       '[[发文件:D:\\private\\report.docx]]',
                       '[[发文件\nD:\\private\\report.docx\n]]'):
            reply = compact_failure('Error: export failed\n' + marker)
            self.assert_reply(reply)
            self.assertIn('export failed', reply)
            self.assertNotIn('[[', reply)
            self.assertNotIn('private', reply)
            self.assertNotIn('report.docx', reply)

    def test_credentials_are_redacted_in_generic_error(self):
        reply = compact_failure('Error: failed with sk-fake_123-abc and Authorization: Bearer fake-token-123')
        self.assert_reply(reply)
        self.assertNotIn('sk-fake', reply)
        self.assertNotIn('fake-token-123', reply)
        self.assertIn('[已隐藏]', reply)

    def test_basic_authorization_is_redacted(self):
        reply = compact_failure('Error: rejected Authorization=BasicZmFrZTpwYXNz')
        self.assertNotIn('BasicZmFrZTpwYXNz', reply)

    def test_long_unknown_failure_is_bounded(self):
        reply = compact_failure('❌ Error: ' + '服务异常' * 1000)
        self.assert_reply(reply)
        self.assertIn('查看日志', reply)

    def test_empty_or_raw_dump_has_useful_fallback(self):
        for value in (None, '', '{}', '{"debug": "raw output"}'):
            reply = compact_failure(value)
            self.assert_reply(reply)
            self.assertIn('未识别错误', reply)
            self.assertIn('查看日志', reply)


if __name__ == '__main__':
    unittest.main()
