"""Opt-in private Claude-compatible API profile; legacy Claude remains untouched."""
import re
from backend_profile import validate_url
from deepseek_runner import DeepSeekRunner

class ClaudeAPIRunner(DeepSeekRunner):
    backend_name='Claude'
    allowed_models=None
    key_environment=None
    require_sk_prefix=False

    def __init__(self,*,base_url,model,**kwargs):
        try:
            self.api_base_url=validate_url(base_url)
            if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_./:\[\]-]{1,160}',model):
                raise ValueError('model')
        except ValueError:
            raise SystemExit('Claude 自定义 API 地址或模型配置无效') from None
        try:super().__init__(model=model,**kwargs)
        except SystemExit as e:raise SystemExit(str(e).replace('DeepSeek','Claude')) from None
        self.supports_vision=True
