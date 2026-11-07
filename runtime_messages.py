"""User-facing network diagnostics; raw exceptions remain in bridge.log."""
def explain_error(error):
    text = str(error).lower()
    if any(x in text for x in ('http 401', 'http 403', 'token expired', 'errcode=-14')):
        return '微信登录可能已失效', '请重新扫码登录，再启动服务。'
    if 'certificate_verify_failed' in text:
        return '安全证书校验失败', '请检查系统时间和网络代理；不要关闭证书校验。'
    if any(x in text for x in ('ssl', 'eof', 'tls')):
        return '微信加密连接中断', '正在自动重试；若持续失败，请检查网络或代理。'
    if any(x in text for x in ('getaddrinfo', 'name resolution', 'nodename')):
        return '无法解析微信服务器地址', '请检查网络连接和 DNS 设置。'
    if any(x in text for x in ('timeout', 'timed out', '超时', '秒内没返回')):
        return '微信服务器暂未响应', '正在自动重试，请稍候。'
    if any(x in text for x in ('http 429', 'http 50', 'http 52')):
        return '微信服务暂时繁忙', '正在自动重试，暂时不用重复发送任务。'
    return '暂时无法连接微信', '正在自动重试；详细原因可在“打开日志”中查看。'


def task_event(message):
    if message.startswith('完成 user='):
        return '一项任务已处理结束，可在任务记录中查看。'
    if message.startswith('任务被叫停'):
        return '当前任务已停止，已发生的文件修改不会自动撤销。'
    if '记忆' in message:
        return '固定记忆读取失败，请在“固定记忆”中检查内容。'
    return '一项操作遇到问题，请查看任务记录或详细日志。'
