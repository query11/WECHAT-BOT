"""Observations of actual model calls, separate from task and WeChat status."""
import json
import re
import threading
import time

class ModelHealth:
    def __init__(self):
        self.lock=threading.Lock();self.state='unknown';self.last_checked=0;self.last_success=0;self.error_kind='';self.note='';self._previous='unknown'
    def begin(self):
        with self.lock:self._previous=self.state;self.state='running'
    def cancelled(self):
        with self.lock:self.state=self._previous
    def finish(self,text='',details=None,returncode=None,local_error=False):
        details=details if isinstance(details,dict) else {}
        failed=bool(details.get('is_error')) or returncode not in (None,0)
        kind=''
        if failed:
            t=str(text or '').lower()
            if re.search(r'(?:http|api error|status)\s*[:= ]*\s*(?:401|403)\b|authentication|invalid.api.key',t):kind='auth'
            elif re.search(r'(?:http|api error|status)\s*[:= ]*\s*402\b|insufficient.*(?:balance|quota)|余额不足',t):kind='quota'
            elif re.search(r'(?:http|api error|status)\s*[:= ]*\s*429\b|rate.limit',t):kind='rate_limit'
            elif re.search(r'timed? out|timeout|超时|connection error|connection refused|econn|network error',t):kind='network'
            elif re.search(r'(?:http|api error|status)\s*[:= ]*\s*5\d\d\b',t):kind='service'
        answered=bool(details.get('modelUsage') or details.get('usage'))
        now=time.time()
        with self.lock:
            self.last_checked=now
            self.note="模型已响应，部分本机操作被拒绝" if answered and details.get("permission_denials") else ""
            if kind:self.state='error';self.error_kind=kind
            elif answered:self.state='available';self.last_success=now;self.error_kind=''
            else:self.state='unconfirmed';self.error_kind='local' if local_error or failed else ''
    def snapshot(self):
        with self.lock:
            labels={'unknown':'尚无模型调用记录','running':'模型任务执行中','available':'上次模型调用正常','error':'上次模型调用异常','unconfirmed':'接口状态未确认'}
            reasons={'auth':'认证或访问权限异常','quota':'余额或额度不足','rate_limit':'请求受到限流','network':'连接失败或超时','service':'服务端异常','local':'本机执行未完成，不能据此判断接口异常','':''}
            return {'state':self.state,'label':labels[self.state],'last_checked':self.last_checked,'last_success':self.last_success,'error_kind':self.error_kind,'detail':reasons[self.error_kind] or self.note}
