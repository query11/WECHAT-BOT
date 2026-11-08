# -*- coding: utf-8 -*-
"""DeepSeek official Anthropic API through the existing Claude Code tool harness.

Provider settings are child-local. Claude's original settings and sessions stay
untouched. This is DeepSeek inference, not a fallback to Anthropic inference.
"""
import os
import re
from pathlib import Path
from claude_runner import ClaudeRunner
from key_vault import read_secret
from project_paths import ROOT

OFFICIAL_BASE_URL = "https://api.deepseek.com/anthropic"
MODELS = ("deepseek-flash", "deepseek-v4-pro")


class DeepSeekRunner(ClaudeRunner):
    backend_name = "DeepSeek"
    allowed_models = MODELS
    api_base_url = OFFICIAL_BASE_URL
    key_environment = "DEEPSEEK_API_KEY"
    require_sk_prefix = True

    def __init__(self, *, key_file=None, api_key=None, model="deepseek-flash",
                 config_dir=None, **kwargs):
        if self.allowed_models is not None and model not in self.allowed_models:
            raise SystemExit("DeepSeek model 必须是 deepseek-flash 或 deepseek-v4-pro")
        if key_file and Path(key_file).suffix.lower() != ".key":
            raise SystemExit("DeepSeek 密钥文件必须使用 .key 扩展名，防止被当附件发送")
        try:
            key = api_key or (os.environ.get(self.key_environment) if self.key_environment else None) or (
                read_secret(key_file) if key_file else "")
        except (OSError, UnicodeError):
            raise SystemExit("无法读取 DeepSeek 密钥文件，请检查本机 deepseek.key_file")
        if not key or not isinstance(key, str) or (self.require_sk_prefix and not key.startswith("sk-")) or any(c.isspace() for c in key):
            raise SystemExit("未配置有效的 DeepSeek API 密钥")
        self._api_key = key
        self.model = model
        self.supports_vision = model == "deepseek-flash"
        self.config_dir = os.path.abspath(config_dir or os.path.join(
            str(ROOT), ".runtime", "deepseek-cli"))
        # Caller-provided provider/settings switches cannot redirect credentials.
        args = list(kwargs.get("extra_args") or [])
        forbidden = ("--model", "--fallback-model", "--settings", "--setting-sources",
                     "--mcp-config", "--plugin-dir", "--plugin-url", "--agent", "--agents")
        if any(str(a).split("=", 1)[0] in forbidden for a in args):
            raise SystemExit("DeepSeek 执行参数不能覆盖模型、设置或加载外部代理；请使用 deepseek.model")
        kwargs["extra_args"] = args + ["--setting-sources", "", "--strict-mcp-config",
            "--model", model, "--effort", "high"]
        kwargs.setdefault("session_file", os.path.join(str(ROOT),
                                                       ".runtime", "deepseek-sessions.json"))
        super().__init__(**kwargs)

    def _process_options(self):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")) or k in (
                "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
                "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CONFIG_DIR", "CLAUDECODE"):
                env.pop(k, None)
        env.update({
            "ANTHROPIC_BASE_URL": self.api_base_url,
            "ANTHROPIC_AUTH_TOKEN": self._api_key,
            "ANTHROPIC_API_KEY": self._api_key,
            "ANTHROPIC_MODEL": self.model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.model,
            "CLAUDE_CODE_SUBAGENT_MODEL": self.model,
            "CLAUDE_CONFIG_DIR": self.config_dir,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": str(self.timeout_sec * 1000),
        })
        return {"env": env}

    def redact(self, text):
        from runner_result import RunnerResult
        clean = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "***", str(text).replace(self._api_key, "***"))
        return RunnerResult(clean, text.state) if isinstance(text, RunnerResult) else clean

    def _parse(self, stdout):
        return self._read_result(stdout)

    def _read_result(self, stdout):
        text, sid = super()._read_result(stdout)
        return self.redact(text), sid

    def run(self, user_id, prompt):
        return self.redact(super().run(user_id, prompt))
