"""Offline result-contract regressions: synthetic CLI output, no live providers."""
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from claude_api_runner import ClaudeAPIRunner
from claude_runner import ClaudeRunner
from deepseek_runner import DeepSeekRunner
from wechat_bridge import Bridge


KEY = "sk-structured-offline-placeholder-12345678"
BACKENDS = ("claude", "deepseek", "claude_api")
SHAPES = ("object", "jsonl", "array")


class StructuredResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, self.old_logging)

    def runner(self, backend, keep_context=False):
        options = dict(claude_bin=sys.executable, workdir=str(self.root),
                       session_file=str(self.root / (backend + "-sessions.json")),
                       keep_context=keep_context)
        if backend == "claude":
            return ClaudeRunner(**options)
        options.update(api_key=KEY, config_dir=str(self.root / (backend + "-cli")))
        if backend == "deepseek":
            return DeepSeekRunner(**options)
        return ClaudeAPIRunner(base_url="https://example.invalid", model="offline-model", **options)

    @staticmethod
    def output(payload, shape, tail=None):
        if shape == "object":
            return json.dumps(payload)
        events = [{"type": "system", "session_id": "wrong-start"}, payload]
        if tail is not None:
            events.append(tail)
        return ("\n".join(json.dumps(event) for event in events) if shape == "jsonl"
                else json.dumps(events))

    @staticmethod
    def payload(text="Completed requested work", denied=False, **extra):
        value = {"type": "result", "result": text, "is_error": False,
                 "session_id": "final-session", "modelUsage": {"offline-model": {"outputTokens": 1}},
                 "permission_denials": [{"tool_name": "Bash", "tool_input": {
                     "command": "synthetic offline operation"}}] if denied else []}
        value.update(extra)
        return value

    def run_bridge(self, runner, raw, code=0, stderr=b""):
        bridge = Bridge.__new__(Bridge)
        bridge.cfg = {}
        bridge.backend_name = runner.backend_name
        bridge.enable_typing = False
        bridge.notice_after = 0
        bridge.send_file_enable = True
        bridge.send_file_limit = 3
        bridge.send_file_max_mb = 20
        bridge.send_roots = [str(self.root)]
        bridge._set_stage = Mock()
        bridge._recent_inbox_context = Mock(return_value="")
        bridge._mask = str
        bridge.reply = Mock()
        bridge.client = Mock()
        bridge.runner = runner
        message = SimpleNamespace(from_user_id="owner", context_token="synthetic-context",
                                  _receipt_outcome="completed")
        process = Mock(returncode=code)
        process.communicate.return_value = (raw.encode("utf-8"), stderr)
        with patch.object(runner, "_process_options", return_value={}), \
             patch("claude_runner.subprocess.Popen", return_value=process):
            bridge._run_task(message, "Perform synthetic work")
        return bridge, message

    def test_permission_matrix_through_bridge_with_and_without_prose(self):
        for backend in BACKENDS:
            for shape in SHAPES:
                for denied in (False, True):
                    for text in ("Completed requested work", ""):
                        with self.subTest(backend=backend, shape=shape, denied=denied, text=text):
                            runner = self.runner(backend)
                            bridge, message = self.run_bridge(runner, self.output(self.payload(text, denied), shape))
                            self.assertEqual(message._receipt_outcome, "failed" if denied else "completed")
                            reply = bridge.reply.call_args.args[1]
                            self.assertEqual(reply.startswith("❌"), denied)
                            if denied:
                                self.assertIn("执行权限", reply)
                            bridge.client.send_file.assert_not_called()
                            health = runner.health.snapshot()
                            self.assertEqual(health["state"], "available")
                            self.assertEqual("操作被拒绝" in health["detail"], denied)

    def test_final_result_wins_over_trailing_non_result_events(self):
        tail = {"type": "system", "text": "unrelated tail", "session_id": "wrong-tail",
                "is_error": True, "permission_denials": [], "modelUsage": {}}
        for backend in BACKENDS:
            for shape in ("jsonl", "array"):
                with self.subTest(backend=backend, shape=shape):
                    runner = self.runner(backend, keep_context=True)
                    bridge, message = self.run_bridge(runner, self.output(self.payload(denied=True), shape, tail))
                    self.assertEqual(message._receipt_outcome, "failed")
                    self.assertEqual(runner._sessions["owner"], "final-session")
                    self.assertIn("执行权限", bridge.reply.call_args.args[1])
                    self.assertEqual(runner.health.snapshot()["state"], "available")
                    self.assertIn("操作被拒绝", runner.health.snapshot()["detail"])

    def test_last_final_result_and_legacy_final_result_selection(self):
        for backend in BACKENDS:
            for shape in ("jsonl", "array"):
                for typed in (False, True):
                    with self.subTest(backend=backend, shape=shape, typed=typed):
                        final = self.payload("final text", False, session_id="chosen-session")
                        if not typed:
                            final.pop("type")
                        events = [self.payload("earlier failure", True), final,
                                  {"type": "assistant", "text": "tail assistant", "session_id": "wrong-tail"}]
                        if not typed:
                            events[0].pop("type")
                        raw = ("\n".join(json.dumps(event) for event in events) if shape == "jsonl"
                               else json.dumps(events))
                        runner = self.runner(backend, keep_context=True)
                        bridge, message = self.run_bridge(runner, raw)
                        self.assertEqual(message._receipt_outcome, "completed")
                        self.assertEqual(bridge.reply.call_args.args[1], "final text")
                        self.assertEqual(runner._sessions["owner"], "chosen-session")
                        self.assertEqual(runner.health.snapshot()["detail"], "")

    def test_mixed_final_formats_use_the_last_final_not_trailing_bare_text(self):
        for backend in BACKENDS:
            for shape in ("jsonl", "array"):
                for first_typed in (False, True):
                    with self.subTest(backend=backend, shape=shape, first_typed=first_typed):
                        first = self.payload("earlier success")
                        final = self.payload("final denied operation", True, session_id="chosen-session")
                        (final if first_typed else first).pop("type")
                        events = [first, final, {"text": "unrelated wrapper tail"}]
                        raw = ("\n".join(json.dumps(event) for event in events) if shape == "jsonl"
                               else json.dumps(events))
                        runner = self.runner(backend, keep_context=True)
                        bridge, message = self.run_bridge(runner, raw)
                        self.assertEqual(message._receipt_outcome, "failed")
                        self.assertEqual(runner._sessions["owner"], "chosen-session")
                        self.assertIn("执行权限", bridge.reply.call_args.args[1])

    def test_error_flag_and_nonzero_exit_never_publish_attachments(self):
        path = self.root / "synthetic-report.txt"
        path.write_text("synthetic content", encoding="utf-8")
        for backend in BACKENDS:
            for shape in SHAPES:
                for error_flag, code in ((True, 0), (False, 7)):
                    with self.subTest(backend=backend, shape=shape, is_error=error_flag, code=code):
                        payload = self.payload("Partial output\n[[发文件]]" + str(path), is_error=error_flag)
                        runner = self.runner(backend)
                        bridge, message = self.run_bridge(runner, self.output(payload, shape), code)
                        self.assertEqual(message._receipt_outcome, "failed")
                        self.assertTrue(bridge.reply.call_args.args[1].startswith("❌"))
                        bridge.client.send_file.assert_not_called()
                        self.assertNotIn("[[发文件]]", bridge.reply.call_args.args[1])

    def test_denied_results_never_publish_attachments(self):
        path = self.root / "synthetic-report.txt"
        path.write_text("synthetic content", encoding="utf-8")
        for backend in BACKENDS:
            for shape in SHAPES:
                with self.subTest(backend=backend, shape=shape):
                    payload = self.payload("Completed\n[[发文件]]" + str(path), denied=True)
                    bridge, message = self.run_bridge(self.runner(backend), self.output(payload, shape))
                    self.assertEqual(message._receipt_outcome, "failed")
                    bridge.client.send_file.assert_not_called()

    def test_health_uses_selected_error_result_for_every_shape(self):
        for backend in BACKENDS:
            for shape in SHAPES:
                with self.subTest(backend=backend, shape=shape):
                    payload = self.payload("API Error: 401", is_error=True, modelUsage={})
                    runner = self.runner(backend)
                    self.run_bridge(runner, self.output(payload, shape, {"type": "system"}))
                    self.assertEqual(runner.health.snapshot()["state"], "error")
                    self.assertEqual(runner.health.snapshot()["error_kind"], "auth")
                    self.assertEqual(runner._health_details, runner.last_result)

    def test_diagnostics_are_minimal_and_provider_text_is_redacted(self):
        for backend in BACKENDS:
            for shape in SHAPES:
                with self.subTest(backend=backend, shape=shape):
                    payload = self.payload("API Error: 401 " + KEY, is_error=True)
                    payload["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": KEY}}]
                    payload["modelUsage"] = {KEY: {"note": "private diagnostic", "outputTokens": 1}}
                    runner = self.runner(backend)
                    self.run_bridge(runner, self.output(payload, shape))
                    diagnostics = json.dumps(runner._health_details, ensure_ascii=False)
                    self.assertNotIn(KEY, diagnostics)
                    self.assertNotIn("tool_input", diagnostics)
                    self.assertNotIn("private diagnostic", diagnostics)
                    self.assertNotIn("API Error", diagnostics)
                    self.assertEqual(runner.last_result, runner._health_details)
                    if backend != "claude":
                        text, _ = runner._parse(self.output(payload, shape))
                        self.assertNotIn(KEY, text)

    def test_next_task_clears_permission_and_missing_process_clears_diagnostics(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                runner = self.runner(backend)
                self.run_bridge(runner, self.output(self.payload(denied=True), "jsonl"))
                bridge, message = self.run_bridge(runner, self.output(self.payload("next task"), "array"))
                self.assertEqual(message._receipt_outcome, "completed")
                self.assertEqual(bridge.reply.call_args.args[1], "next task")
                self.assertFalse(runner.last_result.get("permission_denials"))
                self.assertEqual(runner.health.snapshot()["detail"], "")
                with patch.object(runner, "_process_options", return_value={}), \
                     patch("claude_runner.subprocess.Popen", side_effect=FileNotFoundError):
                    result = runner.run("owner", "missing process")
                self.assertTrue(result.startswith("❌"))
                self.assertEqual(runner.last_result, {})
                self.assertIsNone(runner._health_details)
                self.assertEqual(runner.health.snapshot()["state"], "unconfirmed")

    def test_no_final_result_does_not_claim_completion(self):
        values = ("", "{}", "[]", "null", "wrapper warning only",
                  '{"type":"system","session_id":"not-final"}',
                  '[{"type":"assistant","text":"not final"}]')
        for backend in BACKENDS:
            for raw in values:
                with self.subTest(backend=backend, raw=raw):
                    runner = self.runner(backend)
                    bridge, message = self.run_bridge(runner, raw)
                    self.assertEqual(message._receipt_outcome, "failed")
                    self.assertTrue(bridge.reply.call_args.args[1].startswith("❌"))
                    self.assertEqual(runner.health.snapshot()["state"], "unconfirmed")
                    bridge.client.send_file.assert_not_called()

    def test_static_parse_compatibility_and_legacy_text(self):
        self.assertEqual(ClaudeRunner._parse('{"result":"legacy","session_id":"s"}'), ("legacy", "s"))
        self.assertEqual(ClaudeRunner._parse('{"text":"legacy text"}'), ("legacy text", None))
        text, session = ClaudeRunner._parse(json.dumps(self.payload(denied=True)))
        self.assertTrue(text.startswith("❌"))
        self.assertEqual(session, "final-session")

    def test_nonzero_empty_stdout_retains_stderr_diagnostic(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                runner = self.runner(backend)
                process = Mock(returncode=1)
                process.communicate.return_value = (b"", b"API Error: 401")
                with patch.object(runner, "_process_options", return_value={}), \
                     patch("claude_runner.subprocess.Popen", return_value=process):
                    text = runner.run("owner", "synthetic request")
                self.assertIn("API Error: 401", text)
                self.assertEqual(runner.health.snapshot()["error_kind"], "auth")


if __name__ == "__main__":
    unittest.main()
