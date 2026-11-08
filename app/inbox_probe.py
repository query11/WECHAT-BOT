# -*- coding: utf-8 -*-
"""看「你发来的图片和文件」里到底有哪些字段——决定「收文件」这条路走不走得通。

背景：收消息侧的附件（`image_item` / `file_item`）里有没有可下载的凭据（url、
`encrypt_query_param` 之类）、有没有 `aes_key`、`file_name` / `len` 在哪儿，
**没有任何文档可以对照**（发送侧的字段名是照官方 npm 包逐字抄的，两边不一定一样），
历史日志里连一条样本都没有。所以写下载代码之前，先让桥把附件的**形状**
（键名 + 值只留类型和长度）记一条进 DEBUG 日志，再用这个脚本读出来看清楚。

怎么用（三步，大约一分钟）：
  1. 停桥：powershell -ExecutionPolicy Bypass -File desktop\toggle.ps1 -Off
  2. DEBUG 起：powershell -ExecutionPolicy Bypass -File desktop\toggle.ps1 -DebugLog
  3. 手机上给这个 bot 发**一张图片**、再发**一个文件**，然后
     python app/inbox_probe.py            ← 它读 bridge.log，把形状摆出来并下结论
     （验完记得再 toggle.ps1 -Off：DEBUG 会把**收发的原始 JSON 整条**写进日志）

别的用法：
  python app/inbox_probe.py --raw '<一条消息或 item 的 JSON>'   现算形状，不用等真消息
  python app/inbox_probe.py -n 5                                看最近 5 条（默认 3）
  python app/inbox_probe.py --log 别的.log                      换个日志文件

**这份输出不含凭据**：名字像 key/token/url/param… 的字段只留长度和头 2 个字符
（见 `ilink_client.item_shape`），所以贴出来也安全。
"""

import json
import os
import sys

import bridge_config

HERE = bridge_config.HERE   # project root, where bridge.log is written
DEFAULT_LOG = os.path.join(HERE, "bridge.log")

# 这句是 ilink_client._parse_items 往日志里写形状时用的标记，改那边要一起改
MARK = "的字段形状："
MARK_PREFIX = "收到附件（"
# 日志尾巴最多读这么多字节（形状行很短，2 MB 足够翻到）
TAIL_BYTES = 2 * 1024 * 1024

# 判定用的三组线索。**只看键名**（值已经被掩过了），命中即认为「有这类字段」。
DOWNLOAD_HINTS = ("url", "param", "download", "link", "filekey", "file_key",
                  "cdn", "media_id", "mediaid", "token", "path")
CRYPTO_HINTS = ("aes", "encrypt", "decrypt", "cipher")
# 文件名 / 大小这类。**故意不含 "ext"**：`text_item.text` 里就带着 "ext"，会误报成
# 「有文件名」；宁可漏认（真漏了还有 suffix / name 兜着），也不能误认。
META_HINTS = ("name", "size", "len", "rawsize", "md5", "sha1", "suffix")

# 形状被「限长」截断时，key_paths 会在这条路径后面加这句（verdict 靠它判「看不全」）。
CUT_HINT = "后面还有"


def _hits(paths, hints):
    """在键路径里挑出命中某一组线索的。"""
    out = []
    for p in paths:
        low = p.lower()
        for h in hints:
            if h in low:
                out.append(p)
                break
    return out


def parse_line(line):
    """从一行日志里抠出（附件标签, 形状字典）；不是形状行就返回 None。"""
    i = line.find(MARK)
    if i < 0:
        return None
    try:
        shape = json.loads(line[i + len(MARK):])
    except ValueError:
        return None                      # 被切半的行、或者格式变了
    label = ""
    j = line.rfind(MARK_PREFIX, 0, i)
    if j >= 0:
        label = line[j + len(MARK_PREFIX):i].rstrip("）")
    return label, shape


def read_shapes(path, want=3):
    """从日志里捞出最近 want 条形状行，返回 [(标签, 形状字典), …]（老的在前）。"""
    if not os.path.isfile(path):
        return []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()                 # 丢掉可能被切掉一半的那一行
        data = f.read()
    got = []
    for line in data.decode("utf-8", "replace").splitlines():
        hit = parse_line(line)
        if hit:
            got.append(hit)
    return got[-want:] if want else got


def key_paths(obj, prefix=""):
    """把形状里的键名摊成一条条路径，好在报告里一眼看出「凭据在哪儿」。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "%s.%s" % (prefix, k) if prefix else str(k)
            if k == "…":
                out.append(p + "（%s，被限长省掉了）" % CUT_HINT)
            else:
                out.extend(key_paths(v, p) or [p])
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = "%s[%d]" % (prefix, i)
            out.extend(key_paths(v, p) or [p])
    return out


def verdict(shapes):
    """对着一堆形状下结论。返回（这条路上有没有可下载的凭据, 结论行列表）。"""
    paths = []
    for _label, shape in shapes:
        paths.extend(key_paths(shape))
    if not shapes:
        return False, ["结论：还没得看——日志里一条附件形状都没有（见下面的「先造一条」）。"]

    dl = _hits(paths, DOWNLOAD_HINTS)
    cr = _hits(paths, CRYPTO_HINTS)
    meta = _hits(paths, META_HINTS)
    # 形状被「限长」截断过的地方（key_paths 会标出来）。有这种地方的时候
    # 「没戏」这个结论是不可信的——凭据可能就在被省掉的那几个键里。
    cut = [p for p in paths if CUT_HINT in p]

    lines = []
    if dl:
        lines.append("结论：有戏 —— 附件里有像下载凭据的字段，照着它写下载就行：")
        for p in dl:
            lines.append("    " + p)
    else:
        lines.append("结论：没戏 —— 附件里看不到任何像下载凭据的字段"
                     "（url / param / token / cdn… 一个都没有）。")
        lines.append("    那就是服务端根本不把原文件给你，得换方案，别硬写。")
    if cut:
        lines.append("⚠ 这次看不全：有 %d 处被「限长」省掉了（上面带「%s」的那些）——"
                     "下载凭据可能就藏在省掉的那几个键里，别急着下结论："
                     "把 ilink_client.SHAPE_KEY_LIMIT 调大再发一条。"
                     % (len(cut), CUT_HINT))
    if cr:
        lines.append("另外：有像加密的字段（%s），拿到的可能是密文；" % "、".join(cr))
        lines.append("    aes128.py 现在只有加密方向，得补解密——按块来，别整份进内存。")
    else:
        lines.append("另外：没有 aes / encrypt 字样——真没加密的话，下下来就是原文件。")
    if meta:
        lines.append("文件名 / 大小：%s" % "、".join(meta))
    else:
        lines.append("文件名 / 大小：一个都没有——落盘时得自己起名字。")
    lines.append("（判定只看字段名：值为了安全一律掩过，所以最后还得你自己"
                 "把上面的键路径扫一眼再决定。）")
    return bool(dl), lines


def sample_notes():
    """日志里没有样本时，把「怎么造一条」讲清楚。"""
    return "\n".join([
        "🔍 先造一条样本（大约一分钟）：",
        "",
        "  1. 停桥：      powershell -ExecutionPolicy Bypass -File desktop\\toggle.ps1 -Off",
        "  2. DEBUG 起桥： powershell -ExecutionPolicy Bypass -File desktop\\toggle.ps1 -DebugLog",
        "     （-DebugLog 是个开关：桥正跑着的时候直接给它会把它关掉，所以先 -Off 再给）",
        "  3. 手机上给这个 bot 发一张图片，再发一个文件",
        "  4. 跑：        python app\\inbox_probe.py",
        "  5. 看完关掉 DEBUG： powershell -ExecutionPolicy Bypass -File desktop\\toggle.ps1 -Off",
        "",
        "为什么单独记一条形状：桥本来就有一条「收到原始消息」的 DEBUG 日志，但它截断在",
        "1500 字符，附件那一段常常正好被切掉；形状行是独立一条，只留键名和长度，",
        "既不截断、也不带凭据。",
    ])


def _raw_shapes(obj):
    """--raw 时用：给了整条消息就逐条 item 出形状，给了单个 item 就出它自己。"""
    items = None
    if isinstance(obj, dict):
        for k in ("item_list", "items"):
            if isinstance(obj.get(k), list):
                items = obj[k]
                break
    if items is None:
        return [("", _shape(obj))]
    return [("", _shape(it)) for it in items]


def _shape(obj):
    import ilink_client
    return ilink_client.item_shape(obj)


def main(argv=None):
    # 中文 Windows 控制台是 GBK，而报告里有 🔍 和「」——按项目惯例降级成「?」，
    # 不能让一句报告把脚本搞崩（和桥回话走的是同一条降级路，见 make_console_safe）
    bridge_config.make_console_safe()
    argv = list(sys.argv[1:] if argv is None else argv)
    want, path, raw = 3, DEFAULT_LOG, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            return 0
        if a in ("-n", "--lines") and i + 1 < len(argv):
            i += 1
            try:
                want = max(1, int(argv[i]))
            except ValueError:
                pass
        elif a == "--log" and i + 1 < len(argv):
            i += 1
            path = argv[i]
        elif a == "--raw" and i + 1 < len(argv):
            i += 1
            raw = argv[i]
        else:
            print("❌ 不认识的参数：%s（看 python app\\inbox_probe.py --help）" % a)
            return 2
        i += 1

    if raw is not None:
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            print("❌ --raw 后面那段不是合法 JSON：%s" % exc)
            return 2
        shapes = _raw_shapes(obj)
        print("🔍 现算的形状（没等真消息，所以日志里可能还是没有）")
    else:
        shapes = read_shapes(path, want)
        print("🔍 从 %s 里读到 %d 条附件形状（最多看最近 %d 条）"
              % (path, len(shapes), want))

    for n, (label, shape) in enumerate(shapes, 1):
        print("")
        print("【第 %d 条】%s" % (n, label or "（没写标签）"))
        print(json.dumps(shape, ensure_ascii=False, indent=2))

    print("")
    paths = []
    for _label, shape in shapes:
        paths.extend(key_paths(shape))
    if paths:
        print("键路径（凭据在哪儿，一眼看这里的名字）：")
        for p in paths:
            print("  " + p)
        print("")

    ok, lines = verdict(shapes)
    for line in lines:
        print(line)

    if not shapes:
        print("")
        print(sample_notes())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
