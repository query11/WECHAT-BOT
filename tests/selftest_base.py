# -*- coding: utf-8 -*-
"""自测的共用底座：假 iLink 服务端、假 claude、造一个桥、以及记分的 check。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。

所有 selftest_*.py 都从这里 import，而且必须是从这里：PASS/FAIL 是两个列表，
check 往里 append，最后 main() 数的就是它们；FakeILink 身上挂着十几个可变的类
属性（sent、inbox、uploads、upload_style……），drain 和 texts 也是隔着这个类
读写的。大家用同一个对象才对得上账，谁自己复制一份，账就散了。

make_launcher 那一套本来在「开关应用」那一段里，但音量链路和 test_meeting_chain
也要用（后者要证明「打开腾讯会议 413231713」不会被开应用那条通道截走），所以搬
到这儿来。让 selftest_media 去 import selftest_apps 也能跑，但测试文件之间互相
import 是乱的开始。
"""

import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

import app_launcher
import wechat_bridge

# 回话里有 emoji 状态记号（✅/⏳/❌…），GBK 控制台直接打印会抛 UnicodeEncodeError。
# 自测的输出是给人看的，遇到打不出来的字符替换掉就行——不能让一句断言把整轮搞崩。
wechat_bridge.make_console_safe()


PORT = 18099
BASE = "http://127.0.0.1:%d" % PORT
USER = "u-me"
OTHER = "u-stranger"
ACCOUNT = "bot-1"
QR_KEY = "6db97abe7f57c103afee598d51e988ec"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                       ("  -> " + str(detail)) if detail and not cond else ""))


def make_fake_claude(tmpdir):
    """假 claude：回显 prompt，看到 SLEEP 就慢一点，看到 LONG 就吐一大段。

    直接返回 [python, 脚本] 这个数组交给 claude_bin，不再包一层 .cmd/.sh ——
    Windows 上批处理包装器碰上应用执行别名会直接「系统找不到指定的路径」。
    stdout 用 buffer 写死 UTF-8，否则 Windows 上管道走本地编码会乱码。
    """
    path = os.path.join(tmpdir, "fake_claude.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "# -*- coding: utf-8 -*-\n"
            "import json, sys, time\n"
            "p = sys.argv[-1]\n"
            "if 'SLEEP' in p: time.sleep(2)\n"
            "out = '长'*4000 if 'LONG' in p else '已处理: ' + p.replace('\\n',' ')\n"
            "if 'WANTFILE:' in p:\n"
            "    out = '给你\\n[[发文件]]' + p.split('WANTFILE:',1)[1].strip()\n"
            "payload = {'type':'result','result':out,'session_id':'sess-abc','is_error':False}\n"
            "sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))\n")
    return [sys.executable, path]


class FakeILink(BaseHTTPRequestHandler):
    inbox = []          # 待发给桥的消息
    sent = []           # 桥发出去的 sendmessage（已经拆掉外层 msg）
    wrapped = []        # 每次 sendmessage 是不是包了一层 msg
    typing = []         # sendtyping
    configs = []        # getconfig 的入参
    headers_seen = []
    bufs_seen = []
    base_infos = []
    qr_polls = [0]
    qr_status_params = []
    msg_key = "message_list"
    reject_account_id = False
    # 发文件用的假 CDN
    upload_asks = []            # getuploadurl 的入参
    uploads = []                # CDN 真收到的东西
    upload_style = "full"       # full=直接给完整 URL / param=只给 param+cdn / weird=键名不认识
    upload_via = "json"         # download_param 放 JSON 里还是放响应头里

    # 收附件用的假 CDN：GET 那个 full_url 就吐密文（用例自己塞 download_body）
    downloads = []              # 收到过哪些下载请求（整条 path+query）
    download_body = b""         # 要吐出去的密文
    download_code = 200         # 想测「下载失败」时把它改成非 200
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/upload"):
            # CDN 那一跳收的是裸密文，不是 JSON——所以要在 json.loads 之前分流
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            FakeILink.uploads.append({
                "filekey": (qs.get("filekey") or [""])[0],
                "param": (qs.get("encrypted_query_param") or [""])[0],
                "ctype": (self.headers.get("Content-Type") or ""),
                "data": raw})
            if FakeILink.upload_via == "header":
                body = b"ok"
                self.send_response(200)
                self.send_header("x-encrypted-param", "dl-param-1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif FakeILink.upload_via == "none":
                # 收了文件却什么凭据都不给：要能报出"看响应头和正文"的可诊断错误
                self._json({"errcode": 0, "ok": 1})
            else:
                self._json({"errcode": 0, "download_param": "dl-param-1"})
            return
        payload = json.loads(raw.decode("utf-8") or "{}")
        FakeILink.headers_seen.append({k.lower(): v for k, v in self.headers.items()})
        FakeILink.base_infos.append(payload.get("base_info"))
        if path.endswith("/getupdates"):
            FakeILink.bufs_seen.append(payload.get("get_updates_buf"))
            msgs, FakeILink.inbox = FakeILink.inbox, []
            self._json({"errcode": 0, FakeILink.msg_key: msgs,
                        "get_updates_buf": "cursor-%d" % len(FakeILink.bufs_seen)})
        elif path.endswith("/sendmessage"):
            # 真机认的是外面包一层 msg 的写法，所以这里照样拆一层再看内容
            inner = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
            if FakeILink.reject_account_id and "account_id" in inner:
                # 真机第一次就是这么被拒的：errcode=-2 invalid arguments
                return self._json({"errcode": -2, "errmsg": "invalid arguments"})
            FakeILink.sent.append(inner)
            FakeILink.wrapped.append(inner is not payload)
            self._json({"errcode": 0})
        elif path.endswith("/getuploadurl"):
            FakeILink.upload_asks.append(payload)
            if FakeILink.upload_style == "param":
                # 只给 param + cdn_base_url，让客户端自己拼 URL
                self._json({"errcode": 0, "upload_param": "up-param-1",
                            "cdn_base_url": BASE})
            elif FakeILink.upload_style == "weird":
                # 键名一个都不在别名列表里，考验「按形状找 URL」那条兜底
                self._json({"errcode": 0, "cdn": {"the_addr_to_post_to":
                                                  BASE + "/upload?filekey=x"}})
            elif FakeILink.upload_style == "empty":
                self._json({"errcode": 0, "expire_time": 600})
            else:
                self._json({"errcode": 0, "upload_full_url":
                            BASE + "/upload?filekey=" + str(payload.get("filekey") or "")})
        elif path.endswith("/getconfig"):
            FakeILink.configs.append(payload)
            self._json({"errcode": 0, "typing_ticket": "tk-1"})
        elif path.endswith("/sendtyping"):
            FakeILink.typing.append(payload)
            self._json({"errcode": 0})
        else:
            self._json({"errcode": 404, "errmsg": "unknown path " + path}, 404)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path.endswith("/get_bot_qrcode"):
            # 抄真机 2026-08-29 实测的形状：qrcode 是 key，图在 qrcode_img_content，用 ret 报错
            self._json({"ret": 0, "qrcode": QR_KEY,
                        "qrcode_img_content":
                            "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=%s&bot_type=3" % QR_KEY})
        elif path.endswith("/get_qrcode_status"):
            FakeILink.qr_status_params.append(qs)
            if (qs.get("qrcode") or [""])[0] != QR_KEY:
                return self._json({"ret": 0})        # key 没传对就什么状态都没有
            FakeILink.qr_polls[0] += 1
            n = FakeILink.qr_polls[0]
            if n == 1:
                time.sleep(0.6)                      # 真机行为：key 对了就挂住等扫码
                return
            if n == 2:
                self._json({"status": "wait"})
            elif n == 3:
                self._json({"status": "scaned_but_redirect", "redirect_host": BASE})
            else:
                self._json({"status": "confirmed", "bot_token": "tok-abc",
                            "ilink_bot_id": ACCOUNT, "baseurl": BASE})
        elif path.endswith("/download"):
            # 收附件那一跳：伪 CDN 直接把用例塞进来的密文吐出去
            FakeILink.downloads.append(self.path)
            if FakeILink.download_code != 200:
                body = b"nope"
                self.send_response(FakeILink.download_code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = FakeILink.download_body
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"errcode": 404}, 404)


def wecom_msg(text, mid, user=USER, items=None, **extra):
    d = {"message_id": mid, "from_user_id": user, "context_token": "ctx-1",
         "message_type": 1,
         "item_list": items if items is not None
         else [{"type": 1, "text_item": {"text": text}}]}
    d.update(extra)
    return d


def texts():
    out = []
    for p in FakeILink.sent:
        for it in p.get("item_list") or []:
            out.append((it.get("text_item") or {}).get("text", ""))
    return out


def make_bridge(tmpdir, fake_claude, allowed=(USER,)):
    cfg = dict(wechat_bridge.DEFAULTS)
    cfg.update({
        "base_url": BASE, "bot_token": "tok", "account_id": ACCOUNT,
        "allowed_user_ids": list(allowed), "require_prefix": True, "prefix": "/cc",
        "workdir": tmpdir, "claude_bin": fake_claude, "timeout_sec": 60,
        "claude_extra_args": [], "prompt_prefix": "PFX:", "enable_typing": True,
        "progress_notice_seconds": 0, "poll_timeout_seconds": 2,
        "max_reply_chars": 1800,
        # 默认关掉：不带前缀的提示会在别的用例里多出一条回复，扰乱它们对
        # 「这条没被执行」的判断。提示本身由 test_no_prefix_hint 单独测
        "no_prefix_hint_minutes": 0,
        # 默认关掉：真桌面是什么样不该影响别的用例，要测的用例自己塞假 launcher
        "app_launch": {"enable": False},
        # 同理：默认不装播放控制，免得任何用例真去按本机的媒体键
        "media_control": {"enable": False},
        # 同理：默认不装看画面/点鼠标，免得任何用例真去读窗口、真去点
        "screen_control": {"enable": False},
        # 同理，而且这条最要紧：默认不装电源通道。装上了的话，任何一个用例里只要
        # 有句话被 parse_shutdown 认出来，自测就会**真的把这台机器关掉**
        "power_control": {"enable": False},
        # 同理：默认不装进会通道，免得任何用例真去拉起腾讯会议
        "meeting": {"enable": False},
        # 同理：默认不装定时提醒，免得有用例真去写一份 reminders.json
        "remind": {"enable": False},
        # 同理：默认不装收附件那条线——装了的话任何带附件的用例都会真去联网下载、
        # 真往盘上写文件。要测的用例自己开，并把 inbox_dir / 登记表指到 tmpdir
        "inbox": {"enable": False},

        # 同理：默认不装屏幕亮度，免得任何用例真去起 PowerShell 改你屏幕的亮度
        "brightness": {"enable": False},
    })
    b = wechat_bridge.Bridge(cfg)
    b.runner.session_file = os.path.join(tmpdir, "sessions.json")
    b.runner._sessions = {}
    b.client.cursor_file = os.path.join(tmpdir, "buf.txt")
    b.client.cursor = ""
    # send_style 会落盘，别让自测去读写项目目录里那份真的
    b.client.style_file = os.path.join(tmpdir, "send_style.txt")
    b.client.send_style = None
    return b


def drain(bridge, msgs):
    """把消息塞给假服务端，让桥收一轮并等任务跑完。"""
    FakeILink.sent = []
    FakeILink.wrapped = []
    FakeILink.typing = []
    FakeILink.configs = []
    FakeILink.inbox = list(msgs)
    got = bridge.poll_once()
    bridge.queue.join()
    time.sleep(0.3)
    return got


# ---------- 开关应用的假零件（音量和进会那两条链路也要用） ----------

DESKTOP_FILES = ("波点音乐.lnk", "微信.lnk", "CC Switch.lnk", "PDFgear.lnk", "PDF2Zh.lnk",
                 "剪映专业版.lnk", "系统资源管理器.lnk", "便签.exe", "笔记.txt", "desktop.ini")
FAKE_TARGETS = {
    "波点音乐.lnk": "D:\\波点音乐\\bodian\\bodian_pc.exe",
    "微信.lnk": "D:\\WeChat\\WeChat.exe",
    "系统资源管理器.lnk": "C:\\Windows\\explorer.exe",
    "CC Switch.lnk": "D:\\安装位置\\CC Switch\\CC Switch.exe",
}


class FakeShell(object):
    """替掉 PowerShell 解析 .lnk 目标那一步，顺带数一下被调了几次（验缓存）。"""

    def __init__(self):
        self.calls, self.asked = 0, []

    def resolve(self, paths):
        self.calls += 1
        self.asked.append(list(paths))
        return {p: FAKE_TARGETS[os.path.basename(p)] for p in paths
                if os.path.basename(p) in FAKE_TARGETS}


def make_launcher(tmpdir, allow_close=True, kill_code=0):
    desktop = os.path.join(tmpdir, "假桌面")
    if not os.path.isdir(desktop):
        os.makedirs(desktop)
        for fn in DESKTOP_FILES:
            with open(os.path.join(desktop, fn), "w", encoding="utf-8") as fh:
                fh.write("x")
    shell = FakeShell()
    opened, killed = [], []
    lc = app_launcher.AppLauncher(
        dirs=[desktop], allow_close=allow_close,
        cache_file=os.path.join(tmpdir, "app_targets_%s.json" % allow_close),
        opener=opened.append,
        closer=lambda exe: (killed.append(exe), (kill_code, ""))[1],
        resolver=shell.resolve)
    return lc, desktop, opened, killed, shell
