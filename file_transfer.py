"""Cancellable outgoing file transport isolated from the bridge process.
Credentials travel over stdin, never argv or disk. Child must receive explicit
permission immediately before posting the attachment message.
"""
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from task_control import TransferCancelled


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill();proc.wait(timeout=5)


def send_cancellable(client,to_user_id,path,file_name,context_token,control):
    from ilink_client import ILinkError,ILinkTimeout
    control.check()
    request={'base_url':client.base_url,'token':client.token,'account_id':client.account_id,
        'state_dir':client.state_dir,'send_style':client.send_style,'to_user_id':to_user_id,
        'path':os.fspath(path),'file_name':file_name,'context_token':context_token}
    proc=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve())],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    messages=queue.Queue()
    def read():
        try:
            for line in proc.stdout:
                try:messages.put(json.loads(line))
                except ValueError:messages.put({'event':'invalid'})
        finally:messages.put({'event':'eof'})
    reader=threading.Thread(target=read,daemon=True);reader.start()
    publishing=False;deadline=time.monotonic()+240
    try:
        proc.stdin.write(json.dumps(request,ensure_ascii=False)+'\n');proc.stdin.flush()
        request=None
        while True:
            if control.requested.is_set():
                _stop(proc);reader.join(timeout=1)
                # Any response already returned by the server remains evidence.
                accepted=None
                while True:
                    try:event=messages.get_nowait()
                    except queue.Empty:break
                    if event.get('event')=='accepted':accepted=event
                if accepted:return accepted['name'],accepted['size']
                raise TransferCancelled(publishing)
            if time.monotonic()>deadline:
                _stop(proc);raise ILinkTimeout('文件传输超时，结果未确认')
            try:event=messages.get(timeout=.05)
            except queue.Empty:continue
            kind=event.get('event')
            if kind=='phase':
                control.transfer_phase=event.get('phase','')
            elif kind=='publish_ready':
                control.check()
                publishing=True
                proc.stdin.write('publish\n');proc.stdin.flush()
            elif kind=='accepted':return event['name'],event['size']
            elif kind=='error':
                if event.get('kind')=='file':raise OSError(event['message'])
                if event.get('kind')=='timeout':raise ILinkTimeout(event['message'])
                raise ILinkError(event['message'])
            elif kind in ('eof','invalid'):
                raise ILinkError('文件传输进程异常退出，结果未确认')
    finally:
        _stop(proc);reader.join(timeout=1)
        for stream in (proc.stdin,proc.stdout):
            try:stream.close()
            except (OSError,ValueError):pass


def _emit(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)


def main():
    logging.disable(logging.CRITICAL)
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    from ilink_client import ILinkClient,ILinkTimeout
    request=json.loads(sys.stdin.readline())
    try:
        client=ILinkClient(request['base_url'],request['token'],request['account_id'],state_dir=request['state_dir'])
        client.send_style=request.get('send_style')
        upload=client.upload_file
        def encrypt(*args,**kwargs):
            _emit({'event':'phase','phase':'encrypting'})
            return upload(*args,**kwargs)
        client.upload_file=encrypt
        original=client.send_item
        def publish(*args,**kwargs):
            _emit({'event':'publish_ready'})
            if sys.stdin.readline().strip()!='publish':raise RuntimeError('publication not authorized')
            return original(*args,**kwargs)
        client.send_item=publish
        name,size=client.send_file(request['to_user_id'],request['path'],
            file_name=request.get('file_name'),context_token=request.get('context_token'))
        _emit({'event':'accepted','name':name,'size':size})
    except Exception as exc:
        message=str(exc)
        for value in (request.get('token'),request.get('account_id'),request.get('context_token')):
            if value:message=message.replace(value,'***')
        message=re.sub(r'https?://\S+','[网络地址]',message)[:800]
        _emit({'event':'error','kind':'timeout' if isinstance(exc,ILinkTimeout) else ('file' if isinstance(exc,OSError) else 'transport'),'message':message})

if __name__=='__main__':main()
