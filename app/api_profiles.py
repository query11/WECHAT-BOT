"""Saved model services beyond the built-in Claude and DeepSeek backends.

Every service here speaks the Anthropic Messages API, so Claude Code runs it with the
same child-local environment as DeepSeek (see claude_api_runner.ProfileRunner): the
user's own Claude settings and sessions are never touched.

A profile is one saved service, stored in ``config.json`` under ``api_profiles``::

    {"id": "kimi", "name": "Kimi", "preset": "kimi", "base_url": "https://...",
     "model": "...", "key_file": ".runtime/api-kimi-<uuid>.key", "vision": false}

``preset`` is a key of PRESETS or ``custom``. Presets only pre-fill the address and
suggest model names; both stay editable because providers rename models often.
``backend`` in config.json selects ``claude``, ``deepseek`` or a profile id.
"""
import re
import urllib.parse
import uuid

RESERVED = ('claude', 'deepseek')
BUILTIN_NAMES = {'claude': 'Claude', 'deepseek': 'DeepSeek'}
MAX_PROFILES = 12
_ID = re.compile(r'[a-z][a-z0-9-]{1,31}')
_MODEL = re.compile(r'[A-Za-z0-9_./:\[\]-]{1,160}')

# Mainland-China Anthropic-compatible endpoints as documented by each provider in 2026-11.
# International accounts use other hosts (for example api.moonshot.ai, api.minimax.io);
# the editor keeps the address editable for that reason.
PRESETS = (
    {'id': 'kimi', 'name': 'Kimi', 'vendor': '月之暗面',
     'base_url': 'https://api.moonshot.cn/anthropic', 'models': ('kimi-k3', 'kimi-k2.7-code', 'kimi-k2.6')},
    {'id': 'glm', 'name': '智谱 GLM', 'vendor': '智谱开放平台',
     'base_url': 'https://open.bigmodel.cn/api/anthropic', 'models': ('glm-5.2', 'glm-4.7')},
    {'id': 'qwen', 'name': '通义千问', 'vendor': '阿里云百炼（北京）',
     'base_url': 'https://dashscope.aliyuncs.com/apps/anthropic', 'models': ('qwen3.7-plus', 'qwen3.7-max', 'qwen3.6-flash')},
    {'id': 'minimax', 'name': 'MiniMax', 'vendor': 'MiniMax 开放平台',
     'base_url': 'https://api.minimaxi.com/anthropic', 'models': ('MiniMax-M2.5', 'MiniMax-M2.1')},
)
PRESET_IDS = tuple(p['id'] for p in PRESETS)


class ProfileError(ValueError):
    """A validation message that is safe to show in the editor."""


def preset(preset_id):
    return next((p for p in PRESETS if p['id'] == preset_id), None)


def validate_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ProfileError('接口地址必须是完整 HTTPS 地址')
    p = urllib.parse.urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ProfileError('接口地址必须使用 HTTPS，且不能包含账号、密码、查询参数')
    return value.rstrip('/')


def clean_model(value):
    model = str(value or '').strip()
    if not _MODEL.fullmatch(model):
        raise ProfileError('请填写有效的模型名称')
    return model


def clean_name(value):
    name = ' '.join(str(value or '').split())
    if not name or len(name) > 24 or any(ord(c) < 32 for c in name):
        raise ProfileError('名称需为 1 到 24 个字符')
    return name


def _valid(entry):
    """True for a stored profile the bridge may run; malformed entries are ignored."""
    if not isinstance(entry, dict):
        return False
    try:
        if not _ID.fullmatch(str(entry.get('id', ''))) or entry['id'] in RESERVED:
            return False
        clean_name(entry.get('name'))
        validate_url(entry.get('base_url'))
        clean_model(entry.get('model'))
    except (ProfileError, KeyError):
        return False
    key_file = entry.get('key_file', '')
    return isinstance(key_file, str) and (not key_file or key_file.lower().endswith('.key'))


def profiles(cfg):
    """Valid saved profiles in stored order, as copies; duplicates of an id keep the first."""
    seen, result = set(), []
    for entry in (cfg or {}).get('api_profiles') or []:
        if _valid(entry) and entry['id'] not in seen:
            seen.add(entry['id'])
            result.append(dict(entry))
    return result


def find(cfg, backend_id):
    return next((p for p in profiles(cfg) if p['id'] == backend_id), None)


def backend_ids(cfg):
    return list(RESERVED) + [p['id'] for p in profiles(cfg)]


def backend_list(cfg):
    """Backends the desktop can offer, in display order: built-ins first, then profiles."""
    items = [{'id': 'claude', 'name': 'Claude', 'model': ''},
             {'id': 'deepseek', 'name': 'DeepSeek',
              'model': str(((cfg or {}).get('deepseek') or {}).get('model', 'deepseek-flash'))}]
    items += [{'id': p['id'], 'name': p['name'], 'model': p['model']} for p in profiles(cfg)]
    return items


def new_id(cfg, preset_id):
    """A preset's first profile reuses the preset id; later or custom ones get a suffix."""
    taken = set(backend_ids(cfg)) | {str(e.get('id')) for e in (cfg or {}).get('api_profiles') or [] if isinstance(e, dict)}
    if preset_id in PRESET_IDS and preset_id not in taken:
        return preset_id
    base = preset_id if preset_id in PRESET_IDS else 'custom'
    while True:
        candidate = '%s-%s' % (base, uuid.uuid4().hex[:6])
        if candidate not in taken:
            return candidate
