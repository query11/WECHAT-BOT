# -*- coding: utf-8 -*-
"""收你发来的图片和文件：下载 → 解密 → 落盘整条链路。

和 selftest_inbox.py（那一组盯的是**形状**和探针）分开：这一组盯的是收到之后
真把东西落到盘上这件事。

字段名全部照着 2026-09-26 真机上抓到的形状摆（见 inbox_probe.py 和
`ilink_client.media_download_info` 上面的注释）：
  image_item.media.full_url            完整下载地址
  image_item.media.aes_key             那 32 个 hex 字符的 base64
  image_item.aeskey                    32 个 hex 字符（密钥本体）
  file_item.file_name / len（字符串）/ md5（明文的）

**全程不联网**：假 CDN 就是自测里那个假服务端，`do_GET .../download` 直接吐
用例塞进去的密文。真文件一个字节都不会下。
"""

import base64
import hashlib
import json
import os
import time
import tempfile

import aes128
import bridge_inbox
import ilink_client
import wechat_bridge

from tests.selftest_base import (BASE, USER, FakeILink, check, make_bridge,
                           make_fake_claude, texts, wecom_msg)

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
KEY_HEX = KEY.hex()
# 明文故意带中文和换行：解错了、少一块、编码错了都跑不掉
PLAIN_FILE = "姓名,分数\n张三,90\n李四,88\n".encode("utf-8")
PLAIN_IMG = b"\xff\xd8\xff\xe0" + b"\x00JPEGBODY" * 64
DOWNLOAD_URL = BASE + "/cdn/download?encrypted_query_param=eqp-1"


# ---------- 造假 item：形状照真机抄 ----------

def _media(aeskey_hex=KEY_HEX):
    return {"encrypt_query_param": "x" * 40,
            "aes_key": base64.b64encode(aeskey_hex.encode()).decode(),
            "full_url": DOWNLOAD_URL}


def image_item(plain=PLAIN_IMG, aeskey_hex=KEY_HEX, url=DOWNLOAD_URL, **extra):
    body = {"aeskey": aeskey_hex, "media": _media(aeskey_hex),
            "hd_size": len(plain), "thumb_size": 10}
    body["media"]["full_url"] = url
    body.update(extra)
    return {"type": 2, "create_time_ms": 1790417563710, "is_completed": True,
            "msg_id": "v1:1", "button_item_list": [],
            "at_bot_username_list": [], "image_item": body}


def file_item(plain=PLAIN_FILE, name="表.xlsx", aeskey_hex=KEY_HEX,
              url=DOWNLOAD_URL, with_md5=True, with_len=True, **extra):
    body = {"media": _media(aeskey_hex), "file_name": name}
    body["media"]["full_url"] = url
    if with_md5:
        body["md5"] = hashlib.md5(plain).hexdigest()
    if with_len:
        body["len"] = str(len(plain))              # 真机上就是字符串
    body.update(extra)
    return {"type": 4, "create_time_ms": 1790417587263, "is_completed": True,
            "msg_id": "v1:2", "button_item_list": [],
            "at_bot_username_list": [], "file_item": body}


def _serve(plain, key=KEY):
    """让假 CDN 吐「plain 用 key 加过密」的那段密文。"""
    FakeILink.download_body = aes128.encrypt_ecb_pkcs7(key, plain)
    FakeILink.download_code = 200


def _bridge(tmpdir, fake_claude, max_mb=20):
    """一个开了收件功能的桥：落盘目录和登记表都指到**自己独占的小目录**里。

    每条用例一个小目录——不然同组用例会看见彼此落的文件，数「盘上有几个」就对
    不上了（顺带也保证不碰真目录）。
    """
    sub = tempfile.mkdtemp(dir=tmpdir, prefix="case-")
    b = make_bridge(tmpdir, fake_claude)
    b.inbox_enable = True
    b.inbox_dir = os.path.join(sub, "收件")
    b.inbox_max_bytes = int(max_mb * 1024 * 1024)
    b.inbox_store = bridge_inbox.InboxStore(os.path.join(sub, "inbox.json"))
    b._ensure_inbox()
    return b


def _wait_sent(n, seconds=10.0):
    """等收件线程那条回复出现（它不在 bridge.queue 上，所以不能只靠 drain）。"""
    deadline = time.time() + seconds
    while time.time() < deadline and len(FakeILink.sent) < n:
        time.sleep(0.05)
    return texts()


def _drain(bridge, msgs):
    FakeILink.sent = []
    FakeILink.wrapped = []
    FakeILink.typing = []
    FakeILink.downloads = []
    FakeILink.inbox = list(msgs)
    bridge.poll_once()


def _files_in(path):
    if not os.path.isdir(path):
        return []
    return sorted(os.listdir(path))


# ---------- ① 抠字段 ----------

def test_download_info_parse():
    info = ilink_client.media_download_info(image_item())
    check("图片能抠出下载信息", info is not None and info["kind"] == "图片", info)
    check("地址就是 full_url", info["url"] == DOWNLOAD_URL, info)
    check("aeskey 原样拿着（32 个 hex 字符）", info["aeskey"] == KEY_HEX, info)
    check("aes_key 也留着（base64 那份）", info["aes_key_b64"] != "", info)

    f = ilink_client.media_download_info(file_item())
    check("文件也认，标签是文件", f["kind"] == "文件", f)
    check("文件名抠出来了", f["name"] == "表.xlsx", f)
    check("len 是字符串也能变成数", f["size"] == len(PLAIN_FILE), f)
    check("md5 拿到了（小写）", f["md5"] == hashlib.md5(PLAIN_FILE).hexdigest(), f)

    # 图片没有 len，只有 hd_size 那种提示值：能当大小用，但不能当精确校验
    img_big = image_item()
    img_big["image_item"]["hd_size"] = 267915
    info2 = ilink_client.media_download_info(img_big)
    check("图片的大小取 hd_size（日志里不再写「大小未知」）",
          info2["size"] == 267915, info2)
    check("图片的 exact 是 False（hd_size 只当提示，不当精确校验）",
          info2["exact"] is False, info2)
    check("文件的 exact 是 True（file_item.len 是明文字节数）", f["exact"] is True, f)

    check("文字消息不是附件", ilink_client.media_download_info(
        {"type": 1, "text_item": {"text": "嗨"}}) is None)
    check("没有 media.full_url 就当拿不到（不自己拼地址）",
          ilink_client.media_download_info(
              {"type": 2, "image_item": {"media": {"encrypt_query_param": "x"}}}) is None)
    check("full_url 不是 http 开头也不算",
          ilink_client.media_download_info(
              {"type": 2, "image_item": {"media": {"full_url": "/download?x=1"}}}) is None)
    check("没有 media 那一层时别炸",
          ilink_client.media_download_info(
              {"type": 2, "image_item": {"aeskey": KEY_HEX}}) is None)
    check("给个不是字典的也不炸", ilink_client.media_download_info("x") is None)
    check("视频也认（标签是视频）",
          ilink_client.media_download_info(
              {"type": 3, "video_item": {"media": {"full_url": DOWNLOAD_URL}}})["kind"]
          == "视频")


def test_media_key():
    check("aeskey 直接就是 16 字节密钥",
          ilink_client.media_key_bytes({"aeskey": KEY_HEX}) == KEY)
    b64 = base64.b64encode(("ff" * 16).encode()).decode()
    check("没有 aeskey 时从 aes_key 的 base64 里解（解出来是 32 个 hex 字符）",
          ilink_client.media_key_bytes({"aes_key_b64": b64}) == bytes.fromhex("ff" * 16))
    bad = ""
    try:
        ilink_client.media_key_bytes({"aeskey": "短", "aes_key_b64": "也不是"})
    except ilink_client.ILinkError as exc:
        bad = str(exc)
    check("两个都不成样子时报错（不许瞎解）", "aeskey" in bad, bad)
    check("aeskey 是 32 个字符但不是 hex 时也不认",
          _raises(lambda: ilink_client.media_key_bytes({"aeskey": "z" * 32})))


def _raises(fn):
    try:
        fn()
        return False
    except ilink_client.ILinkError:
        return True


# ---------- ② 文件名清洗 ----------

def test_safe_name():
    check("普通名字原样留着", bridge_inbox.safe_inbox_name("表.xlsx") == "表.xlsx")
    check("路径被剥成 basename",
          bridge_inbox.safe_inbox_name("C:\\Windows\\evil.exe") == "evil.exe")
    check("正斜杠也剥", bridge_inbox.safe_inbox_name("a/b/c.txt") == "c.txt")
    check("相对路径上前缀也剥（不许爬出收件夹）",
          bridge_inbox.safe_inbox_name("..\\..\\x.txt") == "x.txt")
    check("保留字符换掉",
          bridge_inbox.safe_inbox_name('a:b*c?d"e<f>g|h.txt') == "a_b_c_d_e_f_g_h.txt")
    check("保留名（CON）不硬用", bridge_inbox.safe_inbox_name("CON") != "CON")
    check("结尾的点和空格砍掉", bridge_inbox.safe_inbox_name("a.txt. ") == "a.txt")
    check("太长的截断", len(bridge_inbox.safe_inbox_name("长" * 200 + ".txt")) <= 65)
    check("没有名字时按类型编一个（图片给 .jpg）",
          bridge_inbox.safe_inbox_name("", "图片", ".jpg").endswith(".jpg"))
    check("没有名字、也没有默认扩展名时给 .bin",
          bridge_inbox.safe_inbox_name("", "附件").endswith(".bin"))
    check("空名字不产生空文件名", bridge_inbox.safe_inbox_name("") != "")


# ---------- ③ 登记表 ----------

def test_store(tmpdir):
    path = os.path.join(tempfile.mkdtemp(dir=tmpdir, prefix="store-"), "inbox.json")
    st = bridge_inbox.InboxStore(path)
    check("一开始是空的", st.all() == [] and st.total_bytes() == 0, st.all())
    st.add({"name": "a.txt", "bytes": 10, "at": 1.0, "uid": "u", "kind": "文件"})
    st.add({"name": "b.txt", "bytes": 20, "at": 2.0, "uid": "u", "kind": "文件"})
    check("加进去了", len(st.all()) == 2, st.all())
    check("总量算得对", st.total_bytes() == 30, st.total_bytes())

    again = bridge_inbox.InboxStore(path)
    check("重启后还认得（落盘了）",
          [it["name"] for it in again.all()] == ["a.txt", "b.txt"], again.all())

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{这不是 JSON")
    check("登记表坏了不炸（只当空）", bridge_inbox.InboxStore(path).all() == [], "炸了")

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"items": [{"name": ""}, "不是字典", {"name": "c.txt"}]}, fh)
    check("坏条目跳过、好条目留着",
          [it["name"] for it in bridge_inbox.InboxStore(path).all()] == ["c.txt"])


# ---------- ④ 整条链路（假 CDN） ----------

def test_receive_image(tmpdir, fake_claude):
    _serve(PLAIN_IMG)
    b = _bridge(tmpdir, fake_claude)
    _drain(b, [wecom_msg("", "m1", items=[image_item()])])
    sent = _wait_sent(1)
    check("回话说收下了", any("📎 收下了 1 个附件" in t for t in sent), sent)
    check("回话里写了落盘目录", any(b.inbox_dir in t for t in sent), sent)
    check("回话里告诉了名字", any("图片 → " in t for t in sent), sent)

    got = _files_in(b.inbox_dir)
    check("盘上真有一个文件", len(got) == 1, got)
    check("文件名带时间戳前缀、扩展名是 .jpg",
          got and got[0].endswith(".jpg") and got[0][:8].isdigit(), got)
    with open(os.path.join(b.inbox_dir, got[0]), "rb") as fh:
        check("解出来的字节和原图一模一样", fh.read() == PLAIN_IMG)
    check("登记表里记了一条",
          [it["name"] for it in b.inbox_store.all()] == got, b.inbox_store.all())
    check("登记表里记了 md5 和大小",
          b.inbox_store.all()[0]["bytes"] == len(PLAIN_IMG), b.inbox_store.all())
    check("没有 .part 半截文件留下",
          [f for f in got if f.endswith(".part")] == [], got)


def test_receive_file_with_check(tmpdir, fake_claude):
    _serve(PLAIN_FILE)
    b = _bridge(tmpdir, fake_claude)
    _drain(b, [wecom_msg("", "m2", items=[file_item()])])
    sent = _wait_sent(1)
    check("文件也收下了", any("📎 收下了 1 个附件" in t for t in sent), sent)
    check("回话里点了原名（好认）", any("表.xlsx" in t for t in sent), sent)

    got = _files_in(b.inbox_dir)
    check("文件名里保留了原名", got and got[0].endswith("-表.xlsx"), got)
    with open(os.path.join(b.inbox_dir, got[0]), "rb") as fh:
        check("内容对得上（含中文）", fh.read() == PLAIN_FILE)



def test_receive_image_size_hint(tmpdir, fake_claude):
    """图片的 hd_size 只是提示：对不上**不许误拒**，超上限要**事前拦住**。

    真机实测 hd_size == 我们解出来的字节数，但那是「服务端给的是 hd 那一版」的情况；
    万一哪天给的是 mid 版，硬卡长度就会把好图丢掉。所以这里两个方向都钉住。
    """
    _serve(PLAIN_IMG)
    b = _bridge(tmpdir, fake_claude)
    wrong = image_item()
    wrong["image_item"]["hd_size"] = len(PLAIN_IMG) + 12345     # 故意报错
    _drain(b, [wecom_msg("", "h1", items=[wrong])])
    sent = _wait_sent(1)
    check("图片 hd_size 对不上也照样收下（不许误拒）",
          any("收下了 1 个附件" in t for t in sent), sent)
    check("真落了盘", len(_files_in(b.inbox_dir)) == 1, _files_in(b.inbox_dir))

    # 反过来：服务端报的 hd_size 超过单文件上限 → 一开始就不下
    b2 = _bridge(tmpdir, fake_claude, max_mb=0.001)          # 约 1 KB
    too_big = image_item()
    too_big["image_item"]["hd_size"] = 5000000
    _drain(b2, [wecom_msg("", "h2", items=[too_big])])
    sent2 = _wait_sent(1)
    check("图片报的大小超上限 → 事前拦住，报得太大了",
          any("太大了" in t for t in sent2), sent2)
    check("拦住了就一个字节都不下", FakeILink.downloads == [], FakeILink.downloads)


def test_receive_rejects_bad(tmpdir, fake_claude):
    # md5 对不上 → 宁可不留
    _serve(PLAIN_FILE)
    b = _bridge(tmpdir, fake_claude)
    _drain(b, [wecom_msg("", "m3", items=[file_item(with_md5=False)])])
    check("没给 md5 就照收", any("📎 收下了 1 个附件" in t for t in _wait_sent(1)))

    _drain(b, [wecom_msg("", "m4", items=[
        file_item(md5="0" * 32, with_len=False)])])
    sent = _wait_sent(2)
    check("md5 对不上时不收，并说清原因",
          any("md5 对不上" in t for t in sent), sent)
    check("md5 不对的那个没落盘",
          len([f for f in _files_in(b.inbox_dir) if f.endswith("-表.xlsx")]) == 1,
          _files_in(b.inbox_dir))

    # 长度对不上 → 也是「多半没下全」
    _drain(b, [wecom_msg("", "m5", items=[file_item(len=str(len(PLAIN_FILE) + 5))])])
    sent = _wait_sent(3)
    check("长度对不上时不收，并说「多半没下全」",
          any("多半没下全" in t for t in sent), sent)

    # 服务端说太大 → 一开始就不接（连下都不下）
    _drain(b, [wecom_msg("", "m6", items=[file_item(len="99999999")])])
    sent = _wait_sent(4)
    check("服务端报的长度超过上限时直接不收并报大小",
          any("太大了" in t for t in sent), sent)


def test_receive_over_limit(tmpdir, fake_claude):
    """两条上限都要拦得住：服务端报的超了、以及「下了才知道超了」。"""
    _serve(PLAIN_FILE)
    b = _bridge(tmpdir, fake_claude, max_mb=0.001)     # 约 1 KB
    _drain(b, [wecom_msg("", "m7", items=[file_item(len="5000")])])
    sent = _wait_sent(1)
    check("服务端说的长度超过上限 → 不收", any("太大了" in t for t in sent), sent)
    check("没收就没文件", _files_in(b.inbox_dir) == [], _files_in(b.inbox_dir))
    check("登记表里也没记", b.inbox_store.all() == [], b.inbox_store.all())

    # 服务端没报长度（或报得不准）：写到一半发现超了也要停下来
    big = b"x" * 4096
    _serve(big)
    _drain(b, [wecom_msg("", "m7b", items=[
        file_item(plain=big, with_len=False, with_md5=False)])])
    sent = _wait_sent(2)
    check("边下边超上限时也停下并说清", any("超过上限" in t for t in sent), sent)
    check("半截文件被清掉了", _files_in(b.inbox_dir) == [], _files_in(b.inbox_dir))


def test_receive_http_error(tmpdir, fake_claude):
    _serve(PLAIN_FILE)
    FakeILink.download_code = 500
    try:
        b = _bridge(tmpdir, fake_claude)
        _drain(b, [wecom_msg("", "m8", items=[file_item()])])
        sent = _wait_sent(1)
        check("下载失败要如实说（带 HTTP 码）",
              any("没收下" in t and "500" in t for t in sent), sent)
        check("失败时不留半截文件",
              [f for f in _files_in(b.inbox_dir) if f.endswith(".part")] == [],
              _files_in(b.inbox_dir))
    finally:
        FakeILink.download_code = 200


def test_receive_no_url(tmpdir, fake_claude):
    b = _bridge(tmpdir, fake_claude)
    _drain(b, [wecom_msg("", "m9", items=[
        {"type": 6, "voice_item": {"text": ""}}])])
    sent = _wait_sent(1)
    check("形状里没有可下载地址时明说拿不到",
          any("没有可下载的地址" in t for t in sent), sent)


def test_receive_mixed_with_text(tmpdir, fake_claude):
    """「看看这张图」：要等文件落盘，Claude 拿到的正文里得有那个路径。"""
    _serve(PLAIN_IMG)
    b = _bridge(tmpdir, fake_claude)
    # 注意：wecom_msg 一旦给了 items，正文就只看 items 里那个 text_item 了，
    # 所以这里得**自己把正文也塞成一个 item**（真机上一条消息就是这样的）
    _drain(b, [wecom_msg("", "m10", items=[
        {"type": 1, "text_item": {"text": "/cc 看看刚发的图"}},
        image_item()])])
    sent = _wait_sent(2, seconds=15)
    check("第一条是落盘通知", any("📎 收下了" in t for t in sent), sent)
    check("第二条是 Claude 的回答，而且正文里带着落盘路径",
          any("已处理" in t and "收件" in t for t in sent), sent)
    check("正文里带的是**文件名**（不是目录）",
          any("已处理" in t and ".jpg" in t for t in sent), sent)


def test_receive_disabled(tmpdir, fake_claude):
    """功能关着时，行为和以前一模一样（还是那句「拿不到内容」）。"""
    b = make_bridge(tmpdir, fake_claude)          # make_bridge 默认关掉 inbox
    check("默认关着", b.inbox_enable is False and b._inbox is None, b.inbox_enable)
    _drain(b, [wecom_msg("", "m11", items=[
        {"type": 1, "text_item": {"text": "/cc 看看这张图"}},
        image_item()])])
    b.queue.join()
    time.sleep(0.2)
    sent = texts()
    check("关着时照旧说「拿不到内容」",
          any("拿不到内容" in t for t in sent), sent)
    check("关着时一个字节都没下", FakeILink.downloads == [], FakeILink.downloads)


def test_inbox_config_state(tmpdir, fake_claude):
    """配置派生：inbox 那几项由 _build_config_state 一处算出来。"""
    cfg = dict(wechat_bridge.DEFAULTS)
    cfg.update({
        "workdir": tmpdir, "claude_bin": fake_claude, "claude_extra_args": [],
        "app_launch": {"enable": False}, "media_control": {"enable": False},
        "screen_control": {"enable": False}, "power_control": {"enable": False},
        "meeting": {"enable": False}, "remind": {"enable": False},
        "inbox": {"enable": True, "dir": os.path.join(tmpdir, "收件"), "max_mb": 3}})
    b = wechat_bridge.Bridge(cfg)
    check("开关传下去了", b.inbox_enable is True)
    check("目录传下去了", b.inbox_dir == os.path.join(tmpdir, "收件"), b.inbox_dir)
    check("单文件上限按 MB 换算成字节", b.inbox_max_bytes == 3 * 1024 * 1024,
          b.inbox_max_bytes)
    check("登记表建起来了", b.inbox_store is not None)
    check("线程也起来了", b._inbox is not None)

    check("关掉时登记表是 None（附件照旧只回拿不到内容）",
          wechat_bridge.make_inbox_store({"inbox": {"enable": False}}) is None)
