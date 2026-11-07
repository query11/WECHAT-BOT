# -*- coding: utf-8 -*-
"""发文件：自己写的 AES、能不能发的判断、CDN 上传整条链路，以及走到桥上。

从 selftest.py 拆出来的（2026-09-02），用例和注释原样搬，一行没改。
"""

import base64
import os

import aes128
import ilink_client
import wechat_bridge

from tests.selftest_base import (ACCOUNT, BASE, USER, FakeILink, check, drain,
                           make_bridge, texts, wecom_msg)


# ---------- 发文件：AES ----------
#
# 自己写的 AES 是这一块里最容易"看着对其实错"的部分（错一位密文全废，而且假服务端
# 不会抗议），所以判据只认官方向量，不认"我读着觉得对"。

def test_aes():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = aes128.encrypt_block(pt, aes128.expand_key(key))
    check("AES-128 对上 FIPS-197 附录 C.1 向量",
          ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex())
    key2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    pt2 = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
    ct2 = aes128.encrypt_block(pt2, aes128.expand_key(key2))
    check("AES-128 对上 FIPS-197 附录 B 向量",
          ct2.hex() == "3925841d02dc09fbdc118597196a0b32", ct2.hex())
    check("S 盒是算出来的且几个已知值对得上",
          (aes128.SBOX[0x00], aes128.SBOX[0x01], aes128.SBOX[0x53],
           aes128.SBOX[0xff]) == (0x63, 0x7c, 0xed, 0x16),
          [hex(aes128.SBOX[i]) for i in (0x00, 0x01, 0x53, 0xff)])

    bad = ""
    try:
        aes128.expand_key(b"short")
    except ValueError as exc:
        bad = str(exc)
    check("密钥长度不对会报错", "16" in bad, bad)

    check("PKCS7 在长度刚好整块时也补满一整块",
          len(aes128.pkcs7_pad(b"a" * 16)) == 32 and aes128.pkcs7_pad(b"")[-1] == 16,
          len(aes128.pkcs7_pad(b"a" * 16)))
    sizes = [0, 1, 15, 16, 17, 1000, 4096]
    check("padded_size 和真实密文长度处处一致",
          all(len(aes128.encrypt_ecb_pkcs7(key, b"x" * n)) == aes128.padded_size(n)
              for n in sizes), sizes)
    check("密文永远比明文长 1 到 16 字节",
          all(1 <= aes128.padded_size(n) - n <= 16 for n in sizes))
    # ECB 的性质：同样的明文块给同样的密文块。这不是 bug，是微信那套本来就这么做的
    blocks = aes128.encrypt_ecb_pkcs7(key, b"A" * 32)
    check("ECB：相同明文块给相同密文块", blocks[:16] == blocks[16:32])

    # ---- 解密方向（2026-09-26 加的：收你发来的文件要的就是这一半）----
    #
    # 这里最该防的是「加密和解密一起错、往返反而自洽」——所以 FIPS 官方向量是主证据，
    # 「加密再解密原样回来」只是兜底。另外有个真踩过的坑：T 表版解密的中间九轮
    # 轮密钥必须先过一遍 InvMixColumns（expand_key_dec），拿 expand_key 的结果
    # 去解就是一片乱码，而往返测试会一起红（因为两边都错的时候也"自洽"）。
    check("解密对上 FIPS-197 附录 C.1（密文解回明文）",
          aes128.decrypt_block(bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a"),
                               aes128.expand_key_dec(key)).hex()
          == "00112233445566778899aabbccddeeff", "不对")
    check("解密对上 FIPS-197 附录 B",
          aes128.decrypt_block(bytes.fromhex("3925841d02dc09fbdc118597196a0b32"),
                               aes128.expand_key_dec(key2)).hex()
          == "3243f6a8885a308d313198a2e0370734", "不对")
    check("拿 expand_key 的结果去解密必须解错（这条就是钉死那个坑）",
          aes128.decrypt_block(bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a"),
                               aes128.expand_key(key)).hex()
          != "00112233445566778899aabbccddeeff", "居然蒙对了？")
    check("逆 S 盒是 S 盒反查来的，几个已知值对得上",
          (aes128.ISBOX[0x63], aes128.ISBOX[0x7c], aes128.ISBOX[0x16])
          == (0x00, 0x01, 0xff),
          [hex(aes128.ISBOX[i]) for i in (0x63, 0x7c, 0x16)])

    # 各种长度都要能原样回来（0 字节、整块、跨块、非整块）
    for n in (0, 1, 15, 16, 17, 1000, 4096):
        raw = os.urandom(n)
        check("加解密原样回来（%d 字节）" % n,
              aes128.decrypt_ecb_pkcs7(key, aes128.encrypt_ecb_pkcs7(key, raw)) == raw)
    check("解回来的和原来一样（跨块也对）",
          aes128.decrypt_ecb_pkcs7(key, blocks) == b"A" * 32, blocks.hex())

    # 填充 / 长度坏了必须炸 —— 绝不能把解错的字节当正文写进文件
    bad_pad = ""
    try:
        aes128.decrypt_ecb_pkcs7(key, b"\x00" * 16)
    except ValueError as exc:
        bad_pad = str(exc)
    check("填充不合法会抛错（不静悄悄给半份文件）", "PKCS7" in bad_pad, bad_pad)
    bad_len = ""
    try:
        aes128.decrypt_ecb_pkcs7(key, b"\x00" * 15)
    except ValueError as exc:
        bad_len = str(exc)
    check("密文长度不是 16 的倍数会抛错", "16" in bad_len, bad_len)

    # 流式：一块一块喂（还夹着空块和错位碎片），结果必须和整份解一样
    raw = os.urandom(1000)
    ct = aes128.encrypt_ecb_pkcs7(key, raw)
    check("流式解密和整份解密结果一致（按 7 字节一块喂）",
          b"".join(aes128.decrypt_stream(
              key, [ct[i:i + 7] for i in range(0, len(ct), 7)])) == raw)
    check("流式解密兜得住空块",
          b"".join(aes128.decrypt_stream(key, [ct[:5], b"", ct[5:]])) == raw)
    half = ""
    try:
        b"".join(aes128.decrypt_stream(key, [b"\x00" * 20]))
    except ValueError as exc:
        half = str(exc)
    check("流式解密：最后剩半块时报错（多半是没下全）", "没下全" in half, half)


# ---------- 发文件：解析与边界 ----------

def test_send_file_parse():
    B = wechat_bridge

    def hit(s):
        t = B.parse_send_file(s)
        return t if (t and B.looks_like_path(t)) else None

    yes = {
        "发给我 D:\\x\\a.pptx": "D:\\x\\a.pptx",
        "把 D:/x/a.pptx 发给我": "D:/x/a.pptx",
        "/file D:\\a.txt": "D:\\a.txt",
        "发文件 D:\\a.txt": "D:\\a.txt",
        "帮我把那个 D:\\报告\\汇报.pptx 传给我，谢谢": "D:\\报告\\汇报.pptx",
        "D:\\a.txt 发过来吧": "D:\\a.txt",
        "请把 D:\\a b\\c.docx 发到微信": "D:\\a b\\c.docx",
        "发我 /tmp/a.txt": "/tmp/a.txt",
    }
    for s, want in yes.items():
        check("认得出「%s」" % s, hit(s) == want, hit(s))

    # 这些都必须落回 Claude：或者是含糊的说法（它去找），或者根本不是要文件
    for s in ("发给我桌面上那个汇报的ppt", "把汇报.pptx 发给我", "看一下 D:\\x 里有什么",
              "打开波点音乐", "结束对话", "发给我", "先发给我 D:\\a.txt 然后打开它",
              "这个文件发出来了", "发文件的功能怎么用"):
        check("不误认「%s」" % s, hit(s) is None, hit(s))

    check("光一个文件名不算路径", not B.looks_like_path("汇报.pptx"))
    check("盘符/斜杠/~ 才算路径",
          all(B.looks_like_path(x) for x in ("D:\\a", "/tmp/a", "~/a", "\\\\nas\\a")))
    check("太长的句子不当发文件看", B.parse_send_file("发给我 " + "啊" * 300) is None)

    text, paths = B.strip_file_marks(
        "做完了\n[[发文件]]D:\\a.pptx\n[[发文件]] 「D:\\b.docx」 \n就这样")
    check("摘掉标记行", text == "做完了\n就这样", text)
    check("标记里的路径都拿到了（引号也剥掉）",
          paths == ["D:\\a.pptx", "D:\\b.docx"], paths)
    check("标记条数有上限",
          B.strip_file_marks("[[发文件]]a\n[[发文件]]b\n[[发文件]]c", 2)[1] == ["a", "b"])
    check("同一个路径不重复发",
          B.strip_file_marks("[[发文件]]a\n[[发文件]]a")[1] == ["a"])
    check("没有标记时正文不变", B.strip_file_marks("只有文字") == ("只有文字", []))
    check("human_size 三档",
          (B.human_size(1), B.human_size(2048), B.human_size(1153433))
          == ("1 字节", "2 KB", "1.1 MB"),
          (B.human_size(1), B.human_size(2048), B.human_size(1153433)))


def test_send_file_guards(tmpdir):
    B = wechat_bridge
    root = os.path.join(tmpdir, "sendroot")
    outside = os.path.join(tmpdir, "sendout")
    for d in (root, outside):
        os.makedirs(d, exist_ok=True)

    def put(d, name, data=b"hello"):
        p = os.path.join(d, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    good = put(root, "汇报.pptx", b"x" * 1000)
    empty = put(root, "空的.txt", b"")
    big = put(root, "太大.bin", b"x" * 3000)
    key = put(root, "server.key")
    env = put(root, ".env")
    far = put(outside, "别人的.txt")
    own = put(root, "config.json")
    roots = [root]

    check("正常文件能发", B.file_send_reason(good, roots, 20) == "")
    check("文件夹不发", "文件夹" in B.file_send_reason(root, roots, 20))
    check("不存在的文件说找不到",
          "找不到" in B.file_send_reason(os.path.join(root, "无.txt"), roots, 20))
    check("空文件不发", "空文件" in B.file_send_reason(empty, roots, 20))
    check("超过上限不发", "上限" in B.file_send_reason(big, roots, 0.001))
    check("密钥后缀不发", "密钥" in B.file_send_reason(key, roots, 20))
    check("凭据文件名不发", "密钥" in B.file_send_reason(env, roots, 20))
    check("范围外的文件不发", "范围" in B.file_send_reason(far, roots, 20))
    check("桥自己的 config.json 不发",
          "机器人" in B.file_send_reason(own, roots, 20, here=root),
          B.file_send_reason(own, roots, 20, here=root))
    check("别处的同名 config.json 不受影响",
          B.file_send_reason(own, roots, 20, here=tmpdir) == "")

    cfg = {"claude_extra_args": ["--add-dir", root, outside, "--allowedTools",
                                 "WebSearch,WebFetch", "--permission-mode",
                                 "acceptEdits"]}
    got = [os.path.normcase(p) for p in B.allowed_roots(cfg, tmpdir)]
    want = [os.path.normcase(p) for p in (tmpdir, root, outside)]
    check("--add-dir 的多个值都算可发范围，且不会把后面的参数当目录",
          got == want, got)
    check("不存在的目录不进可发范围",
          B.allowed_roots({"claude_extra_args": ["--add-dir", os.path.join(tmpdir, "nope")]},
                          tmpdir) == [tmpdir])


# ---------- 发文件：CDN 上传整条链路 ----------

def make_cdn_client(tmpdir, tag):
    c = ilink_client.ILinkClient(BASE, "tok", ACCOUNT, state_dir=tmpdir)
    c.cursor_file = os.path.join(tmpdir, "buf-%s.txt" % tag)
    c.style_file = os.path.join(tmpdir, "style-%s.txt" % tag)
    c.send_style = None
    return c


def reset_cdn(style="full", via="json"):
    FakeILink.upload_asks = []
    FakeILink.uploads = []
    FakeILink.sent = []
    FakeILink.upload_style = style
    FakeILink.upload_via = via


def test_send_file_upload(tmpdir):
    import hashlib
    data = "汇报内容一二三\n".encode("utf-8") * 400
    path = os.path.join(tmpdir, "汇报.pptx")
    with open(path, "wb") as fh:
        fh.write(data)

    reset_cdn("full", "json")
    c = make_cdn_client(tmpdir, "u1")
    name, size = c.send_file(USER, path, context_token="ctx-1")
    check("send_file 返回文件名和明文大小", (name, size) == ("汇报.pptx", len(data)),
          (name, size))

    ask = FakeILink.upload_asks[0]
    want = {"filekey", "media_type", "to_user_id", "rawsize", "rawfilemd5",
            "filesize", "no_need_thumb", "aeskey"}
    check("getuploadurl 的字段一个不少", want <= set(ask), sorted(ask))
    check("media_type 用上传口径的 3（消息里的文件是 4，两张表不一样）",
          ask["media_type"] == 3, ask["media_type"])
    check("rawsize 是明文大小", ask["rawsize"] == len(data), ask["rawsize"])
    check("rawfilemd5 是明文的 md5，不是密文的",
          ask["rawfilemd5"] == hashlib.md5(data).hexdigest(), ask["rawfilemd5"])
    check("filesize 是补齐后的密文大小",
          ask["filesize"] == aes128.padded_size(len(data)), ask["filesize"])
    check("no_need_thumb 传的是 True", ask["no_need_thumb"] is True)
    check("aeskey 是 32 个十六进制字符", len(ask["aeskey"]) == 32, ask["aeskey"])

    up = FakeILink.uploads[0]
    check("CDN 那一跳是裸密文，不是 multipart",
          up["ctype"] == "application/octet-stream", up["ctype"])
    check("上传的字节数和先报的 filesize 一致",
          len(up["data"]) == ask["filesize"], len(up["data"]))
    check("上传的密文正是用交出去的那把密钥加出来的",
          up["data"] == aes128.encrypt_ecb_pkcs7(bytes.fromhex(ask["aeskey"]), data))

    sent = FakeILink.sent[-1]
    item = (sent.get("item_list") or [{}])[0]
    fi = item.get("file_item") or {}
    media = fi.get("media") or {}
    check("消息里的 item 类型是 4（文件）", item.get("type") == 4, item.get("type"))
    check("file_name 是原文件名", fi.get("file_name") == "汇报.pptx", fi.get("file_name"))
    check("file_item.len 是明文大小而且是字符串（照官方包抄的，不是笔误）",
          fi.get("len") == str(len(data)) and isinstance(fi.get("len"), str),
          fi.get("len"))
    check("encrypt_type 是 1", media.get("encrypt_type") == 1, media.get("encrypt_type"))
    check("凭据用的是 CDN 给的 download_param",
          media.get("encrypt_query_param") == "dl-param-1",
          media.get("encrypt_query_param"))
    check("aes_key 是那 32 个十六进制字符的 base64，不是 16 字节原始密钥的 base64",
          base64.b64decode(media.get("aes_key", "")).decode() == ask["aeskey"],
          media.get("aes_key"))
    check("发文件也带上了 context_token", sent.get("context_token") == "ctx-1")

    # 服务端只给 param + cdn_base_url，凭据放在响应头里
    reset_cdn("param", "header")
    c2 = make_cdn_client(tmpdir, "u2")
    c2.send_file(USER, path)
    up2 = FakeILink.uploads[-1]
    check("只给 param 时自己拼出 CDN 地址", up2["param"] == "up-param-1", up2["param"])
    check("拼 URL 时带上了 filekey",
          up2["filekey"] == FakeILink.upload_asks[-1]["filekey"], up2["filekey"])
    check("凭据也能从响应头里取到",
          ((FakeILink.sent[-1].get("item_list") or [{}])[0].get("file_item", {})
           .get("media", {}).get("encrypt_query_param")) == "dl-param-1")

    # 键名全对不上时，按形状找 URL 那条兜底要能救回来
    reset_cdn("weird", "json")
    c3 = make_cdn_client(tmpdir, "u3")
    c3.send_file(USER, path)
    check("上传地址键名不认识也能按形状找到", len(FakeILink.uploads) == 1,
          len(FakeILink.uploads))

    # 两种失败都要报出「照日志改别名」级别的可诊断错误，而不是 KeyError
    reset_cdn("empty", "json")
    why = ""
    try:
        make_cdn_client(tmpdir, "u4").send_file(USER, path)
    except ilink_client.ILinkError as exc:
        why = str(exc)
    check("getuploadurl 没给地址时报错里带上顶层键名",
          "expire_time" in why, why)
    check("这种情况不会白传一遍文件", not FakeILink.uploads, len(FakeILink.uploads))

    reset_cdn("full", "none")
    why = ""
    try:
        make_cdn_client(tmpdir, "u5").send_file(USER, path)
    except ilink_client.ILinkError as exc:
        why = str(exc)
    check("CDN 不给 download_param 时报错里带上响应头和正文",
          "download_param" in why and "响应头" in why, why)
    check("凭据没拿到就绝不发那条消息", not FakeILink.sent, FakeILink.sent)
    reset_cdn("full", "json")


def file_items():
    """桥发出去的文件 item（拿文件名就够判断了）。"""
    out = []
    for p in FakeILink.sent:
        for it in p.get("item_list") or []:
            if it.get("file_item"):
                out.append(it["file_item"].get("file_name"))
    return out


def test_send_file_chain(tmpdir, fake_claude):
    reset_cdn("full", "json")
    p1 = os.path.join(tmpdir, "报告.txt")
    with open(p1, "wb") as fh:
        fh.write(b"report body\n" * 300)
    p2 = os.path.join(tmpdir, "第二份.txt")
    with open(p2, "wb") as fh:
        fh.write(b"second\n" * 100)

    b = make_bridge(tmpdir, fake_claude)
    b.send_roots = [tmpdir]
    check("默认就开着发文件", b.send_file_enable)

    # ① 直接给了路径：桥自己传，不花额度。和「打开X」一样仍要带前缀（或在对话模式里）
    FakeILink.uploads = []
    drain(b, [wecom_msg("发给我 " + p1, "f0")])
    check("没带前缀的发文件请求照旧被跳过", not texts() and not FakeILink.uploads, texts())
    drain(b, [wecom_msg("/cc 发给我 " + p1, "f1")])
    t = texts()
    check("带前缀就走发文件通道", any("正在传" in x for x in t), t)
    check("回复里报了文件名和大小",
          any("报告.txt" in x and "KB" in x for x in t), t)
    check("文件真的传出去了", file_items() == ["报告.txt"], file_items())
    check("走的是快速通道，没找 Claude", not any("已处理" in x for x in t), t)

    # ② 路径不存在 → 落回 Claude 去找，别急着报错
    FakeILink.uploads = []
    drain(b, [wecom_msg("/cc 发给我 " + os.path.join(tmpdir, "没有这个.txt"), "f2")])
    t = texts()
    check("路径不存在时交给 Claude", any("已处理" in x for x in t), t)
    check("这种情况不会传任何文件", not FakeILink.uploads)

    # ③ Claude 在回复里写 [[发文件]]<路径>：标记行摘掉、文件照发
    FakeILink.uploads = []
    drain(b, [wecom_msg("/cc WANTFILE:" + p2, "f3")])
    t = texts()
    check("标记行不出现在回复里", not any("[[发文件]]" in x for x in t), t)
    check("标记外的正文还在", any("给你" in x for x in t), t)
    check("Claude 指的文件被发出去了", file_items() == ["第二份.txt"], file_items())

    # ④ 越界的文件：拒绝并说清怎么办
    outside = os.path.join(tmpdir, "chain-out")
    os.makedirs(outside, exist_ok=True)
    far = os.path.join(outside, "别人的.txt")
    with open(far, "wb") as fh:
        fh.write(b"not yours")
    b.send_roots = [os.path.join(tmpdir, "chain-in")]
    os.makedirs(b.send_roots[0], exist_ok=True)
    FakeILink.uploads = []
    drain(b, [wecom_msg("/cc 发给我 " + far, "f4")])
    t = texts()
    check("范围外的文件被拒", any("范围" in x for x in t), t)
    check("被拒时什么都没传", not FakeILink.uploads)

    # ⑤ 功能关掉时：两条路都要说清楚，而且绝不偷偷发
    b2 = make_bridge(tmpdir, fake_claude)
    b2.send_roots = [tmpdir]
    b2.send_file_enable = False
    FakeILink.uploads = []
    drain(b2, [wecom_msg("/cc 发给我 " + p1, "f5")])
    t = texts()
    check("关掉时直说功能关着并给出路径",
          any("关着" in x and p1 in x for x in t), t)
    drain(b2, [wecom_msg("/cc WANTFILE:" + p2, "f6")])
    t = texts()
    check("关掉时 Claude 的标记也只摘不发",
          any("关着" in x for x in t) and not any("[[发文件]]" in x for x in t), t)
    check("关掉时一个文件都没传", not FakeILink.uploads)
