# -*- coding: utf-8 -*-
"""AES-128-ECB + PKCS7，加密和解密两个方向都有，纯 Python 实现。

为什么要自己写：这个项目的硬规矩是零第三方依赖（HTTP 全走 urllib），而
Python 标准库里没有 AES。微信 CDN 上传要先把文件 AES-128 加密再传；收文件
反过来 —— 从 CDN 下来的也是密文，得在本地解密，所以两个方向都得有
（解密方向 2026-09-26 加的，就是「收你发来的图片和文件」这一步要用的）。

正确性靠自测里的 FIPS-197 官方向量保证，不是靠我读着觉得对：
    key       000102030405060708090a0b0c0d0e0f
    plaintext 00112233445566778899aabbccddeeff
    cipher    69c4e0d86a7b0430d8cdb78070b4c55a
附录 B 那组（32 43 f6 a8… → 39 25 84 1d…）也一起卡住；另外用「随机数据加密
再解密必须原样回来」兜一遍——但那只能证明两个方向自洽，证明不了它们都合 FIPS，
所以官方向量才是主证据。

S 盒不是手抄 256 个十六进制数（那样抄错一个自测未必抓得住位置），而是按
GF(2^8) 求逆 + 仿射变换现算出来的；逆 S 盒由 S 盒反查得到。自测里另外核对了
两边各几个已知值。
"""

BLOCK_SIZE = 16


def _rotl8(x, s):
    return ((x << s) | (x >> (8 - s))) & 0xFF


def _build_sbox():
    """按标准的 p 乘 3 / q 除 3 走一圈 GF(2^8)，再做仿射变换。"""
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q = (q ^ (q << 1)) & 0xFF
        q = (q ^ (q << 2)) & 0xFF
        q = (q ^ (q << 4)) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _build_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


# T 表：把 SubBytes + ShiftRows + MixColumns 压成 4 张 256 项的 32 位表。
# 逐字节实现也能跑，但要给几 MB 的文件加密，纯 Python 下慢得不能接受。
def _build_tables():
    te0 = []
    for x in range(256):
        s = SBOX[x]
        s2, s3 = _xtime(s), _xtime(s) ^ s
        te0.append((s2 << 24) | (s << 16) | (s << 8) | s3)
    te1 = [((w >> 8) | (w << 24)) & 0xFFFFFFFF for w in te0]
    te2 = [((w >> 16) | (w << 16)) & 0xFFFFFFFF for w in te0]
    te3 = [((w >> 24) | (w << 8)) & 0xFFFFFFFF for w in te0]
    return te0, te1, te2, te3


TE0, TE1, TE2, TE3 = _build_tables()


# 解密那半边：逆 S 盒由 S 盒反查（S 盒是双射，反查唯一），再由它算 InvMixColumns
# 的 T 表。InvShiftRows 是**往右**移，所以查表时的取字节顺序和加密那边正好反过来
# （见 decrypt_block 里那四行）。
def _build_inv_sbox():
    inv = [0] * 256
    for i, v in enumerate(SBOX):
        inv[v] = i
    return inv


ISBOX = _build_inv_sbox()


def _gmul(a, b):
    """GF(2^8) 上的乘法。只为了算 InvMixColumns 的 0e/09/0d/0b 四个系数。"""
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _build_inv_tables():
    td0 = []
    for x in range(256):
        s = ISBOX[x]
        td0.append((_gmul(s, 0x0E) << 24) | (_gmul(s, 0x09) << 16)
                   | (_gmul(s, 0x0D) << 8) | _gmul(s, 0x0B))
    td1 = [((w >> 8) | (w << 24)) & 0xFFFFFFFF for w in td0]
    td2 = [((w >> 16) | (w << 16)) & 0xFFFFFFFF for w in td0]
    td3 = [((w >> 24) | (w << 8)) & 0xFFFFFFFF for w in td0]
    return td0, td1, td2, td3


TD0, TD1, TD2, TD3 = _build_inv_tables()


def expand_key(key):
    """128 位密钥展开成 44 个 32 位字（11 轮，每轮 4 个）。"""
    if len(key) != 16:
        raise ValueError("AES-128 的密钥必须是 16 字节，给的是 %d" % len(key))
    w = [int.from_bytes(key[i:i + 4], "big") for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = w[i - 1]
        if i % 4 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF          # RotWord
            t = ((SBOX[(t >> 24) & 0xFF] << 24) | (SBOX[(t >> 16) & 0xFF] << 16)
                 | (SBOX[(t >> 8) & 0xFF] << 8) | SBOX[t & 0xFF])
            t ^= _RCON[i // 4 - 1] << 24
        w.append(w[i - 4] ^ t)
    return w


def expand_key_dec(key):
    """解密要用的轮密钥。

    T 表版解密把 InvSubBytes + InvShiftRows + InvMixColumns 合成一次查表
    （见 decrypt_block），所以中间九轮的轮密钥得**先各自过一遍 InvMixColumns**
    —— 这就是 FIPS-197 §5.3.5 的「等价逆密码」。首尾两轮（第 10 轮和第 0 轮）
    不参与混列，原样用。

    也可以不转轮密钥、老老实实按 InvCipher 一轮一轮混列（那样每块都要多算一遍），
    转一次、整份文件复用更划算。**忘了转就是解出来一片乱码**，而且往返测试
    会一起红（加密和解密都错的时候，往返反而可能"自洽"——所以自测里
    FIPS 官方向量是主证据，往返只是兜底）。
    """
    w = expand_key(key)
    dw = list(w)
    for i in range(4, 40):
        v = w[i]
        dw[i] = (TD0[SBOX[(v >> 24) & 0xFF]] ^ TD1[SBOX[(v >> 16) & 0xFF]]
                 ^ TD2[SBOX[(v >> 8) & 0xFF]] ^ TD3[SBOX[v & 0xFF]])
    return dw


def encrypt_block(block, w):
    """加密一个 16 字节块。w 是 expand_key 的结果。"""
    s0 = int.from_bytes(block[0:4], "big") ^ w[0]
    s1 = int.from_bytes(block[4:8], "big") ^ w[1]
    s2 = int.from_bytes(block[8:12], "big") ^ w[2]
    s3 = int.from_bytes(block[12:16], "big") ^ w[3]
    k = 4
    for _ in range(9):
        t0 = (TE0[s0 >> 24] ^ TE1[(s1 >> 16) & 0xFF] ^ TE2[(s2 >> 8) & 0xFF]
              ^ TE3[s3 & 0xFF] ^ w[k])
        t1 = (TE0[s1 >> 24] ^ TE1[(s2 >> 16) & 0xFF] ^ TE2[(s3 >> 8) & 0xFF]
              ^ TE3[s0 & 0xFF] ^ w[k + 1])
        t2 = (TE0[s2 >> 24] ^ TE1[(s3 >> 16) & 0xFF] ^ TE2[(s0 >> 8) & 0xFF]
              ^ TE3[s1 & 0xFF] ^ w[k + 2])
        t3 = (TE0[s3 >> 24] ^ TE1[(s0 >> 16) & 0xFF] ^ TE2[(s1 >> 8) & 0xFF]
              ^ TE3[s2 & 0xFF] ^ w[k + 3])
        s0, s1, s2, s3 = t0, t1, t2, t3
        k += 4
    # 最后一轮没有 MixColumns，直接用 S 盒
    out = bytearray(16)
    for i, (a, b, c, d) in enumerate(((s0, s1, s2, s3), (s1, s2, s3, s0),
                                      (s2, s3, s0, s1), (s3, s0, s1, s2))):
        v = ((SBOX[a >> 24] << 24) | (SBOX[(b >> 16) & 0xFF] << 16)
             | (SBOX[(c >> 8) & 0xFF] << 8) | SBOX[d & 0xFF]) ^ w[40 + i]
        out[i * 4:i * 4 + 4] = (v & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(out)


def pkcs7_pad(data):
    """PKCS7：长度刚好是 16 的倍数时也补满一个整块。

    这一条和微信那边算 filesize 的公式对得上（rawsize + (16 - rawsize % 16)），
    所以密文长度永远比明文长 1 到 16 字节，绝不会相等。
    """
    n = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
    return bytes(data) + bytes([n]) * n


def padded_size(raw_size):
    """密文长度。和上面的补齐规则必须一致，getuploadurl 要先报这个数。"""
    return raw_size + (BLOCK_SIZE - raw_size % BLOCK_SIZE)


def encrypt_ecb_pkcs7(key, data):
    """AES-128-ECB + PKCS7。ECB 没有 IV，每块独立加密。

    微信那套 JS 实现写的是「CBC + 全零 IV，一次只喂一个块」，那和 ECB 完全等价
    （每块的 IV 都是零，等于没有链接），所以这里直接按 ECB 写。
    """
    w = expand_key(key)
    buf = pkcs7_pad(data)
    out = bytearray(len(buf))
    for i in range(0, len(buf), BLOCK_SIZE):
        out[i:i + BLOCK_SIZE] = encrypt_block(buf[i:i + BLOCK_SIZE], w)
    return bytes(out)


def decrypt_block(block, w):
    """解密一个 16 字节块。w 必须是 expand_key_dec 的结果（不是 expand_key）。

    和 encrypt_block 对称：轮密钥倒着用（先 w[40..43]，最后一轮回到 w[0..3]），
    中间九轮走 InvMixColumns 那四张表，最后一轮不混列、直接用逆 S 盒。
    这里的 w 必须已经过 expand_key_dec 那一步（中间九轮密钥混过列），否则解出来
    是乱码——原因写在 expand_key_dec 的说明里。
    """
    s0 = int.from_bytes(block[0:4], "big") ^ w[40]
    s1 = int.from_bytes(block[4:8], "big") ^ w[41]
    s2 = int.from_bytes(block[8:12], "big") ^ w[42]
    s3 = int.from_bytes(block[12:16], "big") ^ w[43]
    k = 36
    for _ in range(9):
        t0 = (TD0[s0 >> 24] ^ TD1[(s3 >> 16) & 0xFF] ^ TD2[(s2 >> 8) & 0xFF]
              ^ TD3[s1 & 0xFF] ^ w[k])
        t1 = (TD0[s1 >> 24] ^ TD1[(s0 >> 16) & 0xFF] ^ TD2[(s3 >> 8) & 0xFF]
              ^ TD3[s2 & 0xFF] ^ w[k + 1])
        t2 = (TD0[s2 >> 24] ^ TD1[(s1 >> 16) & 0xFF] ^ TD2[(s0 >> 8) & 0xFF]
              ^ TD3[s3 & 0xFF] ^ w[k + 2])
        t3 = (TD0[s3 >> 24] ^ TD1[(s2 >> 16) & 0xFF] ^ TD2[(s1 >> 8) & 0xFF]
              ^ TD3[s0 & 0xFF] ^ w[k + 3])
        s0, s1, s2, s3 = t0, t1, t2, t3
        k -= 4
    out = bytearray(16)
    for i, (a, b, c, d) in enumerate(((s0, s3, s2, s1), (s1, s0, s3, s2),
                                      (s2, s1, s0, s3), (s3, s2, s1, s0))):
        v = ((ISBOX[a >> 24] << 24) | (ISBOX[(b >> 16) & 0xFF] << 16)
             | (ISBOX[(c >> 8) & 0xFF] << 8) | ISBOX[d & 0xFF]) ^ w[i]
        out[i * 4:i * 4 + 4] = (v & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(out)


def unpad(data):
    """去掉 PKCS7 填充。

    填充不合法就抛 ValueError —— 宁可炸，也不能把「解错的字节」当正文写进文件。
    """
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("密文长度必须是 %d 的整数倍，给的是 %d"
                         % (BLOCK_SIZE, len(data)))
    n = data[-1]
    if not 1 <= n <= BLOCK_SIZE or data[-n:] != bytes([n]) * n:
        raise ValueError("PKCS7 填充不合法（末尾 %d 字节对不上）" % n)
    return bytes(data[:-n])


def _decrypt_blocks(w, data):
    out = bytearray(len(data))
    for i in range(0, len(data), BLOCK_SIZE):
        out[i:i + BLOCK_SIZE] = decrypt_block(data[i:i + BLOCK_SIZE], w)
    return bytes(out)


def decrypt_ecb_pkcs7(key, data):
    """AES-128-ECB + PKCS7 解密（整份进、整份出）。大文件走 decrypt_stream。"""
    if len(data) % BLOCK_SIZE:
        raise ValueError("密文长度必须是 %d 的整数倍，给的是 %d"
                         % (BLOCK_SIZE, len(data)))
    return unpad(_decrypt_blocks(expand_key_dec(key), data))


def decrypt_stream(key, chunks):
    """按块解密一段流式密文，吐出来的是一段段明文。

    为什么要有它：CDN 下来的可能是几十 MB，整份进内存会把桥顶爆（纯 Python 的
    AES 实测大约 1.5 MB/s，20 MB 要十几秒，期间内存里同时躺着密文、明文和一份
    中间缓冲）。这里**往后只压一个块**——PKCS7 的填充信息在最后一个字节里，
    所以最后一块必须留到「确定没有更多数据」之后才吐（就是函数末尾那次）。

    用法：for plain in decrypt_stream(key, 一段段密文): 写到盘上
    """
    w = expand_key_dec(key)
    buf = b""
    for chunk in chunks:
        buf += chunk
        # 留住最后（可能不完整、也可能带着填充）的那一块
        keep = BLOCK_SIZE if len(buf) >= BLOCK_SIZE else 0
        usable = len(buf) - keep
        usable -= usable % BLOCK_SIZE
        if usable <= 0:
            continue
        yield _decrypt_blocks(w, buf[:usable])
        buf = buf[usable:]
    if len(buf) % BLOCK_SIZE:
        raise ValueError("密文总长不是 %d 的整数倍（最后剩 %d 字节）——多半是没下全"
                         % (BLOCK_SIZE, len(buf)))
    yield unpad(_decrypt_blocks(w, buf))
