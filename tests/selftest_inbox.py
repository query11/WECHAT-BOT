# -*- coding: utf-8 -*-
"""收「图片和文件」这条路的第一小步：附件形状日志 + inbox_probe。

现状：桥收到图片/文件只会回一句「这条消息还附了图片，但我拿不到内容」——因为收消息侧的
附件字段名**没有任何文档可以对照**（发送侧是照官方 npm 包逐字抄的），历史日志里连一条
样本都没有。所以在写「下载 + 解密 + 落盘」之前先做一步：把附件的**形状**（键名留着、
值只留类型和长度）记一条进 DEBUG 日志，用 inbox_probe 读出来判「里面到底有没有下载凭据」。

这一组测四件事：
  ① `item_shape` 掩得对不对：像凭据的（aes_key / url / encrypt_query_param）只留长度，
     普通短值原样保留（encrypt_type 的 1、文件名、len），超长的只留头 12 字符；
  ② 形状日志：**只有附件才记**、文字不记；日志里**一个字凭据原文都不许有**；
  ③ 老行为一步没动：`Message._parse_items` 对文字/图片的判定照旧（这步只加日志）；
  ④ `inbox_probe`：能从日志里抓形状、判「有戏/没戏」，没样本时给造样本的操作指引。

**这里从来没有真的连过网**：全部是拿手工造的 item 字典直接调纯函数。
"""

import contextlib
import io
import json
import logging
import os

import ilink_client
import inbox_probe

from tests.selftest_base import check

# 凭据的替身：26 字符，掩过之后只该出现「26 字符，头 2 字符 'ZZ'」，原文绝不该出现
SECRET = "ZZZTOPSECRETZZZ01234567890"   # 正好 26 个字符，掩过之后只剩长度和头两位
LONG = "x" * 60

IMG_ITEM = {
    "type": 2,
    "image_item": {
        "media": {
            "encrypt_query_param": SECRET,
            "aes_key": SECRET,
            "encrypt_type": 1,
        },
        "image_name": "照片.jpg",
        "len": 40960,
        "extra_long": LONG,
    },
}

FILE_ITEM = {
    "type": 5,
    "file_item": {
        "media": {"aes_key": SECRET},
        "file_name": "表.xlsx",
        "len": 12345,
    },
}

# 真机上抓到的图片 item 的样子（2026-09-26）：**键名和先后顺序照抄，值全换掉**。
# 注意 image_item 排在最后一个，一共 8 个键——这就是探针当初误报「没戏」的原因：
# 形状窗口只要 6 个键，image_item 正好被「…」吃掉。真正的下载凭据就在它里面：
#   image_item.aeskey                     32 位 hex（键名是 aeskey，不是 aes_key）
#   image_item.media.encrypt_query_param  下载时要带上的那段密文凭据
# 下面这个 AESKEY 是**编的**（真凭据不进代码库），只用来验「掩得对不对」。
AESKEY = "0123456789abcdef0123456789abcdef"
REAL_IMG_ITEM = {
    "type": 2,
    "create_time_ms": 1790394751092,
    "update_time_ms": 1790394751092,
    "is_completed": True,
    "msg_id": "v1:0000000000000000001",
    "button_item_list": [],
    "at_bot_username_list": [],
    "image_item": {
        "aeskey": AESKEY,
        "media": {"encrypt_query_param": SECRET},
    },
}


def _grab(build):
    """跑 build() 期间，「ilink」这个 logger 记下的原话。"""
    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    lg = logging.getLogger("ilink")
    cap = _Cap()
    old = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(cap)
    try:
        build()
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old)
    return lines


def _msg(items, mid="m1"):
    return {"message_id": mid, "from_user_id": "u-1", "message_type": 1,
            "item_list": items}


# ---------- ① 形状：掩得对不对 ----------

def test_shape_masks():
    sh = ilink_client.item_shape(IMG_ITEM)
    body = sh["image_item"]
    media = body["media"]

    check("aes_key 只剩长度和头 2 字符",
          media["aes_key"] == "26 字符，头 2 字符 'ZZ'", media["aes_key"])
    check("encrypt_query_param 也只剩长度",
          media["encrypt_query_param"] == "26 字符，头 2 字符 'ZZ'",
          media["encrypt_query_param"])
    check("整个形状里凭据原文一个字都不留",
          SECRET not in json.dumps(sh, ensure_ascii=False), sh)

    check("整数原样留着（encrypt_type）", media["encrypt_type"] == 1, media)
    check("普通短字符串原样留着（文件名）",
          body["image_name"] == "照片.jpg", body)
    check("数字原样留着（len）", body["len"] == 40960, body)
    check("超长的普通字符串只留头 12 字符",
          body["extra_long"] == "60 字符，头 12 字符 'xxxxxxxxxxxx'", body["extra_long"])
    check("嵌套的 media 表还在、键名都在",
          isinstance(media, dict) and set(media) == {"encrypt_query_param",
                                                     "aes_key", "encrypt_type"},
          media)
    check("item 外层的键名也都在（type / image_item）",
          set(sh) == {"type", "image_item"}, list(sh))

    check("名字像凭据的才掩：file_name 不掩",
          ilink_client.item_shape(FILE_ITEM)["file_item"]["file_name"] == "表.xlsx",
          ilink_client.item_shape(FILE_ITEM))
    check("_looks_like_secret 认得出这几类",
          all(ilink_client._looks_like_secret(k) for k in
              ("aes_key", "encrypt_query_param", "download_url", "bot_token",
               "signature", "cookie", "passwd", "ticket", "errcode")), "有漏的")
    check("_looks_like_secret 不误伤这几个",
          not any(ilink_client._looks_like_secret(k) for k in
                  ("file_name", "len", "encrypt_type", "type", "media")), "误伤了")


def test_shape_limits():
    """限长：键数、嵌套层数、数组，超过就一律「…」顶上。

    这里**显式传小的 limit**：默认值见 `ilink_client.SHAPE_KEY_LIMIT`，那个数是
    故意放大的——真机上一条图片 item 有 8 个键，窗口小了会把 `image_item` 整个吃掉。
    """
    many = {"type": 5, "a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6}
    sh = ilink_client.item_shape(many, limit=6)
    check("只留前 6 个键", len([k for k in sh if k != "…"]) == 6, list(sh))
    check("多出来的用「…」报个数", sh.get("…") == "还有 1 个键", sh)

    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    got = json.dumps(ilink_client.item_shape(deep), ensure_ascii=False)
    check("太深的那层折叠成「字典 N 个键」", "字典 1 个键" in got, got)

    lst = {"type": 2, "image_item": {"items": [1, 2, 3, 4, 5, 6, 7]}}
    arr = ilink_client.item_shape(lst, limit=6)["image_item"]["items"]
    check("长长的数组只留前 6 个", arr[:6] == [1, 2, 3, 4, 5, 6], arr)
    check("数组也多一句「还有几项」", arr[6] == "…还有 1 项", arr)

    # 真机上误报「没戏」的那一次，就是这么来的
    cut6 = ilink_client.item_shape(REAL_IMG_ITEM, limit=6)
    check("回归：窗口只要 6 个键，image_item 就被吃掉（真机上就是这么误报的）",
          "…" in cut6 and "image_item" not in cut6, cut6)
    full = ilink_client.item_shape(REAL_IMG_ITEM)
    check("默认窗口放得下真机那种 8 键 item（image_item 在最后一个也看得见）",
          "image_item" in full and "…" not in full, full)
    check("真机上那个键名是 aeskey（不是 aes_key），照样掩过",
          full["image_item"]["aeskey"] == "32 字符，头 2 字符 '01'", full["image_item"])
    check("media.encrypt_query_param 也掩过",
          full["image_item"]["media"]["encrypt_query_param"] == "26 字符，头 2 字符 'ZZ'",
          full["image_item"])

    check("空字典不炸", ilink_client.item_shape({}) == {}, "boom")
    check("不是字典的也兜得住（给 item_shape 一个字符串）",
          ilink_client.item_shape("abc") == "abc", ilink_client.item_shape("abc"))
    check("认不出的类型只留类型名（保证 json 能序列化）",
          ilink_client.item_shape({"type": 1, "blob": b"\x00\x01"})["blob"] == "<bytes>",
          ilink_client.item_shape({"type": 1, "blob": b"\x00\x01"}))


# ---------- ② 形状日志 ----------

def test_shape_log():
    lines = _grab(lambda: ilink_client.Message(_msg([IMG_ITEM])))
    check("收到图片时记了一条形状", len(lines) == 1, lines)
    line = lines[0] if lines else ""
    check("形状行带着「图片」这个标签", "收到附件（图片）的字段形状：" in line, line)
    check("形状行里有 media / aes_key 这些键名（好判凭据在哪儿）",
          "media" in line and "aes_key" in line, line)
    check("形状行里没有凭据原文", SECRET not in line, line)
    check("形状行里文件名照常留着（那不是凭据）", "照片.jpg" in line, line)
    check("形状行本身就是合法 JSON 尾巴（inbox_probe 靠这个解析）",
          inbox_probe.parse_line(line) is not None, line)

    got_file = _grab(lambda: ilink_client.Message(_msg([FILE_ITEM], "m2")))
    check("收到文件时标签是「文件」",
          len(got_file) == 1 and "收到附件（文件）的字段形状：" in got_file[0], got_file)

    got_text = _grab(lambda: ilink_client.Message(
        _msg([{"type": 1, "text_item": {"text": "你好"}}], "m3")))
    check("文字消息不记形状（只有真附件才记）", got_text == [], got_text)

    got_voice = _grab(lambda: ilink_client.Message(
        _msg([{"type": 6, "voice_item": {"text": "转写好了"}}], "m4")))
    check("语音有转写也不记形状（它当文字用，不是拿不到内容）", got_voice == [], got_voice)

    # 真机上栽过的那一次：8 个键的 item 里 image_item 排在最后，窗口小了就
    # 整条看不见，形状行会写成「还有 2 个键」——探针于是误报「没有下载凭据」。
    real = _grab(lambda: ilink_client.Message(_msg([REAL_IMG_ITEM], "m5")))
    check("回归：真机那种 8 键 item，形状行里必须看得见 image_item",
          len(real) == 1 and "image_item" in real[0], real)
    check("回归：那一行里凭据（aeskey / encrypt_query_param）原文一个字都没有",
          bool(real) and SECRET not in real[0] and AESKEY not in real[0], real)
    check("回归：那一行里也没写「还有 N 个键」（8 个键全都看得到）",
          bool(real) and "还有" not in real[0], real)


# ---------- ③ 老行为一步没动 ----------

def test_parse_items_unchanged():
    m = ilink_client.Message(_msg([IMG_ITEM], "r1"))
    check("老行为：图片还是算「附件」", m.attachments == ["图片"], m.attachments)
    check("老行为：图片消息没有正文", m.text == "", m.text)

    m2 = ilink_client.Message(_msg([{"type": 1, "text_item": {"text": "你好"}}], "r2"))
    check("老行为：文字照旧进 text", m2.text == "你好", m2.text)
    check("老行为：文字不算附件", m2.attachments == [], m2.attachments)

    m3 = ilink_client.Message(_msg([{"type": 1, "text_item": {"text": "再发你"}},
                                    IMG_ITEM], "r3"))
    check("老行为：文字 + 图片混着来，两个都认",
          m3.text == "再发你" and m3.attachments == ["图片"], (m3.text, m3.attachments))

    m4 = ilink_client.Message(_msg([{"type": 5, "file_item": {"file_name": "表.xlsx"}}], "r4"))
    check("老行为：文件也算「附件」", m4.attachments == ["文件"], m4.attachments)


# ---------- ④ inbox_probe ----------

def test_probe_reads_log(tmpdir):
    good = ("2026-09-25 11:52:03,123 INFO ilink 收到附件（文件）的字段形状："
            + json.dumps(ilink_client.item_shape(FILE_ITEM), ensure_ascii=False))
    half = ('2026-09-25 11:52:04,123 INFO ilink 收到附件（图片）的字段形状：{"type": 2, "imag')
    p = os.path.join(tmpdir, "fake_shape.log")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write("2026-09-25 11:52:00,000 INFO ilink 别的行，不是形状\n")
        f.write(good + "\n")
        f.write(half + "\n")

    got = inbox_probe.read_shapes(p, 5)
    check("从日志里抓到形状", len(got) == 1, got)
    check("标签也一起抓到了", bool(got) and got[0][0] == "文件", got)
    check("抓到的形状里凭据原文一个字都没有",
          SECRET not in json.dumps(got, ensure_ascii=False), got)
    check("被切半的那行不当样本、也不炸（所以只抓到 1 条）", len(got) == 1, got)

    check("只取最近 N 条", len(inbox_probe.read_shapes(p, 1)) == 1, "没截")
    check("want=0 时全给", len(inbox_probe.read_shapes(p, 0)) == 1, "错")
    check("文件不存在给空列表",
          inbox_probe.read_shapes(os.path.join(tmpdir, "没有这个.log"), 5) == [], "错")
    check("不是形状行返回 None",
          inbox_probe.parse_line("2026-09-25 11:52:00,000 INFO ilink 别的行") is None, "错")


def test_probe_verdict():
    ok, lines = inbox_probe.verdict([("图片", ilink_client.item_shape(IMG_ITEM))])
    joined = "\n".join(lines)
    check("有 media / 凭据键时判成「有戏」", ok is True, joined)
    check("结论里点名了那些字段", "encrypt_query_param" in joined, joined)
    check("点明了密文要补解密", "解密" in joined, joined)
    check("结论里说明了「只看字段名」", "只看字段名" in joined, joined)

    ok2, lines2 = inbox_probe.verdict([("", ilink_client.item_shape(
        {"type": 1, "text_item": {"text": "你好"}}))])
    joined2 = "\n".join(lines2)
    check("没有凭据键时判成「没戏」", ok2 is False, joined2)
    check("「没戏」时别把 text 误当文件名（ext 那个坑）",
          "文件名 / 大小：一个都没有" in joined2, joined2)
    check("「没戏」时也别把 text 当成有加密", "没有 aes / encrypt 字样" in joined2, joined2)

    ok3, lines3 = inbox_probe.verdict([])
    check("没有样本时说清楚没得看", ok3 is False, lines3)
    check("没有样本时指向「先造一条」", "一条附件形状都没有" in lines3[0], lines3)

    paths = inbox_probe.key_paths(ilink_client.item_shape(IMG_ITEM))
    check("键路径是点号连起来的", "image_item.media.aes_key" in paths, paths)
    wide = {"type": 5}
    for n in range(9):
        wide["k%d" % n] = n
    paths2 = inbox_probe.key_paths(ilink_client.item_shape(wide, limit=6))
    check("被限长省掉的部分也标出来（超过 6 个键的那层会多一条「…」）",
          any("后面还有" in p for p in paths2), paths2)

    # 真机那种形状（8 个键、image_item 在最后一个）必须判成「有戏」
    ok4, lines4 = inbox_probe.verdict([("图片", ilink_client.item_shape(REAL_IMG_ITEM))])
    joined4 = "\n".join(lines4)
    check("真机那种形状判成「有戏」", ok4 is True, joined4)
    check("点到了 encrypt_query_param / aeskey",
          "encrypt_query_param" in joined4 and "aeskey" in joined4, joined4)
    check("真机那种形状没有「看不全」的告警",
          "这次看不全" not in joined4, joined4)

    # 窗口被调小、image_item 被吃掉时，结论必须自己声明「看不全」
    ok5, lines5 = inbox_probe.verdict(
        [("图片", ilink_client.item_shape(REAL_IMG_ITEM, limit=6))])
    joined5 = "\n".join(lines5)
    check("形状被限长截断过时，不许硬下结论", "这次看不全" in joined5, joined5)
    check("截断时点明凭据可能藏在省掉的键里",
          "省掉的那几个键" in joined5, joined5)
    check("截断时也说清怎么办（把 SHAPE_KEY_LIMIT 调大）",
          "SHAPE_KEY_LIMIT" in joined5, joined5)


def test_probe_main(tmpdir):
    def run(args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = inbox_probe.main(args)
        return rc, buf.getvalue()

    rc, out = run(["--raw", json.dumps({"item_list": [IMG_ITEM]}, ensure_ascii=False)])
    check("--raw 有凭据 → 退出码 0", rc == 0, out[:300])
    check("--raw 打出了形状", "image_item" in out, out[:300])
    check("--raw 的输出里不出现凭据原文", SECRET not in out, out[:300])
    check("--raw 也给了结论", "结论：" in out, out[:300])

    rc, out = run(["--raw", json.dumps({"item_list": [{"type": 1, "text_item": {"text": "嗨"}}]},
                                       ensure_ascii=False)])
    check("--raw 没凭据 → 退出码 1", rc == 1, out[:300])

    rc, out = run(["--raw", "{这不是 JSON"])
    check("--raw 给的不是 JSON → 退出码 2 并说清楚",
          rc == 2 and "不是合法 JSON" in out, out[:200])

    rc, out = run(["--log", os.path.join(tmpdir, "没有这个.log")])
    check("日志里没样本 → 退出码 1", rc == 1, out[:200])
    check("没样本时给出造样本的 5 步",
          "toggle.ps1 -DebugLog" in out and "toggle.ps1 -Off" in out, out[:400])
    check("没样本时说明「为什么单独记一条形状」", "1500 字符" in out, out[:600])

    rc, out = run(["--nope"])
    check("不认识的参数 → 退出码 2", rc == 2 and "不认识的参数" in out, out[:200])

    rc, out = run(["--help"])
    check("--help → 退出码 0 并打用法",
          rc == 0 and "inbox_probe" in out and "怎么用" in out, out[:200])
