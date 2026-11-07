"""Offline DeepSeek harness contract tests; never sends API requests."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from claude_runner import ClaudeRunner, TaskCancelled
from deepseek_runner import DeepSeekRunner, OFFICIAL_BASE_URL

KEY = "sk-offline-test-placeholder-12345678"


class FakeProcess:
    pid = 987654
    returncode = 0

    def __init__(self, result="done", code=0):
        self.stdout = json.dumps({"result": result, "session_id": "test-session",
                                  "modelUsage": {"deepseek-flash": {}}}).encode()
        self.stderr = b""
        self.returncode = code

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def poll(self):
        return None


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def runner(self, **overrides):
        options = dict(api_key=KEY, workdir=self.tmp.name,
                       session_file=os.path.join(self.tmp.name, "deepseek-sessions.json"),
                       config_dir=os.path.join(self.tmp.name, "deepseek-cli"))
        options.update(overrides)
        return DeepSeekRunner(**options)

    def test_child_invocation_is_official_and_host_is_unchanged(self):
        inherited = {"ANTHROPIC_BASE_URL": "https://old.invalid", "ANTHROPIC_AUTH_TOKEN": "old-token",
                     "ANTHROPIC_MODEL": "old-model", "CLAUDE_CONFIG_DIR": "old-config",
                     "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDECODE": "1",
                     "CLAUDE_CODE_OAUTH_TOKEN": "old-oauth"}
        with patch.dict(os.environ, inherited):
            before = dict(os.environ)
            runner = self.runner()
            with patch("claude_runner.subprocess.Popen", return_value=FakeProcess()) as popen:
                self.assertEqual(runner.run("me", "hello"), "done")
            cmd = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["ANTHROPIC_BASE_URL"], OFFICIAL_BASE_URL)
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], KEY)
            self.assertEqual(env["ANTHROPIC_MODEL"], "deepseek-flash")
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertNotIn("CLAUDECODE", env)
            self.assertNotIn(KEY, repr(cmd))
            self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
            self.assertIn("--strict-mcp-config", cmd)
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertEqual(dict(os.environ), before)

    def test_original_claude_has_no_environment_override(self):
        runner = ClaudeRunner(workdir=self.tmp.name,
                              session_file=os.path.join(self.tmp.name, "claude-sessions.json"))
        with patch("claude_runner.subprocess.Popen", return_value=FakeProcess()) as popen:
            runner.run("me", "hello")
        self.assertNotIn("env", popen.call_args.kwargs)
        self.assertNotIn("--setting-sources", popen.call_args.args[0])

    def test_settings_and_model_overrides_are_rejected(self):
        for option in ("--model", "--settings=x", "--setting-sources", "--fallback-model=x",
                       "--mcp-config", "--plugin-dir=x", "--agent", "--agents"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                self.runner(extra_args=[option])
        with self.assertRaises(SystemExit):
            self.runner(model="untrusted-model")
        self.assertTrue(self.runner().supports_vision)
        self.assertFalse(self.runner(model="deepseek-v4-pro").supports_vision)

    def test_results_and_stderr_redact_keys(self):
        runner = self.runner()
        with patch("claude_runner.subprocess.Popen", return_value=FakeProcess("error " + KEY)):
            self.assertNotIn(KEY, runner.run("me", "hello"))
        proc = FakeProcess(code=1)
        proc.stdout = b""
        proc.stderr = ("authorization error " + KEY).encode()
        with patch("claude_runner.subprocess.Popen", return_value=proc):
            result = runner.run("me", "hello")
        self.assertTrue(result.startswith("❌"))
        self.assertNotIn(KEY, result)

    def test_separate_session_storage_and_resume(self):
        old = os.path.join(self.tmp.name, "sessions.json")
        with open(old, "w") as out:
            out.write('{"other":"original-session"}')
        runner = self.runner()
        with patch("claude_runner.subprocess.Popen", return_value=FakeProcess()) as popen:
            runner.run("me", "first")
            runner.run("me", "next")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--resume") + 1], "test-session")
        with open(old) as source:
            self.assertEqual(json.load(source), {"other": "original-session"})
        self.assertTrue(runner.reset_session("me"))

    def test_timeout_kills_child_and_clears_handle(self):
        runner = self.runner(timeout_sec=1)
        proc = FakeProcess()
        proc.communicate = unittest.mock.Mock(side_effect=[subprocess.TimeoutExpired("fake", 1), (b"", b"")])
        runner._killer = unittest.mock.Mock(return_value=True)
        with patch("claude_runner.subprocess.Popen", return_value=proc):
            result = runner.run("me", "wait")
        self.assertIn("超过 1 秒", result)
        runner._killer.assert_called_once_with(proc.pid)
        self.assertIsNone(runner._proc)

    def test_cancellation_uses_shared_exception(self):
        runner = self.runner()
        proc = FakeProcess()
        runner._killer = unittest.mock.Mock(return_value=True)
        def complete(timeout=None):
            self.assertTrue(runner.cancel())
            return b"partial", b""
        proc.communicate = complete
        with patch("claude_runner.subprocess.Popen", return_value=proc), self.assertRaises(TaskCancelled):
            runner.run("me", "wait")
        self.assertIsNone(runner._proc)
        self.assertFalse(runner.cancel())

    def test_key_file_and_invalid_key_fail_without_echo(self):
        keyfile = os.path.join(self.tmp.name, "private.key")
        with open(keyfile, "w") as out:
            out.write(KEY)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.runner(api_key=None, key_file=keyfile)._api_key, KEY)
            for value in ("invalid-secret", "sk-secret with space"):
                with self.assertRaises(SystemExit) as caught:
                    self.runner(api_key=value)
                self.assertNotIn(value, str(caught.exception))

    def test_nested_diagnostics_redact_secrets(self):
        runner = self.runner()
        raw = json.dumps({"result": "done", "permission_denials": [
            {"tool_input": {"command": "echo " + KEY}}],
            "modelUsage": {KEY: {"note": KEY}}})
        runner._parse(raw)
        self.assertNotIn(KEY, json.dumps(runner.last_result))
        self.assertNotIn("tool_input", json.dumps(runner.last_result))
        self.assertNotIn("note", json.dumps(runner.last_result))
        self.assertEqual(runner.last_result["permission_denials"], 1)
        self.assertTrue(runner.last_result["modelUsage"])

    def test_unsafe_key_filename_rejected_even_with_explicit_key(self):
        with self.assertRaises(SystemExit):
            self.runner(key_file=os.path.join(self.tmp.name, "unsafe.txt"))


if __name__ == "__main__":
    unittest.main()
