"""Short user-facing failure replies; detailed diagnostics belong in masked logs."""
import json
import re


_MAX_REPLY = 300
_PREFIX = re.compile(r'^[\s#>*_`~\-.、:：❌✖✕⚠️✅⏳🔴]+')
_PERMISSION = re.compile(
    r'permission_denials|approval[ _-]+required|requires?[ _-]+approval|'
    r'有操作未获执行许可|未获执行许可|缺少(?:所需)?权限|需要(?:执行)?许可|'
    r'需要(?:用户)?(?:授权|审批)|未(?:授权|获授权)|permission denied', re.I)


def _sanitize(value):
    text = str(value or '')[:24000]
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    # Remove the complete marker line, including the private local filename.
    text = re.sub(r'[^\r\n]*\[\[\s*发文件[^\]]*\]\][^\r\n]*', '', text)
    text = re.sub(r'^.*\[\[\s*发文件.*$', '', text, flags=re.M)
    text = re.sub(r'\bsk-[A-Za-z0-9_\-=]+', '[已隐藏]', text, flags=re.I)
    text = re.sub(r'\bBearer\s+[^\s,;"\'<>]+', 'Bearer [已隐藏]', text, flags=re.I)
    text = re.sub(r'(Authorization["\']?\s*[:=]\s*["\']?)(?!Bearer\b)[^\r\n,;"\']+',
                  r'\1[已隐藏]', text, flags=re.I)
    # A reply has exactly one failure marker, added by compact_failure itself.
    return text.replace('❌', '').strip()


def _structured_reason(value, depth=0):
    if depth > 4:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ('cause', 'error', 'message', 'detail', 'reason', 'description'):
            if key in value:
                result = _structured_reason(value[key], depth + 1)
                if result:
                    return result
    return ''


def _reason(text):
    stripped = text.strip().strip('`').strip()
    if stripped.startswith('json\n'):
        stripped = stripped[5:]
    try:
        structured = _structured_reason(json.loads(stripped))
    except (ValueError, TypeError):
        structured = ''
    if structured:
        text = structured
    elif text.lstrip().startswith(('{', '[')):
        # Partially logged JSON should not turn into a raw dump in WeChat.
        match = re.search(r'"(?:message|detail|reason|cause)"\s*:\s*("(?:\\.|[^"\\])*")', text)
        if match:
            try:
                text = json.loads(match.group(1))
            except ValueError:
                return ''
        else:
            return ''
    candidates = []
    for raw in text.splitlines():
        line = _PREFIX.sub('', raw).strip().replace('**', '').replace('`', '')
        if not line or re.match(r'(?:Traceback|File\s+["\']|at\s+\S|\^+$|[{}\[\]]|Authorization\b)', line, re.I):
            continue
        explicit = bool(re.match(r'(?:\w*(?:Error|Exception)|错误|操作失败|任务失败|请求失败|执行失败|失败|原因)\s*[:：]', line, re.I))
        line = re.sub(r'^(?:(?:error|错误|操作失败|任务失败|请求失败|执行失败|失败|原因)\s*[:：]\s*)+', '', line, flags=re.I)
        if not line or re.fullmatch(r'(?:任务|操作|请求|执行)?(?:未完成|失败|发生错误)[。.!！]?', line):
            continue
        candidates.append((line, explicit))
    if not candidates:
        return ''
    # Preserve the first concrete diagnostic instead of a traceback or summary.
    diagnostic = next((line for line, explicit in candidates if explicit or re.search(
        r'error|exception|failed|invalid|not found|denied|refused|unavailable|'
        r'错误|失败|异常|无法|不存在|不可用|拒绝|原因|已被占用|锁定', line, re.I)), candidates[0][0])
    diagnostic = re.split(r'\s+(?:Traceback|Stack trace)\b', diagnostic, maxsplit=1, flags=re.I)[0]
    return re.sub(r'\s+', ' ', diagnostic).strip(' 。.!！')[:180]


def compact_failure(text, backend_name='模型'):
    """Return a sanitized Chinese failure reply, at most 300 characters.

    Call only for results already classified as failures. This function does not
    infer whether individual side effects succeeded and never claims rollback.
    """
    clean = _sanitize(text)
    backend = _PREFIX.sub('', _sanitize(backend_name)).splitlines()
    backend = (backend[0] if backend else '模型')[:20]
    # Empty denial lists are normal metadata, not evidence of denied actions.
    permission_text = re.sub(r'["\']?permission_denials["\']?\s*[:=]\s*\[\s*\]', '', clean, flags=re.I)
    if _PERMISSION.search(permission_text):
        reply = ('❌ 任务未全部完成：有操作缺少执行权限。\n'
                 '已完成的操作不会自动撤销。可用 /apps 查看可直接打开的应用；'
                 '其他操作请在电脑端完成授权后重试未完成部分。')
    elif re.search(r'(?<!\d)(?:401|403)(?!\d)|unauthori[sz]ed|forbidden|'
                   r'authentication|invalid[_ -]?api[_ -]?key|登录(?:已)?过期|认证失败|鉴权失败', clean, re.I):
        reply = ('❌ 任务未完成：%s认证或访问权限异常。\n'
                 '请在电脑端检查登录状态、API 凭据及访问权限，再重试未完成部分。') % backend
    elif re.search(r'(?<!\d)402(?!\d)|insufficient[_ -]?(?:quota|balance|credits)|'
                   r'quota[_ -]?exceeded|余额|额度不足|配额(?:不足|已用完)|credit balance', clean, re.I):
        reply = ('❌ 任务未完成：%s可用额度或余额不足。\n'
                 '请在电脑端检查账户额度和计费状态，恢复后重试未完成部分。') % backend
    elif re.search(r'(?<!\d)429(?!\d)|rate[_ -]?limit|too many requests|请求过于频繁|限流', clean, re.I):
        reply = ('❌ 任务未完成：%s请求过于频繁。\n'
                 '请稍后重试未完成部分；若持续出现，请检查服务端限流设置。') % backend
    elif re.search(r'timed?\s*out|timeout|超时', clean, re.I):
        reply = ('❌ 任务未完成：等待%s响应超时。\n'
                 '请先确认前一步是否已完成，再重试未完成部分，避免重复操作。') % backend
    else:
        reason = _reason(clean) or '发生未识别错误'
        reply = ('❌ 任务未完成：%s。\n'
                 '请先检查实际结果，再重试未完成部分；如仍失败，可在电脑端查看日志。') % reason
    return reply[:_MAX_REPLY]
