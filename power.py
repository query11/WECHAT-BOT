# -*- coding: utf-8 -*-
"""关机、锁屏、睡眠／休眠。

几件事的难度不在一个量级，改代码前先分清：

1. **锁屏**：`user32!LockWorkStation`，一行 ctypes，当前用户就能干。
2. **关机**：`shutdown.exe /s /t N`，SeShutdownPrivilege 交互用户默认就有。
   默认留 60 秒空窗，一是回话来得及发出去（`/t 0` 的话桥自己先被杀），
   二是发现按错了还能说「取消关机」（`shutdown /a`）。时间既可以是相对的
   （「十分钟后关机」）也可以是**当天钟点**（「晚上十点关机」）——钟点那半边借
   `wemeet` 那套解析，两者的先后顺序有讲究，见 `_take_time`。

   **`/t` 大于 0 时 shutdown.exe 隐含 `/f`**（`shutdown /?` 里明写着这句）。所以到点
   是**强制**关，没保存的东西不会弹窗问你。这儿以前的注释写的是「不带 /f，让应用自己
   弹窗问」，那是错的，2026-09-03 改过来了——定时关机多半是人不在电脑前的时候到点，
   这个区别要紧。

3. **睡眠 / 休眠**：`powrprof!SetSuspendState(False, False, False)`（睡眠）和
   `shutdown /h`（休眠），两件都不用提权。

   **不能用 `rundll32 powrprof.dll,SetSuspendState 0,1,0` 那条流传很广的写法**：开着
   休眠的机器上它会真休眠下去，而「睡眠」和「休眠」在人嘴里是两件事（一个秒醒、
   一个掉电也还在）。直接调那个 API、第一个参数传 `False` 才分得清——本机就是这种：
   `powercfg /a` 里「休眠」是可用的。

   **睡眠必须延后几秒再动手**（`SLEEP_GRACE`）：回话是 `_try_power` 返回**之后**才发
   出去的，当场就睡的话那句「✅ 好，电脑这就睡了」永远发不出去，微信那头只看见
   「说完没反应」。和关机那个 `MIN_DELAY` 同一个理由，只是那边由 `shutdown.exe` 自己等。

   **动手前先问 `powercfg /a` 能不能睡**（`sleep_states`）：本机就只有「待机(S0 低电量
   待机)」、没有 S3，别的机器还可能把休眠关了——能力先问清楚，回话才不乱承诺。
   睡眠同样是会把人挡在门外的（手机上叫不醒），所以它和关机一样有个开关
   （`power_control.allow_sleep`）。

**解锁屏幕这个功能不做了**（2026-09-03 砍掉的，别照着老注释再加回来）。锁屏界面
（LogonUI）画在 Winlogon 那张独立桌面上，桥跑在 Default 桌面，而 `SendInput` 只往调用
线程所在的那张桌面注入——要碰得着它只能借 SYSTEM 的手：一个以 SYSTEM 跑的计划任务，
加一份存在磁盘上的 PIN。代价是一次真实的提权（能往请求目录写文件的人就能开这台锁着的
电脑），而真机上试了三轮，那张安全桌面始终不接手注入的键盘。收益抵不上这份风险和复杂
度，于是整块拿掉。现在这个文件里**没有任何东西读得到 PIN，也没有任何东西碰得到
SYSTEM**；锁了就只能自己到电脑前开。

按项目规矩：零第三方依赖（ctypes + shutdown.exe）。Windows 相关的动作全在
`default_runner` 一处，测试从外面换掉它，所以 Linux 上也能自测。
"""

import logging
import os
import re
import subprocess
import threading
import time

import wemeet

log = logging.getLogger("bridge.power")

CMD_TIMEOUT = 25            # tasklist / shutdown 这类调用的上限
MAX_LEN = 24                # 比这还长的消息不当电源指令看，交给 Claude
DEFAULT_DELAY = 60          # 关机默认留这么多秒，够回话发出去、也够反悔
# 24 小时是**我们自己**定的上限，口径和定时进会一致（只管今天之内）。
# shutdown.exe 本身能吃到 315360000 秒，别再照着老注释说这是它的上限。
MAX_DELAY = 24 * 3600

# 「立刻关机」也不给 0 秒：回话是 `_try_power` 返回之后才发出去的，`/t 0` 会让
# 电脑在回话发出前就断电，用户那边看起来像桥死了。留这么多秒够一次微信往返。
MIN_DELAY = 10

# 睡眠也得等回话发出去再动手（理由见文件开头）。8 秒这么定的：关机那个 MIN_DELAY 是
# 10 秒，而这里动作本身不占时间（`SetSuspendState` 一调就进待机），8 秒够一次微信往返。
SLEEP_GRACE = 8


# ---------- 解析：什么话算关机、锁屏、睡眠 ----------
#
# 硬规矩两条，破了就会出事：
#
# 1. **认出词以后，剩下的部分必须是空的**（关机可以只剩一个时刻）。
#    「关闭电脑上的微信」里就有「关闭电脑」四个字，只看包含关系等于替用户关机。
# 2. **问句一律不认。** 「怎么关机」「关机快捷键是什么」是在问，不是在使唤。

_NOISE = " \t　:：,，、。!！?？~—-的了吧呀啊呢哦嘛吗谢谢"
_LEAD = ("请", "帮我", "帮忙", "麻烦", "给我", "替我", "你", "来", "先", "把", "给",
         "现在", "立刻", "立即", "马上", "顺手", "顺便", "就")
# 这些字一出现就交给 Claude：都是在问办法，不是在下命令
_ASK_WORDS = ("怎么", "如何", "为什么", "为啥", "什么", "咋", "教我", "能不能", "可不可以",
              "是不是", "有没有", "哪个", "哪里", "区别", "意思")
# 带上这些的一律不认：路径、链接、代码，都说明这句话在说别的事
_HARD_NO = ("http", "www.", "\\", ".py", ".md", ".txt", ".json", ".zip", ".exe",
            "密码", "压缩包", "文件", "账号", "手机", "路由", "屏保")

_LOCK_WORDS = ("锁屏", "锁定屏幕", "锁屏幕", "锁上屏幕", "屏幕锁上", "锁定电脑", "锁电脑",
               "电脑锁上", "锁上电脑", "锁一下", "锁住", "lockscreen", "lock")
_SHUTDOWN_WORDS = ("关机", "关闭电脑", "关掉电脑", "关电脑", "电脑关机", "把电脑关了",
                   "关了电脑", "shutdown", "poweroff", "power off")

# 「睡眠 / 休眠」。**「睡」这个字不能单独认**：「我要睡了」「早点睡」说的是人自己，
# 所以词表里全是带宾语的（睡眠 / 睡一觉），再靠「摘掉词以后剩下的必须是空的」兜一道。
# 「休眠和睡眠有什么区别」这种问句由 `_ASK_WORDS` 里的「区别」挡住。
_SLEEP_WORDS = ("睡眠模式", "睡眠", "休眠模式", "休眠", "待机", "睡一觉", "睡个觉",
                "sleep", "hibernate", "suspend")
_CANCEL_WORDS = ("取消关机", "别关机", "不关机", "不要关机", "先别关机", "取消定时关机",
                 "取消关闭电脑", "不关了", "先别关", "撤销关机", "cancel shutdown")
_STATUS_WORDS = ("锁了吗", "锁了没", "锁着吗", "锁没锁", "锁上了吗", "锁屏了吗",
                 "屏幕状态", "电脑锁了", "锁屏状态", "有没有锁")
# 说了这两个字就是想定个时间。摘完时间之后要是一个时刻都没找着，宁可回一句「时间呢」，
# 也不要按默认的 60 秒关机——「定时关机」和「关机」在人嘴里不是一个意思。
_SCHED_WORDS = ("定时", "自动", "预约")
# 斜杠指令：写全了就不用猜，长度和问句那几条都不查
_SLASH = {"/lock": "lock", "/锁屏": "lock", "/shutdown": "shutdown",
          "/关机": "shutdown", "/poweroff": "shutdown", "/cancelshutdown": "cancel",
          "/取消关机": "cancel", "/locked": "status", "/屏幕": "status",
          "/sleep": "sleep", "/睡眠": "sleep", "/standby": "sleep", "/待机": "sleep",
          "/hibernate": "hibernate", "/休眠": "hibernate"}

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "半": 30}
_DELAY_RE = re.compile(r"(\d{1,4})\s*(秒钟|秒|分钟|分|小时|钟头|个小时)")
_CN_DELAY_RE = re.compile(r"([一两二三四五六七八九十半])\s*(秒钟|秒|分钟|分|小时|钟头|个小时)")
_UNIT_SEC = {"秒": 1, "秒钟": 1, "分": 60, "分钟": 60,
             "小时": 3600, "个小时": 3600, "钟头": 3600}


def _clean(s):
    return (s or "").strip().strip(_NOISE).strip()


def _strip_lead(s):
    """把「帮我」「现在」这类开头的客套和副词剥掉，只留干货。"""
    h = _clean(s)
    changed = True
    while changed and h:
        changed = False
        for w in _LEAD:
            if h.startswith(w):
                h, changed = _clean(h[len(w):]), True
                break
    return h


def _too_odd(text):
    """一眼就该交给 Claude 的：太长、在问办法、带路径链接。"""
    low = (text or "").lower()
    if len(_clean(text)) > MAX_LEN:
        return True
    if any(w in low for w in _HARD_NO):
        return True
    return any(w in text for w in _ASK_WORDS)


def parse_slash(text):
    """`/lock` 这类写全了的指令。返回动作名或 None。"""
    t = _clean(text).lower().replace(" ", "")
    return _SLASH.get(t)


def _match_word(text, words):
    """认出词并且把它摘掉，返回剩下的部分；认不出返回 None。

    按长度倒序试，免得「锁屏幕」先被「锁屏」吃掉一半、剩个「幕」判成有杂字。
    """
    body = _strip_lead(text)
    low = body.lower()
    for w in sorted(words, key=len, reverse=True):
        i = low.find(w.lower())
        if i >= 0:
            return _clean(body[:i] + " " + body[i + len(w):])
    return None


def _rest_is_empty(rest):
    """摘掉关键词后剩下的东西还能不能忽略。剩一个「吧」「一下」可以，剩个名词不行。"""
    r = _clean(rest)
    for w in ("一下", "了", "吧", "呗", "咯", "喽", "屏幕", "电脑", "笔记本", "机器",
              "现在", "立刻", "马上", "立即", "好了", "掉", "上", "住", "关"):
        r = _clean(r.replace(w, " "))
    return not r


def parse_delay(text):
    """「10 分钟后」→ 600。认不出返回 None（调用方自己决定默认多少）。"""
    t = _clean(text)
    m = _DELAY_RE.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
    else:
        m = _CN_DELAY_RE.search(t)
        if not m:
            return None
        n, unit = _CN_NUM.get(m.group(1), 0), m.group(2)
        if m.group(1) == "半":            # 「半小时」是 30 分钟，不是 30 小时
            n, unit = 30, "分钟"
    if n <= 0:
        return None
    sec = n * _UNIT_SEC.get(unit, 60)
    return sec if 0 < sec <= MAX_DELAY else None


def parse_status(text):
    """「电脑锁了吗」这类问句。放在别的解析前面，因为它靠「吗」「了」认人，
    而 `_clean` 会把这两个字当尾巴剥掉——所以这里比的是**没剥过的原文**。"""
    if parse_slash(text) == "status":
        return True
    raw = (text or "").strip()
    if len(_clean(raw)) > MAX_LEN:
        return False
    low = raw.lower()
    if any(w in low for w in _HARD_NO):
        return False
    return any(w in raw for w in _STATUS_WORDS)


def parse_lock(text):
    """「锁屏」「把电脑锁上」。

    **「解锁」这类说法不用单独挡。** 解锁砍掉之后这里也不认它们，靠的还是「剩下必须
    是空的」那条：「解锁屏幕」摘掉「锁屏幕」剩个「解」、「解锁一下」剩个「解」、
    「unlock」摘掉「lock」剩个「un」，光一个「解锁」连词都对不上。认不出就照常落回
    Claude，绝不会误锁——自测里把这几句钉着。
    """
    if parse_slash(text) == "lock":
        return True
    if _too_odd(text) or parse_status(text):
        return False
    rest = _match_word(text, _LOCK_WORDS)
    return rest is not None and _rest_is_empty(rest)


def _match_sleep(text):
    """认「睡眠 / 休眠」并分清是哪一种。返回 `(kind, 摘完剩下的话)`；认不出 `(None, None)`。

    单独写一个而不是用 `_match_word`，只因为它还得**回一个种类**：睡眠和休眠是两个不同的
    动作，而 `_match_word` 只回剩下的话。

    「让电脑睡」「叫电脑睡」也是人话，可 `_LEAD` 里没有「让 / 叫」——那两个词只在电源这
    几条解析里出现，加进 `_LEAD` 会顺带改掉关机/锁屏的判定，所以就地剥一层。
    """
    body = _strip_lead(text)
    if body[:1] in ("让", "叫"):
        body = _clean(body[1:])
    low = body.lower()
    for w in sorted(_SLEEP_WORDS, key=len, reverse=True):
        i = low.find(w.lower())
        if i >= 0:
            kind = "hibernate" if ("休眠" in w or w == "hibernate") else "sleep"
            return kind, _clean(body[:i] + " " + body[i + len(w):])
    return None, None


def parse_sleep(text):
    """「睡眠」「休眠」→ `"sleep"` / `"hibernate"`；认不出返回 None。

    和锁屏同一条规矩（摘掉词以后剩下的必须是空的、问句一律不认、太长的不看），因为
    **认错的代价是「说一句话电脑就睡了，而我在手机那头叫不醒它」**。
    """
    want = parse_slash(text)
    if want in ("sleep", "hibernate"):
        return want
    if _too_odd(text) or parse_status(text):
        return None
    kind, rest = _match_sleep(text)
    if kind is None or not _rest_is_empty(rest):
        return None
    return kind


def parse_cancel_shutdown(text):
    """「取消关机」「先别关机」。必须比 parse_shutdown 先问，它俩都含「关机」。"""
    if parse_slash(text) == "cancel":
        return True
    t = _clean(text)
    if len(t) > MAX_LEN:
        return False
    return any(w in t for w in _CANCEL_WORDS)


def _take_time(rest, now):
    """从「关机」剩下的那半句里摘掉时间说法。返回 `(plan 或 None, 剩下的话)`。

    **钟点必须比这里的 `_DELAY_RE` 先试，顺序换了就会出事**：「三点四十分关机」里
    藏着「十分」两个字，`_CN_DELAY_RE` 会把它当成 10 分钟——于是用户说的 15:40 变成
    了「十分钟后关机」，差两个多钟头，而关机是不可逆的。`wemeet` 那套要求相对时长
    带「后」字（`_REL_RE`），所以它分得清「三点四十分」和「十分钟后」；这里的
    `_DELAY_RE` 不要求「后」（为了认「定时关机 20 分钟」这种写法），分不清，只能靠
    顺序让开。

    于是分工是：**能让 wemeet 认的一律归它**（当天钟点、「N 分钟后」、「一个半小时
    后」，越界的还会给个 `far`），它一个字都没认出来时才轮到 `parse_delay`——后者管
    的是 wemeet 不收的那两类：秒（「45 秒后关机」）和不带「后」的写法。
    """
    base = time.time() if now is None else now
    span = wemeet.match_time_span(rest)
    if span:
        ts, label, bad = wemeet.parse_when(rest, base)
        left = _clean(rest.replace(span, " "))
        if bad:
            return {"problem": bad, "label": label}, left
        if ts is not None:
            return {"delay": int(round(ts - base)), "label": label}, left
        # 认出了形状但算不出时刻（「99 点」这类），当没看见，交回 Claude
        return None, rest
    sec = parse_delay(rest)
    if sec is not None:
        return ({"delay": sec},
                _CN_DELAY_RE.sub(" ", _DELAY_RE.sub(" ", rest)))
    return None, rest


def parse_shutdown(text, now=None):
    """「关机」「十分钟后关机」「晚上十点关机」。认不出返回 None，认出来返回一个 plan。

    plan 有四种形状，后三种也算「这件事我处理了」——1 秒回一句比落回 Claude 有用，
    Claude 那边没有 Bash 权限，落过去必然失败，等于白花一次额度：

        {"delay": 秒 或 None, "label": 可选}   照它安排；delay=None 用配置里的默认值
        {"problem": "past",  "label": "22:00"} 那个点今天已经过去了（**不顺延到明天**）
        {"problem": "far",   "label": …}       超过 24 小时，不接
        {"problem": "notime"}                  说了「定时」却没给时间

    `now` 只有测试会传（钟点解析依赖墙上时钟，不传的话用例会跟着当前时间飘）。
    """
    if parse_slash(text) == "shutdown":
        return {"delay": None}
    if _too_odd(text) or parse_status(text) or parse_cancel_shutdown(text):
        return None
    rest = _match_word(text, _SHUTDOWN_WORDS)
    if rest is None:
        return None
    # 「定时」这两个字下面就被当噪音剥掉了，所以得先记下来它出现过
    asked_sched = any(w in rest for w in _SCHED_WORDS)
    plan, rest = _take_time(rest, now)
    for w in ("定时", "自动", "预约", "之后", "以后", "后", "再"):
        rest = _clean(rest.replace(w, " "))
    if not _rest_is_empty(rest):
        return None
    if plan is not None:
        return plan
    # 光说「定时关机」不给时间：别按默认 60 秒关，那不是他的意思
    return {"problem": "notime"} if asked_sched else {"delay": None}


# ---------- 真正动手的那一层：全在这儿，测试从外面整块换掉 ----------

def _no_window():
    """别弹黑框。Linux 上没这个标志，给 0。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _console_decode(raw):
    """控制台程序的输出按**本机 ANSI 代码页**解（中文 Windows 是 cp936），退不回再当 UTF-8。

    **不能一律按 UTF-8 解**：`powercfg /a`、`tasklist` 是控制台程序，输出走本地代码页。
    真机上就踩到这个坑——`powercfg /a` 明明写着「待机」「休眠」可用，UTF-8 一解中文整段
    变成空白，`sleep_states` 一个关键词都认不出来，`check` 于是报「本机没有待机、没有休眠」。
    这种「明明有却说没有」比报错更坏：人会照着它去改本来没错的东西。
    """
    for enc in (("mbcs",) if os.name == "nt" else ()) + ("utf-8",):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def run_cmd(args, timeout=CMD_TIMEOUT):
    """跑一条命令，回 (返回码, 输出)。编码解不动就丢坏字节，不许因此抛异常。"""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, creationflags=_no_window())
    except subprocess.TimeoutExpired:
        return 258, "timeout"
    except OSError as e:
        return -1, str(e)
    out = _console_decode(p.stdout or b"").strip()
    return p.returncode, out


def lock_now():
    """user32!LockWorkStation。当前用户就能调，不用管权限。"""
    import ctypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    if not u.LockWorkStation():
        raise OSError("LockWorkStation 失败，GetLastError=%d" % ctypes.get_last_error())
    return True


def is_locked_now():
    """锁着没有：看 LogonUI.exe 在不在。锁屏界面就是它画的，它在就等于锁着。

    比 `WTSQuerySessionInformation` 省事，也不用管会话号。开销约 0.3 秒。
    """
    rc, out = run_cmd(["tasklist", "/FI", "IMAGENAME eq LogonUI.exe", "/NH"], timeout=15)
    if rc != 0:
        raise OSError("tasklist 失败（rc=%s）：%s" % (rc, out[:120]))
    return "logonui" in out.lower()


def shutdown_now(delay):
    """shutdown.exe /s /t N。

    **不写 `/f` 不等于不强制**：`shutdown /?` 里写着「`/t` 大于 0 时隐含 `/f`」，而这里
    的 N 永远 ≥ MIN_DELAY，所以到点是强制关，没保存的东西不会弹窗问。真想让应用有机会
    拦一下，只有 `/t 0` 那一条路，而那样回话发不出去（桥自己先被杀）。
    """
    rc, out = run_cmd(["shutdown", "/s", "/t", str(int(delay))], timeout=20)
    if rc != 0:
        raise OSError("shutdown 失败（rc=%s）：%s" % (rc, out[:200]))
    return True


def cancel_shutdown_now():
    """shutdown /a。**没有待执行的关机时它也返回非 0**（1116），那不算错。"""
    rc, out = run_cmd(["shutdown", "/a"], timeout=20)
    if rc == 0:
        return True
    if "1116" in out or "no shutdown" in out.lower():
        return False                    # 本来就没安排关机
    raise OSError("shutdown /a 失败（rc=%s）：%s" % (rc, out[:200]))


# `powercfg /a` 的两段标题（系统是中文就用前者，英文系统用后者）。**必须按这两段切**：
# 不可用的那一段里也会出现「待机」「休眠」这些词——本机的输出里
# 「待机(S0 低电量待机) 连接的网络」就落在「没有以下睡眠状态」那一段（被策略禁掉了），
# 全文搜会把禁掉的状态读成可用。
_STATES_OK = ("此系统上有以下睡眠状态", "The following sleep states are available")
_STATES_NO = ("此系统上没有以下睡眠状态",
              "The following sleep states are not available")


def sleep_states(text):
    """从 `powercfg /a` 的输出里读出睡眠／休眠能不能用。返回 `{"sleep", "hibernate"}`。"""
    cut = len(text or "")
    for mark in _STATES_NO:
        i = text.find(mark)
        if i >= 0:
            cut = min(cut, i)
    head = (text or "")[:cut]           # 只认「可用」那一段
    return {"sleep": ("待机" in head or "Standby" in head),
            "hibernate": ("休眠" in head or "Hibernate" in head)}


def read_sleep_states():
    """跑一次 `powercfg /a` 再解。跑不动就抛，由门面接住（约 0.2 秒）。"""
    rc, out = run_cmd(["powercfg", "/a"], timeout=15)
    if rc != 0:
        raise OSError("powercfg /a 失败（rc=%s）：%s" % (rc, out[:120]))
    return sleep_states(out)


def sleep_now():
    """进睡眠（本机就是 S0 低电量待机）。

    `powrprof!SetSuspendState(False, False, False)`：**第一个参数 False 才是睡眠**。
    那条流传很广的 `rundll32 powrprof.dll,SetSuspendState 0,1,0` 不能用——开着休眠的
    机器上它会真的休眠下去，而「睡眠」（秒醒）和「休眠」（掉电也还在）是两回事。
    """
    import ctypes
    p = ctypes.WinDLL("powrprof", use_last_error=True)
    if not p.SetSuspendState(False, False, False):
        raise OSError("SetSuspendState 失败，GetLastError=%d" % ctypes.get_last_error())
    return True


def hibernate_now():
    """`shutdown /h`。休眠没开（`powercfg /h off`）时它返回非 0，照实报出去。"""
    rc, out = run_cmd(["shutdown", "/h"], timeout=20)
    if rc != 0:
        raise OSError("shutdown /h 失败（rc=%s）：%s" % (rc, out[:200]))
    return True


def _suspend_later(kind, seconds):
    """等 `seconds` 秒再真睡。**必须延后**，理由见文件开头（回话得先发出去）。

    用守护线程，和 `app_actions.MediaControl.run_later` 一个套路：桥要是在这几秒里退出
    了，这一觉就不睡了——用户再说一句就是了，比「回话发不出去」轻得多。
    """
    def job():
        try:
            time.sleep(max(0, seconds))
            (hibernate_now if kind == "hibernate" else sleep_now)()
        except Exception as exc:                      # noqa: BLE001
            log.warning("%s 没成（回话已经发出去了，只能记在这儿）：%s", kind, exc)
    t = threading.Timer(max(0, seconds), job)
    t.daemon = True
    t.start()
    return True


def default_runner(action, arg=0):
    """动作名 → 真干活。测试把整个函数换掉，于是一行 Windows 调用都不跑。"""
    if action == "lock":
        return {"ok": True, "locked": bool(lock_now())}
    if action == "status":
        return {"ok": True, "locked": is_locked_now()}
    if action == "shutdown":
        return {"ok": True, "delay": int(arg), "armed": bool(shutdown_now(arg))}
    if action == "cancel":
        return {"ok": True, "aborted": cancel_shutdown_now()}
    if action == "states":
        st = read_sleep_states()
        return {"ok": True, "sleep": st["sleep"], "hibernate": st["hibernate"]}
    if action in ("sleep", "hibernate"):
        return {"ok": True, "scheduled": _suspend_later(action, int(arg) or SLEEP_GRACE)}
    raise ValueError("不认识的电源动作：%r" % (action,))


# ---------- 门面 ----------

class PowerControl(object):
    """锁屏 / 查状态 / 关机 / 取消关机的门面。每个方法回 `(话, 处理了没)`。

    处理了没 = False 的意思是「这件事我做不了，把消息交给 Claude」，
    和 MediaControl、VolumeMixer 一个约定。
    """

    def __init__(self, runner=None, shutdown_delay=DEFAULT_DELAY, allow_shutdown=True,
                 allow_sleep=True):
        self.runner = runner or default_runner
        self.shutdown_delay = max(MIN_DELAY, min(MAX_DELAY, int(shutdown_delay or 0)))
        self.allow_shutdown = bool(allow_shutdown)
        # 睡眠同样是「把人挡在门外」的动作（手机上叫不醒它），所以单独一个开关
        self.allow_sleep = bool(allow_sleep)

    def _call(self, action, arg=0):
        """runner 抛什么都不许往上冒：电源这层挂了不该拖死整条消息。"""
        try:
            res = self.runner(action, arg)
        except Exception as e:
            log.exception("电源动作 %s 失败", action)
            return {"ok": False, "error": str(e)}
        if not isinstance(res, dict):
            return {"ok": False, "error": "runner 返回的不是字典：%r" % (res,)}
        return res

    # ---------- 查状态 ----------

    def status(self):
        r = self._call("status")
        if not r.get("ok"):
            return "❌ 看不出锁没锁：%s" % r.get("error", "未知原因"), True
        locked = bool(r.get("locked"))
        return ("✅ 屏幕现在是锁着的。" if locked else "✅ 屏幕没锁，是能直接用的状态。"), True

    # ---------- 锁屏 ----------

    def lock(self):
        """锁屏。一句 `LockWorkStation`，没有别的动作。

        **锁上就只能自己去电脑前开**：解锁那条路（SYSTEM 计划任务 + 存在盘上的 PIN）
        2026-09-03 整块砍掉了，理由见文件开头。所以回话里把这句说清楚，别让人以为
        一句「解锁」就能回来。
        """
        r = self._call("lock")
        if not r.get("ok"):
            return "❌ 锁屏没成：%s" % r.get("error", "未知原因"), True
        return "✅ 屏幕已锁。开回来得你自己到电脑前解锁。", True

    # ---------- 睡眠 / 休眠 ----------

    def sleep(self, kind="sleep"):
        """睡眠 / 休眠。**先回话、再动手**（理由见文件开头的 `SLEEP_GRACE`）。

        动手之前先问一次 `powercfg /a`：本机只有 S0 低电量待机，别的机器还可能把休眠关了，
        能力先问清楚才不乱承诺。这两件事**都不给时间参数**——「多久之后睡」没有意义
        （到点人不在，睡了也一样），要定时就用「十分钟后关机」那条路。
        """
        if not self.allow_sleep:
            return "❌ 睡眠/休眠被关掉了（config.json 里 power_control.allow_sleep）。", True
        st = self._call("states")
        if not st.get("ok"):
            return "❌ 看不出这台机器能不能睡：%s" % st.get("error", "未知原因"), True
        if kind == "hibernate" and not st.get("hibernate"):
            return ("❌ 这台机器现在不能休眠（`powercfg /a` 里「休眠」不在「此系统上有以下"
                    "睡眠状态」那一段）。想开的话在电脑上跑一次 `powercfg /hibernate on`。",
                    True)
        if kind == "sleep" and not st.get("sleep"):
            return ("❌ 这台机器现在没有可用的睡眠状态（`powercfg /a` 里「待机」不在"
                    "「此系统上有以下睡眠状态」那一段）。", True)
        r = self._call(kind, SLEEP_GRACE)
        if not r.get("ok"):
            return "❌ 睡眠没安排上：%s" % r.get("error", "未知原因"), True
        what = "休眠" if kind == "hibernate" else "睡眠"
        return ("✅ 好，电脑 %d 秒后进入%s；回话先发给你。唤醒按一下电源键或者键盘"
                "——手机上叫不醒它。" % (SLEEP_GRACE, what), True)


    # ---------- 关机 ----------

    def shutdown(self, delay=None, problem=None, label=None):
        """安排关机。参数直接来自 `parse_shutdown` 的 plan（见那儿的四种形状）。

        `problem` 那三种都不动 shutdown.exe，只回一句话——但仍然算「处理了」。
        `label` 是解析出来的钟点（「22:00」），有就报出来：**用户说的是「十点」，
        到底被理解成 10 点还是 22 点，只有回话里把结果写明白他才纠得过来。**
        """
        if not self.allow_shutdown:
            return "❌ 关机被关掉了（config.json 里 power_control.allow_shutdown）。", True
        if problem == "past":
            return ("❌ 今天的 %s 已经过去了，没给你定——它不会顺延到明天。要现在关就说"
                    "「关机」。" % (label or "那个点"), True)
        if problem == "far":
            return "❌ 只认 24 小时以内的时间，这个太远了。", True
        if problem == "notime":
            return ("❌ 「定时关机」得带上时间：「十点关机」「晚上十点半关机」「十分钟后"
                    "关机」都认。想现在就关的话直接说「关机」。", True)
        sec = self.shutdown_delay if delay is None else max(MIN_DELAY, min(MAX_DELAY, int(delay)))
        r = self._call("shutdown", sec)
        if not r.get("ok"):
            return "❌ 关机没安排上：%s" % r.get("error", "未知原因"), True
        if label:
            return ("✅ 已安排 %s 关机（还有 %s）。到点是强制关，没保存的东西不会弹窗问你。"
                    "反悔就说「取消关机」。" % (label, _dur(sec)), True)
        return "✅ 已安排 %s后关机。反悔就说「取消关机」。" % _dur(sec), True

    def cancel_shutdown(self):
        r = self._call("cancel")
        if not r.get("ok"):
            return "❌ 取消关机没成：%s" % r.get("error", "未知原因"), True
        if r.get("aborted"):
            return "✅ 关机已取消。", True
        return "🔕 本来就没安排关机，不用取消。", True


# ---------- 回话用的小工具 ----------

def _dur(sec):
    """60 → 「1 分钟」，90 → 「1 分 30 秒」。回话里不写「60 秒」这种别扭说法。

    数字和单位之间留一个空格是项目里通行的写法，但**后面不要再补空格**：
    调用处写「已安排 %s后关机」而不是「%s 后关机」，否则出来的是「1 分钟 后关机」。
    """
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec % 3600 == 0:
        return "%d 小时" % (sec // 3600)
    if sec >= 3600:
        return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)
    if sec % 60 == 0:
        return "%d 分钟" % (sec // 60)
    return "%d 分 %d 秒" % (sec // 60, sec % 60)


# ---------- 命令行：不开桥也能验一下这一层 ----------

def _cli(argv):
    args = list(argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("用法：python power.py [--status|--lock|--sleep|--hibernate|--states|"
              "--cancel|--parse 一句话]")
        print("      --sleep / --hibernate 是**真的让电脑睡**（8 秒后生效，"
              "唤醒按电源键或键盘）")
        return 0
    p = PowerControl()
    if args[0] == "--parse":
        for text in args[1:]:
            print("%-16s slash=%-9s lock=%-5s status=%-5s sleep=%-9s cancel=%-5s "
                  "shutdown=%s" % (text, parse_slash(text), parse_lock(text),
                                   parse_status(text), parse_sleep(text),
                                   parse_cancel_shutdown(text), parse_shutdown(text)))
        return 0
    if args[0] == "--states":
        # 只读：问一次 `powercfg /a`（发「睡眠」之前先看这个，别拿自己的电脑试）
        st = read_sleep_states()
        for k in ("sleep", "hibernate"):
            print("%s：%s" % (k, "可用" if st[k] else "不可用"))
        return 0
    todo = {"--status": p.status, "--lock": p.lock,
            "--sleep": lambda: p.sleep("sleep"),
            "--hibernate": lambda: p.sleep("hibernate"),
            "--cancel": p.cancel_shutdown}.get(args[0])
    if todo is None:
        print("不认识：%s" % args[0])
        return 2
    print(todo()[0])
    return 0


if __name__ == "__main__":
    import sys
    import bridge_config
    # 回话里有 ✅/❌/🔕 这些记号，GBK 控制台直接打印会抛 UnicodeEncodeError——而
    # `--status` 恰恰是「从微信发关机之前先在电脑上确认状态」那一步，不能崩在这儿
    bridge_config.make_console_safe()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(_cli(sys.argv))

