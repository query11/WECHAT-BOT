"""Anthropic-compatible API runners; the user's own Claude settings remain untouched.

ClaudeAPIRunner is the opt-in private Claude-compatible profile (config ``claude_api``).
ProfileRunner runs a saved service from ``api_profiles`` (Kimi, GLM, custom, ...).
Both reuse DeepSeekRunner's child-local environment, key handling and redaction.
"""
import re
from api_profiles import validate_url
from deepseek_runner import DeepSeekRunner

class ClaudeAPIRunner(DeepSeekRunner):
    backend_name='Claude'
    allowed_models=None
    key_environment=None
    require_sk_prefix=False

    def __init__(self,*,base_url,model,**kwargs):
        label=self.backend_name
        try:
            self.api_base_url=validate_url(base_url)
            if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_./:\[\]-]{1,160}',model):
                raise ValueError('model')
        except ValueError:
            raise SystemExit(label+' 接口地址或模型配置无效') from None
        try:super().__init__(model=model,**kwargs)
        except SystemExit as e:raise SystemExit(str(e).replace('DeepSeek',label)) from None
        self.supports_vision=True


class ProfileRunner(ClaudeAPIRunner):
    """A saved Anthropic-compatible service; messages and status use its display name."""

    def __init__(self,*,display_name,vision=False,**kwargs):
        self.backend_name=display_name
        super().__init__(**kwargs)
        # Screenshot analysis is only offered when the user marked the model as image-capable.
        self.supports_vision=bool(vision)
