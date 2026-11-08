# -*- coding: utf-8 -*-
"""进腾讯会议：把会议号拼进 wemeet:// 协议链接，直接拉起客户端入会。

为什么要单独一条通道：开应用那条（app_launcher）**刻意不接受任何参数**，只认名字，
多接一个参数就等于多开一条命令行；而入会必须带会议号。交给 Claude 也过不去——
无头模式下 Bash 类命令要人在电脑上点「允许」，微信那头点不了，`--allowedTools`
里也刻意没给 Bash。所以和开应用一样，桥接层自己办。

安全边界（改代码前先读这几条）：
  - 会议号先剥掉分隔符，再要求「纯数字 9-11 位」；密码要求纯字母数字 4-10 位。
    过不了就当没解析到。所以进 URL 的字符永远只有 [0-9A-Za-z]，注入面为零
  - 走 os.startfile 把链接交给系统的协议注册，**不拼命令行、不过 shell**
  - 只认带词的句式，**裸数字串一律不认**：手机号后 9 位、快递单号、验证码都可能
    撞上，而入会是有后果的动作（你会真的出现在别人的会议里）
  - 两条解析通道：自己打的短句（≤60 字、不许带链接），以及整段转过来的腾讯邀请
    文案（必须带 `#腾讯会议：<号>` 这个腾讯自己生成的标记）。否定、推迟、或者
    句子里还夹着别的事，一律不接，照旧交给 Claude
  - 密码只出现在 URL 里，**任何情况下都不写日志**

定时进会（「六点四十进腾讯会议」）：解析出时刻存进 meetings.json，到点由桥主动发一条
微信问一句，**回「确认」才真的拉起链接**。刻意做成「到点问」而不是「到点直接进」：
你可能已经在别的会里、或者根本没在电脑前。钟点只在**今天之内**解释，过去了就明说
「那个点已经过了」，绝不悄悄顺延到明天，也绝不当成「现在就进」。

未验证的一处：密码参数名。`wemeet://page/inmeeting?meeting_code=<号>` 是从腾讯
会议自己的链接里抄的，密码那个参数名我没有可靠来源，所以做成配置项
（`meeting.password_param`，默认 `password`）。万一客户端不认，它会自己弹密码框
让你手输，不会失败得很难看；换个名字重试只要改配置、不用改代码。
"""

import json
import logging
import os
import re
import threading
import time
import urllib.parse

log = logging.getLogger("bridge.meeting")

DEFAULT_URL = "wemeet://page/inmeeting?meeting_code={code}"
DEFAULT_PASSWORD_PARAM = "password"

# 会议号：9-11 位数字，中间允许空格 / 连字符 / 点 / 间隔号（「413-231-713」）。
# 两头的 lookaround 是关键——不加就会从一长串数字里截出 9 位来当会议号
_SEP = r"[\s\-·.]*"
_NUM = r"(?:[0-9]%s){8,10}[0-9]" % _SEP
_CODE_RE = re.compile(r"(?<![0-9])%s(?![0-9])" % _NUM)
_PWD_RE = re.compile(r"(?:入会密码|会议密码|密码|口令|password|passwd|pwd)"
                     r"\s*[:：=是]?\s*([0-9A-Za-z]{4,10})(?![0-9A-Za-z])",
                     re.IGNORECASE)

# 腾讯会议自己生成的邀请文案里那个固定标记（`#腾讯会议：413-231-713`）。
# 认它是为了能把整段邀请原样转过来——那段里有 `https://meeting.tencent.com/dm/…`
# 的链接、还有一堆日期时段数字，短句通道会（也应该）拒掉。标记是腾讯生成的，
# 比文案里随处可见的数字可信得多，所以照标记取号，旁边有什么都不管。
_INVITE_RE = re.compile(r"(?:#\s*腾讯会议|会议\s*号(?:码)?)\s*[:：=是]?\s*(%s)(?![0-9])"
                        % _NUM)

# 句子里必须出现这些词之一，才去找会议号
_KEYS = ("进会", "入会", "开会", "会议", "meeting", "join", "wemeet", "腾讯")

# 出现这些词就不当「现在进会」看，照旧交给 Claude——否定（「别进 413231713」）、
# 推迟（「明天进会 413231713」）、或者顺带还有别的事（「记到日程里」）。
# 桥没法问清楚，Claude 能。宁可漏拦不误拦：进会是有后果的动作。
# 注意「别」会连带命中「特别」「区别」，这是刻意接受的代价（多一次落回 Claude
# 而已），而漏掉「别进」就是真的把你送进别人的会里。
_NOT_NOW = ("不要", "不用", "不想", "不必", "别", "取消", "先不", "暂时不", "算了",
            "待会", "等会", "稍后", "晚点", "明天", "后天", "下周", "下个月",
            "然后", "并且", "以及", "顺便", "日程", "日历", "提醒", "记一下", "记到")
# 「不带号的进会」只认这几句全等的说法：说了但没给号，1 秒回一句问号比花一次额度好。
# 刻意不含「打开腾讯会议」——那句话没有会议号时就该走开应用那条通道
_ASK_WORDS = ("进会", "入会", "进会议", "进入会议", "加入会议", "参加会议",
              "进腾讯会议", "加入腾讯会议", "进个会", "开始进会")
_ASK_LEAD = ("请", "帮我", "帮忙", "麻烦", "给我", "你", "能不能", "可不可以", "现在",
             "再试一下", "再试试", "试一下", "试试", "再", "快")
_ASK_TAIL = ("吧", "呀", "啊", "呢", "哦", "嘛", "了", "谢谢", "一下", "。", "！", "!",
             "，", ",", "~", " ", "\t", "　")
MAX_LEN = 60          # 短句通道：再长就当句子看，交回 Claude
MAX_INVITE_LEN = 500  # 邀请文案通道：腾讯那段约 200 字，留点余量
CODE_MIN, CODE_MAX = 9, 11

# ---------- 定时进会用到的词表和正则 ----------
#
# 只解释**今天之内**的钟点，外加「N 分钟后」这种相对说法。刻意不收「明天」「待会」
# 「稍后」（它们在 _NOT_NOW 里，整句交给 Claude）：一是用户选定了当天的口径，
# 二是模糊说法解释错了代价不小——到点会真的问你一句，甚至进别人的会。
_CN = "[零〇一二两三四五六七八九十]"
_PERIOD = ("今天晚上|今晚上|今晚|今天|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜|凌晨")
# 「六点四十」「6点40」「十一点半」「三点」——分钟可以没有
_CLOCK_CN_RE = re.compile(r"(?:(?P<period>%s)\s*)?(?P<h>\d{1,2}|%s{1,3})\s*[点點時时]"
                          r"\s*(?:(?P<m>半|整|正|\d{1,2}|%s{1,3})\s*(?:分钟|分)?)?"
                          % (_PERIOD, _CN, _CN))
# 「18:40」「下午 3:00」——两位分钟，前后不许再挨数字或冒号（挡住 20:00-21:00 这种时段）
_CLOCK_24_RE = re.compile(r"(?:(?P<period>%s)\s*)?(?<![0-9:：])(?P<h>\d{1,2})\s*[:：]\s*"
                          r"(?P<m>[0-5][0-9])(?![0-9:：])" % _PERIOD)
# 「20 分钟后」「半小时后」「一个小时以后」
_REL_RE = re.compile(r"(?P<n>\d{1,3}|半|%s{1,3})\s*(?:个)?\s*(?P<unit>分钟|分|小时|钟头)"
                     r"\s*(?:之|以)?后" % _CN)
# 「一个半小时后」：不单独认一次的话，上面那条会从中间截出「半小时后」当 30 分钟
_REL_HALF_RE = re.compile(r"(?P<n>\d{1,2}|%s{1,3})\s*个?\s*半\s*(?:小时|钟头)"
                          r"\s*(?:之|以)?后" % _CN)
# 腾讯邀请文案自带的时间（「会议时间：2026/08/30 20:00-21:00」）不是你说的时间。
# 找时刻之前先把这类行整行丢掉——不丢就会照着别人的文案定时，甚至定到别的日子
_NOISE_LINE_RE = re.compile(r"会议时间|会议主题|开始时间|结束时间|meeting\s*time|https?://|"
                            r"\d{4}\s*[/年.-]\s*\d{1,2}\s*[/月.-]\s*\d{1,2}|"
                            r"\d{1,2}\s*[:：]\s*\d{2}\s*[-–—~～至到]\s*\d{1,2}\s*[:：]\s*\d{2}",
                            re.IGNORECASE)
_CN_VALUE = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
MAX_AHEAD_SECONDS = 24 * 3600     # 再远就不接：口径是「今天之内」
LATE_TOLERANCE = 90              # 说的那个点刚过去 90 秒内，还算「就是现在」

# 到点之后那条问话的答复。「进会」这类明确说法即使还有别的待确认也认；
# 光一个「确认」只在没有待确认的点击时才算数（免得两件事抢同一个词）
_OK_WORDS = ("确认", "确定", "可以", "好", "好的", "行", "对", "是", "ok", "okay",
             "yes", "y", "进", "进会", "进去", "进吧", "确认进会", "现在进")
_NO_WORDS = ("取消", "不进", "不进了", "别进", "别进了", "不用", "不用了", "算了", "不",
             "no", "n", "cancel", "取消进会", "先不进")
_EXPLICIT_WORDS = ("进会", "进去", "进吧", "确认进会", "现在进", "不进", "不进了",
                   "别进", "别进了", "取消进会", "先不进")
# 取消已经定下的会（不是取消待确认的那一次）
_CANCEL_TIMER_WORDS = ("取消定时", "取消定时进会", "取消预约", "取消预约进会", "不定了",
                       "不定", "不定时了", "取消那个定时", "/untimer", "/取消定时")
_LIST_TIMER_WORDS = ("/meetings", "/timers", "/定时", "定时列表", "看看定时", "看下定时",
                     "有哪些定时", "待进的会", "/会议")


# ---------- 解析 ----------

def _digits(s):
    return re.sub(r"[^0-9]", "", s or "")


def _as_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def valid_code(code):
    """通过就返回纯数字，不通过返回空串。允许带分隔符，别的字符一律不许有。"""
    raw = (code or "").strip()
    if not raw or re.fullmatch(r"[0-9\s\-·.]+", raw) is None:
        return ""
    d = _digits(raw)
    return d if CODE_MIN <= len(d) <= CODE_MAX else ""


def valid_password(pwd):
    p = (pwd or "").strip()
    return p if re.fullmatch(r"[0-9A-Za-z]{4,10}", p) else ""


def pretty_code(code):
    """9 位按 3-3-3 断开，10 位按 3-3-4：手机上好核对。其余原样。"""
    d = _digits(code)
    if len(d) == 9:
        return "%s-%s-%s" % (d[:3], d[3:6], d[6:])
    if len(d) == 10:
        return "%s-%s-%s" % (d[:3], d[3:6], d[6:])
    return d


def _find_password(text):
    """摘出密码，并把那一段从原文里挖掉，返回 (密码, 剩下的话)。

    必须先摘再找会议号——不然「进会 413231713 密码 8888」里那两串数字会被
    当成一个 11 位的号。
    """
    m = _PWD_RE.search(text)
    if not m:
        return "", text
    cand = valid_password(m.group(1))
    if not cand:
        return "", text
    return cand, text[:m.start()] + " " + text[m.end():]


def _parse_short(text):
    """短句通道：「进会 413231713」这种自己打的话。链接和长句一律不认。"""
    if len(text) > MAX_LEN:
        return None, None
    low = text.lower()
    if "http" in low or "\\" in text or "/dm/" in low:
        return None, None                 # 是个链接，不是会议号，交回 Claude
    if not any(k in low for k in _KEYS):
        return None, None                 # 裸数字串一律不认
    pwd, rest = _find_password(text)
    hits = [_digits(x.group(0)) for x in _CODE_RE.finditer(rest)]
    hits = [h for h in hits if CODE_MIN <= len(h) <= CODE_MAX]
    if len(hits) != 1:
        return None, None                 # 一个都没有，或者有好几串，都不猜
    return hits[0], (pwd or None)


def _parse_invite(text):
    """邀请文案通道：整段转过来也认，前提是里面有 `#腾讯会议：<号>` 这个标记。

    这条通道刻意放宽了长度和「不许有链接」两条——腾讯那段邀请里必然带
    `https://meeting.tencent.com/dm/…`，短句通道会（也应该）拒掉它。仍然要求
    **整段里只有一个标记号**：两个不同的号就是两场会，不猜。
    """
    if len(text) > MAX_INVITE_LEN:
        return None, None
    pwd, rest = _find_password(text)
    codes = []
    for m in _INVITE_RE.finditer(rest):
        d = _digits(m.group(1))
        if CODE_MIN <= len(d) <= CODE_MAX and d not in codes:
            codes.append(d)
    if len(codes) != 1:
        return None, None
    return codes[0], (pwd or None)


def parse_join(body):
    """解析成 (会议号, 密码)；不像「现在带着会议号进会」就返回 (None, None)。

    两条通道：自己打的短句，以及整段转过来的腾讯邀请文案（认 `#腾讯会议：<号>`）。
    句子里出现时刻说法（「六点四十」「18:40」「20 分钟后」）时**一律不算现在进会**
    ——那是定时，走 parse_schedule；漏在这里就会像真机上那次一样立刻把人拉进会。
    """
    text = (body or "").strip()
    if not text:
        return None, None
    if any(w in text for w in _NOT_NOW):
        return None, None                 # 否定、推迟、或者还夹着别的事
    if mentions_time(text):
        return None, None                 # 带时刻的交给 parse_schedule
    code, pwd = _parse_short(text)
    if code:
        return code, pwd
    return _parse_invite(text)


def _strip_ask(text):
    """两头的客套各剥最多三层，剥完还要跟词表全等，所以剥狠一点也不会误认。"""
    s = (text or "").strip()
    for _ in range(3):
        before = s
        for w in _ASK_LEAD:
            if s.startswith(w) and len(s) > len(w):
                s = s[len(w):].lstrip()
                break
        if s == before:
            break
    for _ in range(3):
        before = s
        for w in _ASK_TAIL:
            if s.endswith(w) and len(s) > len(w):
                s = s[:-len(w)].rstrip()
                break
        if s == before:
            break
    return s.strip()


def parse_join_asking(body):
    """「进会」但一个数字都没给：值得 1 秒回一句「把会议号发我」，别花额度。"""
    text = (body or "").strip()
    if not text or len(text) > 20 or any(c.isdigit() for c in text):
        return False
    return _strip_ask(text) in _ASK_WORDS


# ---------- 定时：把「六点四十」变成一个时间戳 ----------

def _cn_int(s):
    """「六」→6、「四十」→40、「十五」→15、「三十五」→35、「20」→20。认不出返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = 1 if head == "" else _CN_VALUE.get(head)
        ones = 0 if tail == "" else _CN_VALUE.get(tail)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    out = 0
    for ch in s:
        if ch not in _CN_VALUE:
            return None
        out = out * 10 + _CN_VALUE[ch]
    return out


def _time_text(text):
    """找时刻之前先把腾讯邀请文案自带的时间行丢掉，只在你自己写的那些字里找。"""
    keep = [ln for ln in (text or "").splitlines() if not _NOISE_LINE_RE.search(ln)]
    return "\n".join(keep)


def _shift_hour(h, period):
    """把「下午三点」这类说法折成 24 小时制。没给上下午就原样返回。"""
    if not period:
        return h
    if period in ("下午", "傍晚"):
        return h + 12 if h < 12 else h
    if period in ("晚上", "今晚", "今晚上", "今天晚上"):
        return h + 12 if h < 12 else h
    if period == "中午":
        return h + 12 if h < 6 else h        # 「中午一点」= 13 点
    if period in ("早上", "早晨", "上午"):
        return 0 if h == 12 else h           # 「上午十二点」当 0 点
    return h                                  # 今天 / 凌晨 / 夜里 / 半夜：原样


def stamp_label(ts, now=None):
    """给用户看的时刻：今天的就「18:40」，跨到明天就写明「明天 00:20」。"""
    now = time.time() if now is None else now
    t, n, tm = time.localtime(ts), time.localtime(now), time.localtime(now + 86400)
    if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday):
        head = ""
    elif (t.tm_year, t.tm_yday) == (tm.tm_year, tm.tm_yday):
        head = "明天 "
    else:
        head = time.strftime("%m-%d ", t)
    return head + time.strftime("%H:%M", t)


def _today_at(h, m, now):
    """今天的 h:m。h 可以是 24（表示明天 0 点），别的越界值返回 None。"""
    if not (0 <= m <= 59) or not (0 <= h <= 24):
        return None
    t = time.localtime(now)
    base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return base + h * 3600 + m * 60


def _match_time(text):
    """在这段话里找时刻说法。返回 (原文片段, 类型, 数值元组) 或 None。"""
    m = _CLOCK_24_RE.search(text)
    if m:
        return m.group(0), "clock", (int(m.group("h")), int(m.group("m")),
                                     m.group("period"))
    m = _REL_HALF_RE.search(text)          # 「一个半小时后」得先认，不然会被当成 30 分钟
    if m:
        n = _cn_int(m.group("n"))
        if n is not None:
            return m.group(0), "rel", (n * 60 + 30,)
    m = _REL_RE.search(text)

    if m:
        unit, raw = m.group("unit"), m.group("n")
        if raw == "半":
            # 「半小时后」= 30 分钟；「半分钟后」不接（那不是在定会议）
            mins = 30 if unit in ("小时", "钟头") else None
        else:
            n = _cn_int(raw)
            mins = None if n is None else (n * 60 if unit in ("小时", "钟头") else n)
        # 越界的（「30 小时后」）也照样认下来：这里放走的话 mentions_time 就是 False，
        # 那句话会被立刻进会那条接走——宁可回一句「太远了」，也不能当场把人拉进会
        if mins is not None and mins >= 1:
            return m.group(0), "rel", (mins,)
    m = _CLOCK_CN_RE.search(text)
    if m:
        h = _cn_int(m.group("h"))
        raw_m = m.group("m")
        if raw_m in ("半",):
            minute = 30
        elif raw_m in ("整", "正", None, ""):
            minute = 0
        else:
            minute = _cn_int(raw_m)
        if h is not None and minute is not None:
            return m.group(0), "clock", (h, minute, m.group("period"))
    return None


def mentions_time(text):
    """这句话里提到时刻了吗（认不认得出来另说）。提了就绝不当「现在进会」。"""
    return _match_time(_time_text(text or "")) is not None


def match_time_span(text):
    """时刻说法占了哪一段原文（`parse_when` 认的就是这一段）。没提时刻返回 None。

    给 `power.py` 用：定时关机也要「摘掉时间之后剩下的必须是空的」，而它拿不到
    `parse_when` 内部匹配到的那一段。这里只是把那段原文露出来，不改任何判断。
    """
    hit = _match_time(_time_text(text or ""))
    return hit[0] if hit else None


def parse_when(text, now=None):
    """→ (时间戳, 标签, 毛病)。没提时刻就 (None, None, None)。

    毛病是 'past'（那个点今天已经过去了）或 'far'（超过 24 小时），这两种都不定时，
    但值得 1 秒回一句，别悄悄顺延到明天——顺延过的时刻到点你早忘了。
    """
    now = time.time() if now is None else now
    hit = _match_time(_time_text(text or ""))
    if not hit:
        return None, None, None
    span, kind, vals = hit
    if kind == "rel":
        ts = now + vals[0] * 60
        if ts - now > MAX_AHEAD_SECONDS:
            return None, stamp_label(ts, now), "far"
        return ts, stamp_label(ts, now), None
    h, minute, period = vals
    if minute > 59:
        return None, None, None
    cands = []
    base = _shift_hour(h, period)
    for cand_h in ([base] if (period or h > 12) else sorted({h, h + 12})):
        ts = _today_at(cand_h, minute, now)
        if ts is not None:
            cands.append(ts)
    future = [ts for ts in sorted(cands) if ts > now - LATE_TOLERANCE]
    if not cands:
        return None, None, None           # 时分越界（「99:99」），当没看见
    if not future:
        # 今天已经过去了。刻意不顺延到明天：口径就是当天，说错了让人再说一遍。
        # 「三点」这种没说上下午的，报最晚那个候选（15:00 比 03:00 更可能是你的意思）
        return None, time.strftime("%H:%M", time.localtime(max(cands))), "past"
    ts = future[0]
    if ts - now > MAX_AHEAD_SECONDS:
        return None, stamp_label(ts, now), "far"
    return ts, stamp_label(ts, now), None


def _blank(text, span):
    """把时刻那一段挖掉再去找会议号：不挖的话「18:40 413231713」会被拼成 11 位。"""
    return text.replace(span, " " * len(span)) if span else text


def parse_schedule(body, now=None):
    """定时进会。返回 dict，或者 None 表示「这条跟定时无关，照旧往下走」。

      {"kind": "schedule", "code": …, "pwd": …, "at": 时间戳, "label": "18:40"}
      {"kind": "need_code", "at": …, "label": …}   说了时间但还没给号
      {"kind": "past" / "far", "label": …}         那个点用不了，值得回一句
    """
    text = (body or "").strip()
    if not text or len(text) > MAX_INVITE_LEN:
        return None
    if any(w in text for w in _NOT_NOW):
        return None                       # 否定、推迟、日程、还夹着别的事：交给 Claude
    low = text.lower()
    if not (any(k in low for k in _KEYS) or _INVITE_RE.search(text)):
        return None                       # 光说个时刻不算，得是在说会
    hit = _match_time(_time_text(text))
    if not hit:
        return None
    ts, label, why = parse_when(text, now)
    if why:
        return {"kind": why, "label": label}
    if ts is None:
        return None
    rest = _blank(text, hit[0])
    code, pwd = _parse_short(rest)
    if not code:
        code, pwd = _parse_invite(rest)
    if code:
        return {"kind": "schedule", "code": code, "pwd": pwd, "at": ts, "label": label}
    return {"kind": "need_code", "at": ts, "label": label}


def _norm_reply(text):
    s = (text or "").strip().lower()
    for ch in " \t　.。!！?？,，、;；~～「」\"'“”":
        s = s.replace(ch, "")
    return s


def read_confirm(body):
    """到点那条问话的答复。返回 ('yes'/'no'/None, 是不是明确说了「进会」这种词)。"""
    s = _norm_reply(body)
    if not s:
        return None, False
    explicit = s in _EXPLICIT_WORDS
    if s in _NO_WORDS:
        return "no", explicit
    if s in _OK_WORDS:
        return "yes", explicit
    return None, False


def parse_cancel_timer(body):
    """整条消息就是「取消定时」这类说法。剥完客套还要全等，所以不会误伤。"""
    return _strip_ask(_norm_reply(body)) in _CANCEL_TIMER_WORDS


def parse_list_timer(body):
    return _strip_ask(_norm_reply(body)) in _LIST_TIMER_WORDS


# ---------- 真正动手 ----------

def _default_open(url):
    starter = getattr(os, "startfile", None)
    if starter is None:
        raise RuntimeError("这台机器不是 Windows，没有 os.startfile")
    starter(url)


class MeetingJoiner(object):
    """只做一件事：把会议号拼进协议链接，交给系统去拉客户端。"""

    def __init__(self, url_template=DEFAULT_URL,
                 password_param=DEFAULT_PASSWORD_PARAM, opener=None):
        self.url_template = url_template or DEFAULT_URL
        self.password_param = (password_param or "").strip()
        self._open = opener or _default_open

    def build_url(self, code, password=None):
        code = valid_code(code)
        if not code:
            return ""
        url = self.url_template.format(code=code)
        pwd = valid_password(password)
        if pwd and self.password_param:
            url += ("&" if "?" in url else "?") + "%s=%s" % (
                self.password_param, urllib.parse.quote(pwd))
        return url

    def join(self, code, password=None):
        """返回 (要回的话, 是否已处理)。没处理就交回上层，别在这里替 Claude 编理由。"""
        clean = valid_code(code)
        if not clean:
            return None, False
        url = self.build_url(clean, password)
        try:
            self._open(url)
        except Exception as exc:                       # noqa: BLE001 - 什么都可能
            log.warning("拉起会议失败：%s", exc)
            return ("❌ 拉不起 wemeet:// 链接（%s）。腾讯会议可能没装，或者这个协议没在"
                    "系统里注册。先试「%s 打开腾讯会议」，起来了再手输 %s。"
                    % (exc.__class__.__name__, "/cc", pretty_code(clean)), True)
        log.info("已拉起会议入会链接（会议号 %d 位%s）",
                 len(clean), "，带密码" if valid_password(password) else "")
        text = "⏳ 正在拉起腾讯会议，进 %s。" % pretty_code(clean)
        if valid_password(password):
            text += ("\n密码我拼进链接了，但那个参数名我没验证过；客户端要是没接住"
                     "就会自己弹密码框，你手输一下。")
        text += "\n麦克风和摄像头由客户端自己的默认设置决定，链接里控制不了。"
        return text, True


class MeetingSchedule(object):
    """待进的会议表：内存一份，落盘一份（meetings.json）。

    落盘是刻意的——18:00 定下 18:40 的会，中间关一次开关不该让它无声消失。代价是
    这个文件里有会议号和入会密码（明文），所以它同时进了 .gitignore 和「不发到微信」
    的名单。表很小（默认最多 5 条），每次改动整份重写。
    """

    def __init__(self, path, max_pending=5):
        self.path = path
        self.max_pending = max(1, int(max_pending or 1))
        self._lock = threading.RLock()
        self.items = []
        self._load()

    # ---- 落盘 ----

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh) or {}
        except (IOError, OSError, ValueError):
            return                            # 没有这个文件是常态，不算错
        out = []
        for it in (raw.get("pending") or []):
            if not isinstance(it, dict):
                continue
            code = valid_code(it.get("code"))
            try:
                at = float(it.get("at") or 0)
            except (TypeError, ValueError):
                continue
            if not code or at <= 0:
                continue
            out.append({"uid": str(it.get("uid") or ""), "code": code,
                        "pwd": valid_password(it.get("pwd")) or None, "at": at,
                        "label": str(it.get("label") or ""),
                        "set_at": _as_float(it.get("set_at"), at)})
        self.items = sorted(out, key=lambda x: x["at"])[:self.max_pending]

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"pending": self.items}, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except (IOError, OSError) as exc:                  # 盘满、被占用
            log.warning("待进会议表没写下去（桥重启就会丢）：%s", exc)

    # ---- 增删查 ----

    def add(self, uid, code, at, label, pwd=None):
        """返回 (记下的那条, 拒绝的理由)。理由非空就是没记下。"""
        code = valid_code(code)
        if not code:
            return None, "会议号不对"
        with self._lock:
            for it in self.items:
                if it["code"] == code and abs(it["at"] - at) < 60:
                    return None, "same"
            if len(self.items) >= self.max_pending:
                return None, "full"
            item = {"uid": str(uid), "code": code,
                    "pwd": valid_password(pwd) or None, "at": float(at),
                    "label": label or "", "set_at": time.time()}
            self.items.append(item)
            self.items.sort(key=lambda x: x["at"])
            self._save()
            return dict(item), ""

    def all(self, uid=None):
        with self._lock:
            return [dict(it) for it in self.items if uid is None or it["uid"] == str(uid)]

    def cancel(self, uid=None):
        """取消（默认这个用户的全部）。返回被取消的那些。"""
        with self._lock:
            gone = [dict(it) for it in self.items if uid is None or it["uid"] == str(uid)]
            if gone:
                self.items = [it for it in self.items
                              if not (uid is None or it["uid"] == str(uid))]
                self._save()
            return gone

    def due(self, now=None):
        """到点的都取出来（同时从表里删掉），每条带上迟了多少秒。

        迟多久算「还值得问一句」由桥决定：桥没在跑的时候到点了，就是靠这个数字
        分出「刚过一会儿，问一下」和「早过去了，只补一句说明」。
        """
        now = time.time() if now is None else now
        with self._lock:
            hit = [it for it in self.items if it["at"] <= now]
            if not hit:
                return []
            self.items = [it for it in self.items if it["at"] > now]
            self._save()
        out = []
        for it in hit:
            d = dict(it)
            d["late"] = max(0.0, now - it["at"])
            out.append(d)
        return out
