"""Interruptible incoming download/decryption. Parent alone commits final files."""
import hashlib
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
from file_transfer import _stop


def receive_cancellable(info,tmp,max_bytes,control):
    from ilink_client import ILinkError
    control.check()
    process=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve())],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    messages=queue.Queue()
    def read():
        try:
            line=process.stdout.readline()
            messages.put(json.loads(line) if line else {})
        except (OSError,ValueError):messages.put({})
    reader=threading.Thread(target=read,daemon=True);reader.start()
    success=False
    try:
        process.stdin.write(json.dumps({'info':info,'tmp':tmp,'max_bytes':max_bytes},ensure_ascii=False)+'\n');process.stdin.close()
        deadline=time.monotonic()+240
        while True:
            control.check()
            if time.monotonic()>deadline:raise ILinkError('附件下载超时')
            try:result=messages.get(timeout=.05)
            except queue.Empty:continue
            control.check()
            if not result.get('ok'):raise ILinkError(result.get('message') or '附件下载进程中断')
            success=True;return int(result['bytes']),str(result['md5'])
    finally:
        _stop(process);reader.join(timeout=1);process.stdout.close()
        if not success:
            try:os.unlink(tmp)
            except FileNotFoundError:pass
            except OSError:
                control.cleanup_paths.add(tmp);control.cleanup_failed=True


class TooLarge(Exception):pass

def main():
    logging.disable(logging.CRITICAL)
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    import aes128,ilink_client
    request=json.loads(sys.stdin.readline());info=request['info'];size=0;digest=hashlib.md5()
    try:
        key=ilink_client.media_key_bytes(info)
        with open(request['tmp'],'xb') as out:
            for plain in aes128.decrypt_stream(key,ilink_client.stream_download(info['url'])):
                size+=len(plain)
                if request['max_bytes'] and size>request['max_bytes']:raise TooLarge('超过上限')
                out.write(plain);digest.update(plain)
        result={'ok':True,'bytes':size,'md5':digest.hexdigest()}
    except TooLarge:
        result={'ok':False,'message':'附件解密后的大小超过上限'}
    except Exception as exc:
        code=re.search(r'HTTP\s+(\d{3})',str(exc))
        message='附件下载失败（HTTP %s）'%code.group(1) if code else '附件下载或解密失败，请检查网络及文件大小'
        result={'ok':False,'message':message}
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
