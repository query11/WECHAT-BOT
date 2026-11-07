# -*- coding: utf-8 -*-
"""扫码登录：二维码落盘时的几种输入，以及 scaned_but_redirect 那个状态机。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。
"""

import base64
import os

import ilink_client

from tests.selftest_base import ACCOUNT, BASE, QR_KEY, FakeILink, check


def test_qrcode_save(tmpdir):
    """真机第一次跑就是在这栽的：一串 32 字符的 id 被 b64decode 成 24 字节写成了 PNG。"""
    d = os.path.join(tmpdir, "qr")
    os.makedirs(d, exist_ok=True)
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

    msg = ilink_client._save_qrcode(base64.b64encode(png).decode(), d)
    check("base64 真图片被写成文件", os.path.exists(os.path.join(d, "ilink_qrcode.png")), msg)

    msg = ilink_client._save_qrcode("https://example.invalid/qr", d)
    check("链接被画成二维码或给出备选办法", "example.invalid" in msg, msg)

    for name in os.listdir(d):
        os.remove(os.path.join(d, name))
    junk = "abcdefghijklmnopqrstuvwxyz012345"          # 32 字符，b64 能解，但不是图片
    msg = ilink_client._save_qrcode(junk, d)
    check("非图片的 base64 不再写出假 PNG", not os.listdir(d), os.listdir(d))
    check("非图片时提示里带上原文线索", "不是任何常见图片格式" in msg, msg)

    msg = ilink_client._save_qrcode("这不是 base64 也不是链接", d)
    check("既非链接也非 base64 时不写文件", not os.listdir(d) and "原文" in msg, msg)
    check("空值有提示", "没返回" in ilink_client._save_qrcode("", d))

    nested = {"errcode": 0, "data": {"login": {"img": base64.b64encode(png).decode()}}}
    hits = ilink_client._scan_for_qr(nested)
    check("能在嵌套结构里按内容找到二维码", hits and hits[0][0] == "data.login.img", hits)
    check("不把随便一串字母当二维码",
          not ilink_client._scan_for_qr({"qrcode_key": junk}), junk)


def test_qr_login(tmpdir):
    FakeILink.qr_polls[0] = 0
    FakeILink.qr_status_params = []
    said = []
    got = ilink_client.qr_login(base_url=BASE, out_dir=tmpdir, poll_interval=0.05,
                                timeout=15, printer=said.append, status_timeout=0.3)
    check("扫码登录拿到 bot_token", got["bot_token"] == "tok-abc", got)
    check("扫码登录拿到 account_id", got["account_id"] == ACCOUNT, got)
    check("登录处理了 scaned_but_redirect 分流", got["base_url"] == BASE, got)
    raw = os.path.join(tmpdir, "ilink_login_raw.json")
    check("原始返回落盘供排查", os.path.exists(raw), raw)
    check("qrcode 字段被当成 key 传给状态接口",
          all((p.get("qrcode") or [""])[0] == QR_KEY for p in FakeILink.qr_status_params),
          FakeILink.qr_status_params[:1])
    check("没把 key 误当成二维码图片写文件",
          not os.path.exists(os.path.join(tmpdir, "ilink_qrcode.jpg")))
    joined = "\n".join(said)
    check("图链接从 qrcode_img_content 取到", "liteapp.weixin.qq.com" in joined, joined[:200])
    check("没有 key 时会明确警告", "警告" not in joined, joined[:200])
    check("状态接口挂住不算失败，超时后继续等", "挂住" in joined, joined[:300])
