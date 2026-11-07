# -*- coding: utf-8 -*-
"""微信 iLink（ClawBot）通道的最小客户端：扫码登录、长轮询收消息、发消息。

只用标准库，不需要第三方依赖。长轮询是纯出站请求，所以本机不需要公网 IP、
不需要隧道、也没有回调超时。

注意：下面所有字段名都是从 CodexPlusPlus 的 PoC 源码里读出来的，没有官方
开发文档可以对照。接口一改就可能挂，所以解析处一律做了兜底，并在对不上时
把服务端返回的键名打进日志——真机排查时看日志，不要靠猜。
"""

import base64
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import aes128

log = logging.getLogger("ilink")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
# Windows 系统代理可能截断腾讯接口的 TLS；只对已验证的官方 iLink 主机直连。
# 其他自定义地址（包括离线测试服务）仍使用原来的代理策略，证书校验保持开启。
_ILINK_HOST = "ilinkai.weixin.qq.com"
_FAKE_IP_RANGES = (ipaddress.ip_network("198.18.0.0/15"),
                   ipaddress.ip_network("2001:2::/48"))
# AliDNS 官方公布的服务地址（https://www.alidns.com/）。仅作 TCP 引导，
# HTTPS 仍用 dns.alidns.com 的 Host、SNI 和证书校验，不依赖系统 DNS。
_DNS_HOST = "dns.alidns.com"
_DNS_BOOTSTRAP = ("223.5.5.5", "223.6.6.6")
_DNS_LOCK = threading.Lock()
_DNS_CACHE = ((), 0)


def _is_fake_ip(address):
    ip = ipaddress.ip_address(address)
    return any(ip.version == network.version and ip in network
               for network in _FAKE_IP_RANGES)


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("微信连接的时间预算已用完")
    return seconds


def _timeout_seconds(timeout, default):
    return default if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT else float(timeout)


def _connect_addresses(connection, addresses, deadline, per_address=6):
    """仅替换 TCP 地址；每个地址的 TCP/TLS 握手共用剩余预算。"""
    original_connect = connection._create_connection
    failure = None
    try:
        for ip in addresses:
            _remaining(deadline)
            attempt_deadline = min(deadline, time.monotonic() + per_address)
            def connect_tcp(address, timeout, source):
                sock = socket.create_connection((ip, address[1]), _remaining(attempt_deadline), source)
                try:
                    sock.settimeout(_remaining(attempt_deadline))
                    return sock
                except OSError:
                    sock.close()
                    raise
            connection._create_connection = connect_tcp
            try:
                # 还没有发送 HTTP 请求，握手失败可以安全地换下一个地址。
                http.client.HTTPSConnection.connect(connection)
                _remaining(deadline)
                return ip
            except ssl.SSLCertVerificationError:
                if connection.sock is not None:
                    connection.sock.close()
                    connection.sock = None
                raise
            except OSError as exc:
                failure = exc
                if connection.sock is not None:
                    connection.sock.close()
                    connection.sock = None
    finally:
        connection._create_connection = original_connect
    _remaining(deadline)
    if failure is not None:
        raise failure
    raise OSError("没有可用的 HTTPS 服务器地址")


def _query_ilink_dns(deadline):
    connection = http.client.HTTPSConnection(_DNS_HOST, timeout=_remaining(deadline))
    try:
        _connect_addresses(connection, _DNS_BOOTSTRAP, deadline, per_address=3)
        sock = connection.sock
        path = "/resolve?" + urllib.parse.urlencode({"name": _ILINK_HOST, "type": "A"})
        sock.settimeout(_remaining(deadline))
        connection.request("GET", path, headers={"Accept": "application/dns-json"})
        sock.settimeout(_remaining(deadline))
        with connection.getresponse() as response:
            if response.status != 200:
                raise OSError("加密 DNS 返回 HTTP %s" % response.status)
            chunks, size = [], 0
            while not response.isclosed():
                sock.settimeout(_remaining(deadline))
                chunk = response.read1(min(8192, 65537 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 65536:
                    raise OSError("微信服务器的加密 DNS 响应过大")
        _remaining(deadline)
        return b"".join(chunks)
    finally:
        connection.close()


def _public_ilink_addresses(timeout, reason='fake_ip'):
    """本机解析不可信时查询加密 DNS；不包含登录凭据。"""
    global _DNS_CACHE
    deadline = time.monotonic() + min(_timeout_seconds(timeout, 8), 8)
    if not _DNS_LOCK.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("等待微信加密 DNS 查询超时")
    try:
        _remaining(deadline)
        addresses, expires = _DNS_CACHE
        if addresses and time.monotonic() < expires:
            return addresses
        raw = _query_ilink_dns(deadline)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise OSError("加密 DNS 返回的不是有效 JSON") from exc
        if not isinstance(data, dict) or data.get("Status") != 0:
            raise OSError("加密 DNS 未能解析微信服务器地址")
        answers = data.get("Answer", [])
        if not isinstance(answers, list):
            raise OSError("加密 DNS 返回的地址列表无效")
        addresses, ttl = [], 300
        for answer in answers:
            if not isinstance(answer, dict) or answer.get("type") != 1:
                continue
            try:
                ip = ipaddress.ip_address(answer.get("data", ""))
                answer_ttl = max(0, float(answer.get("TTL", 0)))
            except (ValueError, TypeError):
                continue
            if ip.version != 4 or not ip.is_global or ip.is_multicast:
                continue
            if str(ip) not in addresses:
                addresses.append(str(ip))
            ttl = min(ttl, answer_ttl)
        if not addresses:
            raise OSError("加密 DNS 未返回可用的微信公网地址")
        _DNS_CACHE = (tuple(addresses), time.monotonic() + ttl)
        if reason == 'dns_error':
            log.info("本机 DNS 解析微信域名失败，已改用加密 DNS 获取公网地址；仍校验微信证书")
        else:
            log.info("微信域名解析到代理虚拟地址，已通过加密 DNS 获取公网地址；仍校验微信证书")
        return _DNS_CACHE[0]
    finally:
        _DNS_LOCK.release()


class _ILinkHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        global _DNS_CACHE
        if self.host != _ILINK_HOST or self._tunnel_host:
            return super().connect()
        deadline = time.monotonic() + _timeout_seconds(self.timeout, 30)
        # 本机解析不可信有两种形态：解析到代理 Fake-IP，或系统／代理 DNS 整个不可用
        # （getaddrinfo 直接抛 Errno 11001）。后者原先直接失败，而能救场的加密 DNS
        # 恰恰被条件挡住，只能等本机 DNS 自己恢复。两种都改用加密 DNS。
        dns_error = None
        try:
            resolved = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            dns_error = exc
            resolved = None
        if resolved is not None and not any(_is_fake_ip(result[4][0]) for result in resolved):
            return super().connect()
        try:
            addresses = _public_ilink_addresses(
                _remaining(deadline), 'dns_error' if dns_error is not None else 'fake_ip')
        except OSError as exc:
            if dns_error is not None:
                log.warning("本机 DNS 失败后，加密 DNS 兜底也失败：%s", exc)
                raise dns_error from exc
            raise
        ip = _connect_addresses(self, addresses, deadline)
        # TCP/TLS 握手用共享预算；长轮询读取恢复调用者的完整超时。
        self.sock.settimeout(self.timeout)
        # 地址排序只是优化，不因另一个 DNS 查询占锁而阻塞已经成功的连接。
        if _DNS_LOCK.acquire(blocking=False):
            try:
                cached, expires = _DNS_CACHE
                if ip in cached:
                    _DNS_CACHE = ((ip,) + tuple(item for item in cached if item != ip), expires)
            finally:
                _DNS_LOCK.release()


class _ILinkHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_ILinkHTTPSConnection, req, context=self._context)


_DIRECT_ILINK = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _ILinkHTTPSHandler())


def _open_api(req, timeout):
    host = urllib.parse.urlsplit(req.full_url).hostname
    if host == "ilinkai.weixin.qq.com":
        return _DIRECT_ILINK.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)

# 发文件要先把加密后的内容 POST 到这个 CDN，再把它给的凭据放进消息里
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
APP_ID = "bot"
CHANNEL_VERSION = "2.1.3"
CLIENT_VERSION = str((2 << 16) | (1 << 8) | 3)      # 2.1.3 压成整数，抄 PoC

ITEM_TEXT, ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE = 1, 2, 3, 5, 6
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

ITEM_NAMES = {ITEM_IMAGE: "图片", ITEM_VIDEO: "视频", ITEM_FILE: "文件"}

# 发送侧的 item 类型是另一套编号，和上面那几个常量对不上——这是整个协议最容易
# 栽的地方。下面这套来自官方 npm 包 @weixin-ts/bot 的构建产物：
#     MessageItemType = {NONE:0, TEXT:1, IMAGE:2, VOICE:3, FILE:4, VIDEO:5}
# 上面那套是收消息时沿用 PoC 源码的编号（真机只逐一核对过文本），两者哪个对
# 不重要：收消息侧已经改成优先看 xxx_item 这个键名，不再依赖数字。
SEND_ITEM_TEXT, SEND_ITEM_IMAGE, SEND_ITEM_VOICE = 1, 2, 3
SEND_ITEM_FILE, SEND_ITEM_VIDEO = 4, 5
# getuploadurl 的 media_type 又是第三套编号（UploadMediaType），别混用
UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE, UPLOAD_VOICE = 1, 2, 3, 4
ENCRYPT_TYPE_AES = 1


class ILinkError(Exception):
    """网络失败或服务端返回非零 errcode。"""


class ILinkTimeout(ILinkError):
    """请求超时。这个通道好几个接口是"挂住等事件"，超时属于正常现象，不是故障。"""


def _is_timeout(exc):
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    return isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError))


def _wechat_uin():
    """随机 uint32 先转字符串再 base64。作用不明，PoC 里每个请求都带，缺了可能被拒。"""
    return base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode()).decode()


def _headers(token=None):
    h = {
        "Content-Type": "application/json",
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
        "X-WECHAT-UIN": _wechat_uin(),
    }
    if token:
        h["Authorization"] = "Bearer " + token
        h["AuthorizationType"] = "ilink_bot_token"
    return h


def _request(method, base_url, path, payload=None, token=None, timeout=40,
             check_err=True):
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    data = None
    if method == "POST":
        body = dict(payload or {})
        base_info = dict(body.get("base_info") or {})
        base_info.setdefault("channel_version", CHANNEL_VERSION)
        body["base_info"] = base_info
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif payload:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(payload)

    req = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
    try:
        with _open_api(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise ILinkError("%s 返回 HTTP %s：%s" % (path, exc.code, detail))
    except Exception as exc:                        # URLError / socket.timeout / ssl
        if _is_timeout(exc):
            raise ILinkTimeout("%s 在 %s 秒内没返回" % (path, timeout))
        raise ILinkError("%s 请求失败：%s" % (path, exc))

    try:
        obj = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise ILinkError("%s 返回的不是 JSON：%s" % (path, raw[:300]))
    if not isinstance(obj, dict):
        raise ILinkError("%s 返回的不是对象：%s" % (path, raw[:300]))

    if check_err:
        # 真机确认：get_bot_qrcode 用的是 ret 而不是 errcode，两套都查
        code = _first(obj, ["errcode", "err_code", "ret"], 0) or 0
        if code:
            msg = obj.get("errmsg") or obj.get("err_msg") or obj.get("ret_msg") or ""
            raise ILinkError("%s 返回 errcode=%s %s" % (path, code, msg))
    return obj


def api_post(base_url, path, payload=None, token=None, timeout=40, check_err=True):
    return _request("POST", base_url, path, payload, token, timeout, check_err)


def api_get(base_url, path, params=None, token=None, timeout=40, check_err=True):
    return _request("GET", base_url, path, params, token, timeout, check_err)


def _first(obj, names, default=None):
    """按顺序找第一个存在且非空的键。字段名没有文档，多备几个别名。"""
    for n in names:
        v = obj.get(n)
        if v not in (None, "", [], {}):
            return v
    return default


# 收消息侧的图片/文件里到底有哪些字段，没有任何文档可对照（见文件开头的说明）。
# 为了搞清楚「里面有没有可下载的凭据」，_parse_items 会把附件的**形状**记一条进
# DEBUG 日志，由 inbox_probe.py 读出来看。往日志里记形状的第一条铁律：名字像凭据的
# 字段只留长度和头一两个字符，绝不整条进日志。
# 注意这和 bridge_admin._mask 不是一回事：那个抹的是**日志文本里已经出现的凭据值**，
# 这个判的是**字段名像不像凭据**，所以两份名单各管一段，没有合并。
_SECRET_HINTS = ("key", "token", "param", "url", "sign", "secret", "cookie",
                 "auth", "passwd", "password", "ticket", "code")

# 形状最多看几个键。**这个数不能小**：真机上一条图片 item 有 8 个键
# （type / create_time_ms / update_time_ms / is_completed / msg_id /
# button_item_list / at_bot_username_list / image_item），而真正要看的
# `image_item` / `file_item` 恰好排在最后一个——6 个键的窗口会把它整个吃掉，
# 探针就会误报「没有下载凭据」（2026-09-26 真机上就是这么栽的）。
# 放到 16：多记几个键名而已，值都是掩过的，不会带凭据。
SHAPE_KEY_LIMIT = 16


def _looks_like_secret(name):
    """字段名看着像凭据吗。宁可多掩几个，也不让下载凭据漏进日志。"""
    low = str(name).lower()
    return any(h in low for h in _SECRET_HINTS)


def _shape_value(key, val, limit, depth):
    """把一个值压成「形状」：值只留类型、长度和一点点预览。"""
    if isinstance(val, dict):
        if depth >= 3:
            return "字典 %d 个键" % len(val)
        return _shape_dict(val, limit, depth + 1)
    if isinstance(val, (list, tuple)):
        if depth >= 3:
            return "列表 %d 项" % len(val)
        head = [_shape_value(key, v, limit, depth + 1) for v in list(val)[:limit]]
        if len(val) > limit:
            head.append("…还有 %d 项" % (len(val) - limit))
        return head
    if isinstance(val, str):
        if _looks_like_secret(key):
            return "%d 字符，头 2 字符 %r" % (len(val), val[:2])
        if len(val) > 40:
            return "%d 字符，头 12 字符 %r" % (len(val), val[:12])
        return val
    if val is None or isinstance(val, (int, float, bool)):
        return val
    return "<%s>" % type(val).__name__


def _shape_dict(obj, limit, depth):
    out = {}
    keys = list(obj.keys())
    for k in keys[:limit]:
        out[str(k)] = _shape_value(k, obj[k], limit, depth)
    if len(keys) > limit:
        out["…"] = "还有 %d 个键" % (len(keys) - limit)
    return out


def item_shape(obj, limit=SHAPE_KEY_LIMIT):
    """把一条 item 压成「形状」：键名留着，值只留类型和少量预览。

    两条规矩：① 名字像凭据的字段（见 _SECRET_HINTS）只留长度和头 2 个字符；
    ② 其余字符串超过 40 字符也只留头 12 个字符。最多看 limit 个键（默认
    SHAPE_KEY_LIMIT，见那边的说明：把这个数调小会把 image_item 挤掉，
    探针就误报「没有下载凭据」）、最多往下三层，多出来的一律用「…」顶替
    ——目的是「看得出结构」，不是「存下内容」。
    """
    if isinstance(obj, dict):
        return _shape_dict(obj, limit, 0)
    return _shape_value("", obj, limit, 0)


def _as_int(val, default=0):
    """字段里的数字可能给成字符串（真机上 file_item.len 就是 "27353"）。"""
    if isinstance(val, bool) or val is None:
        return default
    if isinstance(val, int):
        return val
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


# 收侧附件里「下载要用的东西」的字段名，是 2026-09-26 从真机上抓的形状
# （抓法见 inbox_probe.py）。一条图片 item 长这样：
#     image_item.media.full_url            完整下载地址（https://…，**不用再拼**）
#     image_item.media.encrypt_query_param 728 字符的密文凭据（备用）
#     image_item.media.aes_key             44 字符 = 那 32 个 hex 字符的 base64
#     image_item.aeskey                    32 个 hex 字符（密钥本体，解密的就用这个）
#     image_item.hd_size / mid_size / thumb_size
# 文件那条多三个：
#     file_item.file_name / file_item.len（明文字节数，**是字符串**）
#     file_item.md5（**明文的** md5，落盘后能拿它校验）
_DL_ITEM_KEYS = (("image_item", "图片"), ("file_item", "文件"),
                 ("video_item", "视频"))


def media_download_info(item):
    """从一条 item 里抠出下载要用的字段；不是「带下载地址的附件」就返回 None。

    只认 `media.full_url`。**故意不自己拼下载地址**：真机上是服务端直接把完整
    URL 给的，猜端点名（/download 之类）猜错了就是白写一遍。
    """
    if not isinstance(item, dict):
        return None
    for key, kind in _DL_ITEM_KEYS:
        body = item.get(key)
        if not isinstance(body, dict):
            continue
        media = body.get("media")
        if not isinstance(media, dict):
            media = body
        url = str(_first(media, ["full_url", "fullUrl", "download_url",
                                 "downloadUrl", "url"], "") or "")
        if not url.lower().startswith(("http://", "https://")):
            return None
        # 大小有两个来源，**可信度不一样**：
        #   · 文件：`file_item.len` —— 真机上就是明文字节数（是字符串），可以拿它做
        #     「解出来多长」的精确校验；
        #   · 图片：只有 `hd_size` / `mid_size`（真机实测 hd_size 等于我们解出来的
        #     字节数，所以拿它当**大小提示**没问题：预估要不要下、日志里写多大）。
        #     但**不拿它做精确校验**——万一服务端给的其实是 mid 那一版，校验就会把
        #     好图误拒。所以多带一个 exact 标志出去。
        size = _as_int(_first(body, ["len", "rawsize", "raw_size"], 0))
        exact = bool(size)
        if not size:
            size = _as_int(_first(body, ["hd_size", "mid_size", "thumb_size"], 0))
        return {
            "kind": kind,
            "key_name": key,
            "url": url,
            "aeskey": str(_first(body, ["aeskey", "aes_key_hex"], "") or ""),
            "aes_key_b64": str(_first(media, ["aes_key", "aesKey"], "") or ""),
            "name": str(_first(body, ["file_name", "filename", "name"], "") or ""),
            "size": size,
            "exact": exact,
            "md5": str(_first(body, ["md5", "rawfilemd5"], "") or "").lower(),
        }
    return None


def media_key_bytes(info):
    """解这条附件密文用的 16 字节密钥。

    优先 `aeskey`（32 个 hex 字符，就是密钥本体）。没有就从 `media.aes_key` 里解：
    那个 base64 解出来**是一串 32 个 hex 字符的文本**，不是 16 字节原始密钥
    ——发送侧就是这么拼的（见 selftest_files 里那条断言），别搞反。
    """
    hexs = (info.get("aeskey") or "").strip()
    if len(hexs) == 32:
        try:
            return bytes.fromhex(hexs)
        except ValueError:
            pass
    b64 = (info.get("aes_key_b64") or "").strip()
    if b64:
        try:
            text = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("ascii")
        except Exception:
            text = ""
        if len(text) == 32:
            try:
                return bytes.fromhex(text.strip())
            except ValueError:
                pass
    raise ILinkError("这条附件里没有能用的 aeskey（aeskey 和 media.aes_key 都不成样子），"
                     "解不开就不用写了")


def stream_download(url, timeout=120, chunk=64 * 1024):
    """流式 GET 一个下载地址，一段段 yield 字节。

    地址直接用收侧给的 `media.full_url`（真机上就是完整 https 地址）。按块读是
    为了「边下边解密边落盘」——纯 Python 的 AES 大约 1.4 MB/秒，几十 MB 的文件
    整份进内存没必要。
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                yield buf
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        raise ILinkError("CDN 下载返回 HTTP %s：%s" % (exc.code, body))
    except Exception as exc:
        if _is_timeout(exc):
            raise ILinkTimeout("CDN 下载在 %s 秒内没读完" % timeout)
        raise ILinkError("CDN 下载失败：%s" % exc)


class Message(object):
    """把 iLink 的一条消息抹平成桥接层要用的几个字段。"""

    __slots__ = ("raw", "message_id", "from_user_id", "context_token",
                 "message_type", "text", "attachments", "quoted",
                 "_journal_id", "_inbox_journal_id", "_receipt_outcome",
                 "_progress_stage", "_progress_started", "_task_control", "_inbox_enqueued_at", "_result_summary", "_output_files", "_inbox_summary", "_inbox_outputs",
                 "_dispatch_ready", "_receive_started", "_execution_state", "_desktop_origin",
                 "_queue_id", "_artifacts", "_artifact_saved", "_batch_intake_id",
                 "_template_checks", "_result_checks", "_result_checks_saved",
                 "_batch_intake_entries", "_batch_intake_done", "_batch_frozen", "_batch_recent_entries",
                 "_task_output_dir", "_desktop_file_delivery")

    def __init__(self, raw):
        self.raw = raw
        self.message_id = str(_first(raw, ["message_id", "msg_id", "msgid",
                                           "client_id", "id"], ""))
        self.from_user_id = str(_first(raw, ["from_user_id", "sender_id",
                                             "from_userid", "from"], ""))
        self.context_token = _first(raw, ["context_token"], "")
        self.message_type = _first(raw, ["message_type", "msg_type"], 1)
        self.text, self.attachments = self._parse_items(raw)
        ref = _first(raw, ["ref_msg", "refer_msg", "quote_msg"]) or {}
        self.quoted = self._parse_items(ref)[0] if isinstance(ref, dict) else ""

    @staticmethod
    def _parse_items(raw):
        """把 item_list 拆成（文字, 附件名字列表）。

        **优先看 xxx_item 这个键名，数字只作兜底。** 因为发送侧和收消息侧的类型
        编号是两套（见文件开头的常量注释），谁对谁错没有文档；而键名两边一致。
        """
        texts, atts = [], []
        by_key = [("text_item", None), ("voice_item", "语音"),
                  ("image_item", "图片"), ("video_item", "视频"),
                  ("file_item", "文件")]
        for item in (_first(raw, ["item_list", "items"]) or []):
            if not isinstance(item, dict):
                continue
            hit = None
            for key, label in by_key:
                if key in item:
                    hit = (key, label)
                    break
            if hit is None:
                itype = _first(item, ["type", "item_type"], 0)
                if itype == ITEM_TEXT:
                    hit = ("text", None)
                elif itype == ITEM_VOICE:
                    hit = ("voice", "语音")
                elif itype in ITEM_NAMES:
                    hit = (None, ITEM_NAMES[itype])
                else:
                    continue
            key, label = hit
            body = _first(item, [key]) if key else None
            got = ""
            if isinstance(body, str):
                got = body
            elif isinstance(body, dict):
                # 语音消息服务端已经转写好了，转写文字和正文一样从 text 里取
                got = str(_first(body, ["text", "content"], ""))
            if label is not None and not got:
                # 收消息侧的字段名没有文档（见文件开头），把附件形状记一条进 DEBUG，
                # 由 inbox_probe.py 读出来判「里面到底有没有可下载的凭据」。
                log.debug("收到附件（%s）的字段形状：%s", label,
                          json.dumps(item_shape(item), ensure_ascii=False))
            if label is None:
                texts.append(got)
            elif got:
                texts.append(got)                    # 语音有转写就当文字用
            else:
                atts.append(label)
        return "\n".join(t for t in texts if t).strip(), atts


# 超长单行往回收的时候，认这些位置当「断在这儿不难看」：空白、中英标点、路径分隔符
_CUT_CHARS = " \t　，。；、：！？,.;:!?)/\\】」》"


def _cut_at(s, limit):
    """超长单行该断在第几个字符。返回切点（一定 <= limit）。"""
    if len(s) <= limit:
        return len(s)
    floor = max(1, limit - limit // 4)
    for i in range(limit, floor, -1):
        if s[i - 1] in _CUT_CHARS:
            return i
    return limit


def split_text(text, limit=1800):
    """按行切成不超过 limit 个字符的几段，尽量不在行中间断开。"""
    text = (text or "").strip() or "（没有输出）"
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines(True):
        while len(line) > limit:            # 单行就超长：往回找标点断，别把 URL 劈成两半
            if cur.strip():
                chunks.append(cur)
            cur = ""
            cut = _cut_at(line, limit)
            chunks.append(line[:cut])
            line = line[cut:]
        if len(cur) + len(line) > limit:
            if cur.strip():
                chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur.strip():
        chunks.append(cur)
    return [c.strip("\n") for c in chunks if c.strip()]


def _scan_for_msg_list(obj, path="", depth=0):
    """在返回里找"像消息列表"的东西：元素是 dict 且带 item_list / 发送者字段的 list。

    消息列表的键名没有文档。别名列表可能全对不上，而这种情况下服务端仍然会给
    一个正常的游标，看日志只会看到"一切正常但收不到消息"——所以按形状再找一遍。
    """
    found = []
    if depth > 4:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += _scan_for_msg_list(v, "%s.%s" % (path, k) if path else str(k), depth + 1)
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        keys = set()
        for it in obj[:5]:
            if isinstance(it, dict):
                keys |= set(it.keys())
        if keys & {"item_list", "items", "from_user_id", "sender_id", "message_id",
                   "msg_id", "msgid", "context_token"}:
            found.append((path, obj))
    return found


def _scan_for_upload_url(obj, path="", depth=0):
    """在 getuploadurl 的返回里按形状找上传地址：一个带 upload 的 http 链接。

    和找消息列表、找二维码同一个思路——键名没有文档，别名列表可能全对不上，
    这时候按「内容长什么样」找比按名字找靠谱。
    """
    found = []
    if depth > 4:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += _scan_for_upload_url(v, "%s.%s" % (path, k) if path else str(k),
                                          depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):
            found += _scan_for_upload_url(v, "%s[%d]" % (path, i), depth + 1)
    elif isinstance(obj, str) and obj.lower().startswith(("http://", "https://")):
        if "upload" in obj.lower():
            found.append((path, obj))
    return found


class ILinkClient(object):
    supports_transfer_cancellation = True
    def __init__(self, base_url, token, account_id, state_dir=None,
                 poll_timeout=35, max_reply_chars=1800):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token
        self.account_id = account_id
        self.poll_timeout = int(poll_timeout)
        self.max_reply_chars = int(max_reply_chars)
        self.state_dir = state_dir or os.path.dirname(os.path.abspath(__file__))
        self.cursor_file = os.path.join(self.state_dir, "get_updates_buf.txt")
        self.cursor = self._load_cursor()
        self._warned_keys = set()
        self.style_file = os.path.join(self.state_dir, "send_style.txt")
        self.send_style = self._load_send_style()     # sendmessage 试出来的那套写法

    # ---------- 游标 ----------

    def _load_cursor(self):
        try:
            with open(self.cursor_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except (IOError, OSError):
            return ""

    def _load_send_style(self):
        """试出来的写法落盘，重启后不必再白挨几次 invalid arguments。"""
        try:
            with open(self.style_file, "r", encoding="utf-8") as fh:
                return fh.read().strip() or None
        except (IOError, OSError):
            return None

    def _save_send_style(self, name):
        try:
            with open(self.style_file, "w", encoding="utf-8") as fh:
                fh.write(name)
        except (IOError, OSError) as exc:
            log.debug("记不下 send_style（忽略）：%s", exc)

    def commit_cursor(self, buf):
        """分发后保存游标；磁盘替换成功后再更新内存。内存任务队列不具备崩溃恢复能力。"""
        if not buf or buf == self.cursor:
            return
        tmp = self.cursor_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(buf)
        os.replace(tmp, self.cursor_file)
        self.cursor = buf


    # ---------- 收 ----------

    def get_updates(self):
        """长轮询一次，返回 (消息列表, 新游标)。服务端最多挂 poll_timeout 秒。"""
        payload = {"get_updates_buf": self.cursor}
        obj = api_post(self.base_url, "ilink/bot/getupdates", payload, self.token,
                       timeout=self.poll_timeout + 15)
        new_buf = _first(obj, ["get_updates_buf", "next_buf", "buf", "sync_buf"], self.cursor)
        # 真机确认（2026-08-29）：消息列表的键名是 msgs，返回里同时还有一个 sync_buf
        raw_list = _first(obj, ["msgs", "message_list", "messages", "msg_list",
                                "update_list"]) or []
        if not isinstance(raw_list, list):
            raw_list = []
        if not raw_list:
            # 空轮询是常态，但"服务端明明给了消息、只是键名不认识"看起来一模一样，
            # 所以每次都把顶层键名记进 DEBUG，并按形状再找一遍。
            log.debug("getupdates 没解出消息，顶层键：%s", sorted(obj.keys()))
            hits = _scan_for_msg_list(obj)
            if hits:
                where, raw_list = hits[0]
                if where not in self._warned_keys:
                    self._warned_keys.add(where)
                    log.warning("消息列表的键名不在别名列表里，实际在 %s（%d 条）。"
                                "已经按这个键继续用了，但建议把它加进 ilink_client 的别名列表。",
                                where, len(raw_list))
        for m in raw_list:
            # 发消息的字段名要照着收到的消息抄，所以原样记一条进 DEBUG
            log.debug("收到原始消息：%s", json.dumps(m, ensure_ascii=False)[:1500])
        msgs = [Message(m) for m in raw_list if isinstance(m, dict)]
        return msgs, new_buf

    # ---------- 发 ----------

    def _base_body(self, to_user_id, items, context_token, client_id=None):
        base = {"to_user_id": to_user_id,
                "client_id": client_id or str(random.randint(1, 0x7FFFFFFF)),
                "message_type": MSG_TYPE_BOT, "message_state": MSG_STATE_FINISH,
                "item_list": list(items)}
        if self.account_id:
            base["account_id"] = self.account_id
        if context_token:
            base["context_token"] = context_token
        return base

    def _send_variants(self, to_user_id, body, context_token):
        """sendmessage 的字段组合没有文档，真机上第一次试返回 errcode=-2 invalid arguments。

        所以准备几套写法，从最完整的开始试，哪套过了就记住、后面一直用它。
        client_id 在几套之间保持不变：万一有两套都被服务端接受，也能靠它去重。
        """
        item = {"type": ITEM_TEXT, "text_item": {"text": body}}
        cid = str(random.randint(1, 0x7FFFFFFF))
        base = self._base_body(to_user_id, [item], context_token, cid)

        # 真机确认（2026-08-29）：body 要在外面再包一层 msg，里面的字段名全都是对的。
        # 所以把这套排在最前面——它是已知能过的，其余六套只在服务端又改了协议时才轮得到。
        out = [("包一层 msg", {"msg": base}), ("完整", base)]

        v = dict(base)                               # 多余字段也会被判 invalid arguments
        v.pop("account_id", None)
        out.append(("去掉 account_id", v))

        v = dict(base)
        v["to_user"] = v.pop("to_user_id")
        out.append(("to_user 而不是 to_user_id", v))

        v = dict(base)
        v["msg_type"] = v.pop("message_type")
        v["msg_state"] = v.pop("message_state")
        out.append(("msg_type/msg_state", v))

        v = dict(base)
        v["item_list"] = [{"type": ITEM_TEXT, "text": {"text": body}}]
        out.append(("text 而不是 text_item", v))

        v = {"to_user_id": to_user_id, "client_id": cid, "item_list": [item]}
        if context_token:
            v["context_token"] = context_token
        out.append(("最小集合", v))
        return out

    def _send_one(self, payload):
        api_post(self.base_url, "ilink/bot/sendmessage", payload, self.token, timeout=30)

    def send_chunk(self, to_user_id, body, context_token=None):
        """发一条。字段组合未知时逐套试，试出来的那套记进 send_style.txt 复用。"""
        variants = self._send_variants(to_user_id, body, context_token)
        if self.send_style is not None:
            for name, payload in variants:
                if name != self.send_style:
                    continue
                try:
                    log.debug("sendmessage 用 %s：%s", name,
                              json.dumps(payload, ensure_ascii=False)[:800])
                    return self._send_one(payload)
                except ILinkTimeout:
                    raise
                except ILinkError as exc:
                    # 记住的写法失效了（服务端改协议）：清掉重新试一轮，别就此发不出消息
                    log.warning("记住的「%s」写法不管用了（%s），重新逐套试。", name, exc)
                    self.send_style = None
                break
        last = None
        for name, payload in variants:
            try:
                log.debug("sendmessage 试 %s：%s", name,
                          json.dumps(payload, ensure_ascii=False)[:800])
                self._send_one(payload)
            except ILinkTimeout:
                raise                                # 超时不是字段问题，别拿同一条反复试
            except ILinkError as exc:
                last = exc
                log.info("sendmessage 的「%s」写法被拒：%s", name, exc)
                continue
            self.send_style = name
            self._save_send_style(name)
            log.warning("sendmessage 认的是「%s」这套写法，已记下，后面一直用它。", name)
            return
        raise ILinkError("sendmessage 所有写法都被拒，最后一个错误：%s" % last)

    def send_text(self, to_user_id, text, context_token=None, *, start_chunk=0, chunk_size=None, on_chunk=None):
        """长回复自动分片，带「（1/3）」标号逐条发，最后一条多一个「· 完」。

        标号刻意用中文括号而不是 `[1/3]`：后者在手机上读着像日志行，前者一眼
        能看出「这条是长回复的第几段、后面还有没有」。
        """
        chunks = split_text(text, self.max_reply_chars if chunk_size is None else chunk_size)
        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            if i <= start_chunk:
                continue
            if total == 1:
                body = chunk
            else:
                body = "（%d/%d%s）\n%s" % (i, total,
                                           " · 完" if i == total else "", chunk)
            self.send_chunk(to_user_id, body, context_token)
            if on_chunk is not None:
                on_chunk(i, total)
            if i < total:
                time.sleep(0.4)                      # 别把几条挤在同一毫秒发出去
        return total

    # ---------- 发文件（先加密传 CDN，再把凭据放进消息里） ----------

    def send_item(self, to_user_id, item, context_token=None):
        """发一条非文字消息（文件/图片）。

        文本那条路要逐套试字段组合，这里不必：真机已经验证过 body 要包一层 msg，
        里面的字段名全对。所以只准备两套（包一层 / 不包），第一套失败才试第二套。
        """
        base = self._base_body(to_user_id, [item], context_token)
        tries = [("包一层 msg", {"msg": base}), ("完整", base)]
        if self.send_style == "完整":
            tries.reverse()
        last = None
        for name, payload in tries:
            try:
                log.debug("sendmessage（媒体）用 %s：%s", name,
                          json.dumps(payload, ensure_ascii=False)[:800])
                self._send_one(payload)
                return name
            except ILinkTimeout:
                raise
            except ILinkError as exc:
                last = exc
                log.info("发媒体消息的「%s」写法被拒：%s", name, exc)
        raise ILinkError("发媒体消息的两套写法都被拒，最后一个错误：%s" % last)

    def get_upload_url(self, to_user_id, filekey, media_type, rawsize,
                       rawfilemd5, filesize, aeskey_hex):
        """跟 iLink 换一个 CDN 上传地址。

        字段名来自官方 npm 包 @weixin-ts/cdn 的构建产物（逐字抄的），但**返回**里
        那两个键名是我推的：那个包把 uploadFullUrl/uploadParam 当参数收，没露出
        它们在响应里叫什么。所以别名多备几个，再按形状找一遍 URL，最后无论如何
        把顶层键名记进日志——真机上对不上就照日志改别名，别猜。
        """
        payload = {
            "filekey": filekey,
            "media_type": int(media_type),
            "to_user_id": to_user_id,
            "rawsize": int(rawsize),
            "rawfilemd5": rawfilemd5,
            "filesize": int(filesize),
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
        }
        obj = api_post(self.base_url, "ilink/bot/getuploadurl", payload,
                       self.token, timeout=30)
        full = _first(obj, ["upload_full_url", "uploadFullUrl", "upload_url",
                            "uploadurl", "full_url", "url"], "")
        param = _first(obj, ["upload_param", "uploadParam", "encrypted_query_param",
                             "encrypt_query_param", "param"], "")
        cdn = _first(obj, ["cdn_base_url", "cdnBaseUrl", "cdn_url", "baseurl",
                           "base_url"], "")
        if not full:
            hits = _scan_for_upload_url(obj)
            if hits:
                where, full = hits[0]
                log.warning("getuploadurl 的上传地址不在别名列表里，实际在 %s。"
                            "已经用上了，但建议把这个键名加进 ilink_client。", where)
        log.info("getuploadurl 顶层键：%s（拿到 %s）", sorted(obj.keys()),
                 "完整 URL" if full else ("upload_param" if param else "什么都没有"))
        if not full and not param:
            raise ILinkError("getuploadurl 没给上传地址，返回键：%s。"
                             "把这几个键名对照 ilink_client.get_upload_url 里的别名列表改。"
                             % sorted(obj.keys()))
        return {"full_url": full or "", "param": param or "",
                "cdn_base_url": cdn or "", "raw": obj}

    def upload_to_cdn(self, ciphertext, filekey, full_url="", param="",
                      cdn_base_url="", timeout=120):
        """把密文整体 POST 给 CDN，返回 download_param。

        不是 multipart、不分片、没有额外的头，body 就是裸密文。返回的凭据可能在
        JSON 里，也可能在几个响应头里（那个官方包自己就列了五个候选头名，说明
        它作者也没把握），所以两边都找。
        """
        url = full_url or ("%s/upload?encrypted_query_param=%s&filekey=%s" % (
            (cdn_base_url or DEFAULT_CDN_BASE_URL).rstrip("/"),
            urllib.parse.quote(param, safe=""),
            urllib.parse.quote(filekey, safe="")))
        req = urllib.request.Request(
            url, data=bytes(ciphertext), method="POST",
            headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                headers, raw = resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            why = exc.headers.get("x-error-message") if exc.headers else ""
            body = exc.read().decode("utf-8", "replace")[:400]
            raise ILinkError("CDN 上传返回 HTTP %s：%s %s" % (exc.code, why or "", body))
        except Exception as exc:
            if _is_timeout(exc):
                raise ILinkTimeout("CDN 上传在 %s 秒内没返回" % timeout)
            raise ILinkError("CDN 上传失败：%s" % exc)

        text = raw.decode("utf-8", "replace").strip()
        got = ""
        if text.startswith("{"):
            try:
                body = json.loads(text)
                if isinstance(body, dict):
                    got = _first(body, ["download_param", "encrypt_query_param",
                                        "downloadParam"], "")
            except ValueError:
                pass
        if not got:
            for name in ("x-encrypted-param", "download_param",
                         "encrypt_query_param", "x-download-param",
                         "x-encrypt-query-param"):
                got = headers.get(name) or ""
                if got:
                    break
        if not got:
            raise ILinkError("CDN 收了文件但没给 download_param。响应头：%s；"
                             "正文前 200 字：%s"
                             % (sorted(k.lower() for k in headers.keys()), text[:200]))
        return got

    def upload_file(self, to_user_id, data, media_type=UPLOAD_FILE):
        """加密 + 换地址 + 传 CDN。返回后面拼消息要用的几个值。"""
        aeskey = os.urandom(16)
        aeskey_hex = aeskey.hex()                     # 32 个小写十六进制字符
        filekey = os.urandom(16).hex()
        rawsize = len(data)
        rawfilemd5 = hashlib.md5(data).hexdigest()    # 明文的 md5，不是密文的
        started = time.time()
        cipher = aes128.encrypt_ecb_pkcs7(aeskey, data)
        log.info("加密完 %d 字节 → %d 字节，用了 %.1f 秒", rawsize, len(cipher),
                 time.time() - started)
        got = self.get_upload_url(to_user_id, filekey, media_type, rawsize,
                                  rawfilemd5, len(cipher), aeskey_hex)
        download_param = self.upload_to_cdn(
            cipher, filekey, got["full_url"], got["param"], got["cdn_base_url"])
        return {"filekey": filekey, "aeskey_hex": aeskey_hex,
                "download_param": download_param, "rawsize": rawsize,
                "ciphersize": len(cipher)}

    def send_file(self, to_user_id, path, file_name=None, context_token=None, control=None):
        """把本机一个文件发到微信。返回 (文件名, 字节数)。

        注意 file_item 里两个数的口径不一样（这不是笔误，是照着官方包抄的）：
        len 是**明文**大小而且是字符串，图片/视频那两个 size 字段用的是密文大小。
        """
        if control is not None:
            from file_transfer import send_cancellable
            return send_cancellable(self, to_user_id, path, file_name, context_token, control)
        with open(path, "rb") as fh:
            data = fh.read()
        name = file_name or os.path.basename(path.replace("\\", "/")) or "file"
        up = self.upload_file(to_user_id, data, UPLOAD_FILE)
        media = {
            "encrypt_query_param": up["download_param"],
            # base64 的是那 32 个十六进制**字符**，不是 16 字节原始密钥
            "aes_key": base64.b64encode(up["aeskey_hex"].encode()).decode(),
            "encrypt_type": ENCRYPT_TYPE_AES,
        }
        item = {"type": SEND_ITEM_FILE,
                "file_item": {"media": media, "file_name": name,
                              "len": str(up["rawsize"])}}
        self.send_item(to_user_id, item, context_token)
        return name, up["rawsize"]

    # ---------- 「正在输入」 ----------

    def typing_ticket(self, to_user_id, context_token=None):
        # 真机报的是 errcode=-2 ilink_user_id required：这个接口认的是 ilink_user_id。
        # 三个名字一起传，多余的字段这个接口不介意（sendmessage 才挑）。
        payload = {"account_id": self.account_id, "to_user_id": to_user_id,
                   "ilink_user_id": to_user_id, "user_id": to_user_id}
        if context_token:
            payload["context_token"] = context_token
        obj = api_post(self.base_url, "ilink/bot/getconfig", payload, self.token, timeout=20)
        ticket = _first(obj, ["typing_ticket"])
        if not ticket:
            cfg = _first(obj, ["config", "bot_config"]) or {}
            if isinstance(cfg, dict):
                ticket = _first(cfg, ["typing_ticket"])
        if not ticket:
            log.debug("getconfig 里没有 typing_ticket，顶层键：%s",
                      sorted(obj.keys()) if isinstance(obj, dict) else type(obj))
        return ticket or ""

    def send_typing(self, to_user_id, ticket, status):
        """status 1 开始输入 / 0 结束。纯装饰，失败绝不能影响任务。"""
        if not ticket:
            return False
        payload = {
            "account_id": self.account_id,
            "to_user_id": to_user_id,
            "ilink_user_id": to_user_id,
            "typing_ticket": ticket,
            "status": int(status),
        }
        try:
            api_post(self.base_url, "ilink/bot/sendtyping", payload, self.token, timeout=15)
            return True
        except ILinkError as exc:
            log.debug("sendtyping 失败（忽略）：%s", exc)
            return False


# base64.b64decode 对随便一串字母都会"成功"，所以判断图片只能靠魔数，
# 不能靠"解码没报错"。第一次真机跑就是这么写出了一个 24 字节的假 PNG。
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"),
               (b"GIF8", "gif"), (b"BM", "bmp"), (b"RIFF", "webp"))
LINK_PREFIXES = ("http://", "https://", "weixin://", "wxp://")


def _image_kind(raw):
    for magic, kind in IMAGE_MAGIC:
        if raw.startswith(magic):
            return kind
    return ""


def _looks_like_link(text):
    return isinstance(text, str) and text.strip().lower().startswith(LINK_PREFIXES)


def _try_b64(text):
    """尽力把字符串当 base64 解开，失败返回空 bytes。补齐 padding。"""
    s = "".join(str(text).split())
    if "base64," in s:                               # data:image/png;base64,xxx
        s = s.split("base64,", 1)[1]
    s = s.replace("-", "+").replace("_", "/")        # urlsafe 变体
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except Exception:
        return b""


def _scan_for_qr(obj, path="", depth=0):
    """在整个返回里递归找像二维码的值：链接，或者能解出图片魔数的 base64。

    字段名没有官方文档，顶层别名列表总有可能全都对不上，这时候按"内容长什么样"
    找比按名字找靠谱。返回 [(键路径, 值)]。
    """
    found = []
    if depth > 4:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += _scan_for_qr(v, "%s.%s" % (path, k) if path else str(k), depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):
            found += _scan_for_qr(v, "%s[%d]" % (path, i), depth + 1)
    elif isinstance(obj, str) and len(obj) >= 8:
        if _looks_like_link(obj) or _image_kind(_try_b64(obj)):
            found.append((path, obj))
    return found


def _render_qr_png(text, out_dir):
    """把一段文字画成二维码 PNG。没有可用的库就返回 ""，调用方给出备选办法。

    真机上 get_bot_qrcode 返回的是 qrcode_img_content 这个链接，图得自己画。
    这是唯一一处可选的第三方依赖（segno 是纯 Python 无依赖），装不装都能登录。
    """
    path = os.path.join(out_dir, "ilink_qrcode.png")
    try:
        import segno
        segno.make(text, error="m").save(path, scale=8, border=2)
        return path
    except Exception as exc:
        log.debug("segno 不可用：%s", exc)
    try:
        import qrcode
        qrcode.make(text).save(path)
        return path
    except Exception as exc:
        log.debug("qrcode 不可用：%s", exc)
    return ""


def _save_qrcode(value, out_dir):
    """服务端可能给 URL，也可能给 base64 图片。只在确认是图片时才写文件。"""
    if not value:
        return "服务端没返回二维码内容，原始返回见 ilink_login_raw.json。"
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        s = value.strip()
        if _looks_like_link(s):
            png = _render_qr_png(s, out_dir)
            if png:
                return "二维码已存到 %s，用微信扫它。\n（它编的就是这个链接：%s）" % (png, s)
            return ("服务端给的是链接而不是图片，本机又没有能画二维码的库，所以没写文件。\n"
                    "%s\n"
                    "两条路：① 装一个纯 Python 的库再重跑登录 —— pip install segno；"
                    "② 把上面这条链接发到自己微信（文件传输助手），在手机上点开确认。\n"
                    "别贴到网上的二维码生成器里，那等于把登录链接交给第三方。" % s)
        raw = _try_b64(s)
        if not raw:
            return ("这个值既不是链接也不是 base64（%d 字符），没写文件。原文：\n%s\n"
                    "完整返回见 ilink_login_raw.json。" % (len(s), s[:300]))
    else:
        return ("二维码字段不是字符串而是 %s，没写文件。完整返回见 ilink_login_raw.json。"
                % type(value).__name__)

    kind = _image_kind(raw)
    if not kind:
        return ("解出来的 %d 字节不是任何常见图片格式（开头 %s），没写文件——"
                "上一版就是在这里写出了一个打不开的 PNG。完整返回见 ilink_login_raw.json。"
                % (len(raw), raw[:12].hex() or "空"))
    path = os.path.join(out_dir, "ilink_qrcode." + kind)
    with open(path, "wb") as fh:
        fh.write(raw)
    return "二维码已存到 %s（%s，%d 字节），用微信扫它。" % (path, kind, len(raw))


def _dump_raw(obj, out_dir, say, name="ilink_login_raw.json"):
    """把 get_bot_qrcode 的原始返回落盘。这个接口没文档，第一次跑必须看到全貌。

    注意只 dump 这一个接口：get_qrcode_status 确认后的返回里带 bot_token，不能落盘。
    """
    keys = sorted(obj.keys())
    path = os.path.join(out_dir, name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        return say("get_bot_qrcode 顶层键：%s\n原始返回已存到 %s" % (keys, path))
    except OSError as exc:
        return say("get_bot_qrcode 顶层键：%s（原始返回写文件失败：%s）" % (keys, exc))


def qr_login(base_url=DEFAULT_BASE_URL, bot_type=3, out_dir=None,
             poll_interval=2, timeout=300, printer=None, status_timeout=60):
    """扫码登录，返回 {bot_token, account_id, base_url}。整个过程不需要公网。

    status_timeout 是单次状态查询的等待上限：这个接口会挂住等扫码，超时不算失败。
    timeout 是整体放弃时限——留够你找到二维码、扫、点确认的时间。
    """
    say = printer or (lambda m: print(m))
    out_dir = out_dir or os.path.dirname(os.path.abspath(__file__))
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    obj = api_get(base_url, "ilink/bot/get_bot_qrcode", {"bot_type": bot_type})
    _dump_raw(obj, out_dir, say)
    # 真机确认的形状：{"qrcode": "<32位十六进制 key>",
    #                 "qrcode_img_content": "https://liteapp.weixin.qq.com/q/...", "ret": 0}
    # 注意 qrcode 是 key 不是图片——上一版把它当图片写文件，才有了那个 24 字节的假 PNG。
    qr_key = _first(obj, ["qrcode", "qrcode_key", "key", "qrcode_id", "uuid",
                          "qrcode_token"], "")
    qr_val = _first(obj, ["qrcode_img_content", "qrcode_url", "qrcode_base64",
                          "img_content", "qr_code", "url"], "")
    if not qr_val:
        # 别名全对不上时，按"内容长什么样"在整个返回里找
        hits = _scan_for_qr(obj)
        if hits:
            where, qr_val = hits[0]
            say("没找到二维码字段，改用 %s 里的值。" % where)
    say(_save_qrcode(qr_val, out_dir))
    if not qr_key:
        say("警告：没找到二维码 key（试过 qrcode/qrcode_key/key/qrcode_id/uuid/qrcode_token），"
            "下面轮询状态很可能一直是空的。")

    deadline = time.time() + timeout
    started = time.time()
    last = None
    dumped_status = False
    warned_empty = False
    hinted_hang = False
    while time.time() < deadline:
        # 参数名没有文档，几个写法一起发，服务端认哪个算哪个
        try:
            st = api_get(base_url, "ilink/bot/get_qrcode_status",
                         {"qrcode": qr_key, "qrcode_key": qr_key, "key": qr_key,
                          "bot_type": bot_type},
                         timeout=status_timeout, check_err=False)
        except ILinkTimeout:
            # key 传对之后这个接口会挂住等扫码，所以超时就是"还没动静"，接着问下一轮。
            # 真机实测：key 不对时它立刻返回 {"ret": 0}，对了才挂住——超时反而是好兆头。
            if not hinted_hang:
                say("状态接口挂住等你扫码了（这是正常的，扫完会立刻返回）。")
                hinted_hang = True
            continue
        time.sleep(poll_interval)
        status = str(_first(st, ["status", "state", "qrcode_status",
                                 "scan_status", "qrcode_state"], "")).lower()
        if not status and not dumped_status:
            # 只报键名，不落盘：确认后的返回里带 bot_token
            say("get_qrcode_status 里没有 status 字段，返回键：%s" % sorted(st.keys()))
            dumped_status = True
        if not status and not warned_empty and time.time() - started > 30:
            warned_empty = True
            say("提示：状态已经空了 30 秒。如果你已经扫码并确认过了，那就是 key 的参数名不对——"
                "看 ilink_login_raw.json 里 qrcode 那个字段，对照 get_qrcode_status 的传参改。")
        if status != last:
            say("状态：%s" % (status or "（空）"))
            last = status
        if status in ("scaned_but_redirect", "redirect"):
            host = _first(st, ["redirect_host", "host", "baseurl", "base_url"], "")
            if host:
                base_url = host if host.startswith("http") else "https://" + host
                base_url = base_url.rstrip("/")
                say("被分流到 %s，切过去继续等确认。" % base_url)
            continue
        if status in ("confirmed", "success", "ok"):
            token = _first(st, ["bot_token", "token", "access_token"], "")
            account = _first(st, ["ilink_bot_id", "account_id", "bot_id"], "")
            host = _first(st, ["baseurl", "base_url", "host"], "")
            if host:
                base_url = (host if host.startswith("http") else "https://" + host).rstrip("/")
            if not token:
                raise ILinkError("状态是 confirmed 但没拿到 bot_token，返回键：%s"
                                 % sorted(st.keys()))
            return {"bot_token": token, "account_id": str(account or ""), "base_url": base_url}
        if status in ("expired", "cancel", "canceled", "cancelled"):
            raise ILinkError("二维码已失效或被取消，重新执行登录。")
    raise ILinkError("等了 %d 秒还没确认，放弃。" % timeout)

