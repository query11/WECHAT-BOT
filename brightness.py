# -*- coding: utf-8 -*-
"""屏幕亮度：「亮度调到 50%」「亮度多少」「屏幕亮一点」。

**只做「读」和「设绝对值」**，靠的是 WMI（`root\\wmi` 里的
`WmiMonitorBrightness` 读、`WmiMonitorBrightnessMethods.WmiSetBrightness` 写）。
这条路 2026-09-27 在这台机器上实测量过：读 90 → 写 88 → 再读 88 → 写回 90，
一个来回都对得上。

几件要知道的事：

1. **WMI 这条路只管笔记本的内屏**，也就是背光归系统管的那种屏。外接显示器要
   DDC/CI（`dxva2.dll` 那套 P/Invoke），那个跨显示器型号五花八门，不是这个项目
   该去碰的；真接了外屏也只改内屏，回话里把这件事说清楚（`monitors` 会报几块屏）。
2. **PowerShell 这条路每次调用约 1 秒**（不走 `Add-Type` 编 C#，所以比音量那条
   1~3 秒快）。和音量一样跑在轮询线程上，期间收不到新消息——别挪进循环里连着调。
3. **回话里的数字一律是「读回来的」而不是「让它设的」**：`after` 是设完再读一次的
   真值。音量那边同理（见 volume.py 那段注释），人信的是屏幕上真的变没变。
4. **不给 0**：`MIN_BRIGHTNESS` 是 5。说是「调到 0%」，屏幕就真的一片黑，而人还在
   手机那头——那种情况下他连「再调回 80」都发不出去（这条指令是电脑上的桥在跑，
   屏幕黑不影响微信，但人已经看不见屏幕了、也没法确认到底设成没有）。
   所以下限留在 5%，回话里写明「你说的是 0%，我停在 5%」。
5. 脚本正文里**一个中文都不写**（沿用项目规矩），中文只在 Python 这边拼。
"""

import base64
import json
import logging
import os
import re
import subprocess

log = logging.getLogger("bridge.brightness")

PS_TIMEOUT = 25          # 起一次 PowerShell + 问 WMI，给宽一点
MAX_LEN = 30             # 比这还长的消息不当亮度指令看，交给 Claude
MIN_BRIGHTNESS = 5       # 下限（理由见文件开头第 4 条）
MAX_BRIGHTNESS = 100
DEFAULT_STEP = 10        # 「亮一点」一次调多少

# ---------- 解析：什么话算「设成某个值」、什么话算「现在多少」 ----------
#
# 硬规矩和音量那边一样：**句子里必须有亮度词或「屏幕」**。「调到 30%」光看这几个字
# 可能在说音量、缩放、进度条，不带锚点一律交给 Claude。
#
# 但「屏幕」当锚点要更严一点，因为带「屏幕」的话太多：「屏幕状态」「屏幕截图」
# 「屏幕锁了吗」都带。所以三个解析各自还有自己的第二道门槛（要有数字 / 问句词 /
# 亮暗词），光有「屏幕」两个字什么都不算——那个字还有个主人（power 的 /屏幕 查锁屏）。

_BRIGHT_WORDS = ("亮度", "brightness", "背光")
_SCREEN_WORDS = ("屏幕", "显示屏")
_NOISE = " \t　:：,，、。!！?？~的了吧呀啊呢哦嘛谢谢"
_LEAD = ("请", "帮我", "帮忙", "麻烦", "给我", "你", "来", "先", "把", "现在", "让")
# 「调到」这类**多字**动词才算锚点。光一个「调」「改」不算——「30 分钟后把亮度调好」
# 里就有个「调」，认了就等于把时间当成亮度。
_SET_HINTS = ("调到", "调成", "调至", "调节到", "设到", "设成", "设为", "设置成",
              "改成", "改到", "=")
_QUERY_WORDS = ("多少", "多大", "多亮", "现在是")
# 「太亮了」「有点暗」「刺眼」这些是在嫌，不是在下令——**但要连上锚点才算**
# （「屏幕太亮了」认，「太亮了」不认：那可能是在说台灯）。
# **不收光秃秃的「亮点」「暗点」**：中文里「亮点」多半是「精彩的
# 那个点」（「屏幕亮点是什么」），认了就成了「把屏幕调亮」。
_UP_WORDS = ("亮一点", "亮一些", "亮些", "调亮", "亮一丢丢", "太暗", "有点暗",
             "太黑了", "看不清", "看不见", "bright up")
_DOWN_WORDS = ("暗一点", "暗一些", "暗些", "调暗", "太亮", "有点亮", "刺眼",
               "晃眼", "bright down")
_MAX_WORDS = ("最亮", "最高", "最大", "拉满", "开到最亮", "max")
_MIN_WORDS = ("最暗", "最黑", "最低", "最小", "min")
_HALF_WORDS = ("一半", "百分之五十")       # 「50」不放这儿：那样会绕过下面的挨着检查
# 数字后面紧跟这些词，说明那个数字在说别的事（时间、次数、倍数、眼睛度数），一律不认
_UNIT_AFTER = ("分钟", "小时", "秒", "天", "点", "分", "次", "遍", "倍", "个", "月",
               "号", "首", "条", "寸", "寸屏", "度", "元")
_NUM_RE = re.compile(r"(\d{1,3})\s*%?")


def _clean(s):
    return (s or "").strip().strip(_NOISE).strip()


def _strip_lead(s):
    h = _clean(s)
    changed = True
    while changed and h:
        changed = False
        for w in _LEAD:
            if h.startswith(w):
                h, changed = _clean(h[len(w):]), True
                break
    return h


def _usable(body):
    """长度、外链这类硬门槛，外加**必须带亮度锚点**。返回处理过的小写正文。"""
    text = (body or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    low = _strip_lead(text).lower()
    if not low or "http" in low or "\\" in low or "/dm/" in low:
        return None
    if not any(w in low for w in _BRIGHT_WORDS + _SCREEN_WORDS):
        return None
    return low


def _anchored(low, i, end):
    """数字必须**挨着**亮度词或「调到」这类动词才算数。

    和音量那边同一条：不带它，「30 分钟后把亮度调好」「第 3 集屏幕太亮」都会被当成
    设成某个百分比。
    """
    for w in _BRIGHT_WORDS + _SCREEN_WORDS + _SET_HINTS:
        p = low.find(w)
        while p >= 0:
            q = p + len(w)
            if 0 <= i - q <= 4:            # 锚点在前、数字紧跟：「亮度调到 50」
                return True
            if 0 <= p - end <= 1:          # 数字在前、紧接着锚点：「50% 亮度」
                return True
            p = low.find(w, p + 1)
    return False


def parse_set(body):
    """「亮度调到 50%」→ 50。认不出返回 None（交给 Claude，绝不瞎猜）。

    只认 0~100 的阿拉伯数字，外加「最亮/最暗/一半」。**句子里出现两个数字就不猜**
    ——「亮度从 30 调到 50」看着清楚，但也可能是「亮度 30 分钟后调 50」这类话，
    宁可漏认。数字后面跟着「分钟」「度」这类量词的也一律不认。
    """
    low = _usable(body)
    if not low:
        return None
    # 「最亮/最高/一半」这类只有**连着亮度词**才算数：「屏幕最高」可能是在说窗口，
    # 少这一道就会把一句不相干的话当成了「调最亮」。
    if any(w in low for w in _BRIGHT_WORDS):
        if any(w in low for w in _MAX_WORDS):
            return MAX_BRIGHTNESS
        if any(w in low for w in _MIN_WORDS):
            return MIN_BRIGHTNESS
        if any(w in low for w in _HALF_WORDS):
            return 50
    nums = _NUM_RE.findall(low)
    if len(nums) != 1:
        return None                      # 一个数字都没有，或者不止一个
    try:
        val = int(nums[0])
    except ValueError:
        return None
    if not 0 <= val <= 100:
        return None                      # 「亮度 300」不像在说百分比
    i = low.find(nums[0])
    end = i + len(nums[0])
    rest = low[end:end + 4].lstrip(" %　")
    if any(rest.startswith(u) for u in _UNIT_AFTER):
        return None                      # 「亮度 30 分钟」在说时间
    if "%" in low:
        return val
    return val if _anchored(low, i, end) else None


def parse_query(body):
    """「亮度多少」这类问句。是就返回 True。"""
    low = _usable(body)
    if not low:
        return False
    if parse_set(body) is not None or parse_nudge(body):
        return False                     # 那两条是指令，不是问句
    # 「屏幕多亮」「屏幕亮不亮」这种直接问亮暗的，认。
    if "多亮" in low or "亮不亮" in low or "亮吗" in low:
        return True
    # **光有「屏幕」不算亮度问句**：分辨率、截图、状态那句话都带这两个字
    # （「屏幕分辨率多少」要是被认成亮度，回的是「屏幕亮度 90%」，答非所问）。
    # 所以这里要求先出现亮度词（「亮度」「背光」「brightness」）。
    if not any(w in low for w in _BRIGHT_WORDS):
        return False
    # 光说「亮度」「屏幕亮度」「显示屏亮度」就是在问现在多少：把这几个词全剥掉、
    # 什么都不剩才算；带别的词的（「亮度多少」）看问句词。
    bare = low
    for w in _BRIGHT_WORDS + _SCREEN_WORDS:
        bare = bare.replace(w, "")
    if not bare:
        return True
    return any(w in low for w in _QUERY_WORDS)


def parse_nudge(body):
    """「屏幕亮一点」→ +1，「屏幕太亮了」→ -1；认不出返回 0。

    返回的是**方向**，步长由调用方按配置给（`DEFAULT_STEP`）。方向和幅度分开，
    是因为「亮一点」和「亮一丢丢」在人嘴里不一样，但我们不装作能分清。
    """
    low = _usable(body)
    if not low:
        return 0
    if parse_set(body) is not None:
        return 0                         # 给了明确数字就别再猜方向
    for w in _UP_WORDS:
        if w in low:
            return 1
    for w in _DOWN_WORDS:
        if w in low:
            return -1
    return 0


# ---------- PowerShell + WMI 那一层 ----------
#
# 输出统一是 base64(UTF-8 JSON)：Windows 的报错信息常常是中文，控制台编码不可靠，
# 编一层就不用管它（和 volume.py 一样）。

_PS = """$ErrorActionPreference="Stop"
try {
  $act="__ACTION__"
  $arg=__ARG__
  $min=__MIN__
  $max=__MAX__
  $b=@(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness -ErrorAction Stop)
  $before=[int]$b[0].CurrentBrightness
  $mon=[int]$b.Count
  $mm=@(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop)
  $set=0
  if($act -eq "set"){
    $t=$arg
    if($t -lt $min){$t=$min}
    if($t -gt $max){$t=$max}
    foreach($x in $mm){
      $null=Invoke-CimMethod -InputObject $x -MethodName WmiSetBrightness -Arguments @{Brightness=[uint32]$t;Timeout=[uint32]0}
      $set=$set+1
    }
    Start-Sleep -Milliseconds 400
  }
  $a=@(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness -ErrorAction Stop)
  $r=@{ok=$true;before=$before;after=[int]$a[0].CurrentBrightness;monitors=$mon;
       methods=$mm.Count;set=$set}
} catch {
  $r=@{ok=$false;error=$_.Exception.Message}
}
$j=$r|ConvertTo-Json -Compress
[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)))
"""

ACTIONS = ("get", "set")


def build_script(action, arg=0, lo=MIN_BRIGHTNESS, hi=MAX_BRIGHTNESS):
    """拼脚本。**action 必须是上面两个之一、arg/lo/hi 必须是整数**——这是唯一几处
    外部输入能碰到脚本正文的地方，都在这里卡死，用户的原话永远进不来。"""
    if action not in ACTIONS:
        raise ValueError("未知的亮度动作：%r" % (action,))
    return (_PS.replace("__ACTION__", action)
               .replace("__ARG__", str(int(arg)))
               .replace("__MIN__", str(int(lo)))
               .replace("__MAX__", str(int(hi))))


def _run_powershell(action, arg=0, lo=MIN_BRIGHTNESS, hi=MAX_BRIGHTNESS):
    """跑一次，返回解好的 dict。失败一律抛异常——上层照实说出来。"""
    if os.name != "nt":
        raise RuntimeError("这台机器不是 Windows，读不了屏幕亮度")
    script = build_script(action, arg, lo, hi)
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
    proc = subprocess.run(cmd, capture_output=True, timeout=PS_TIMEOUT, shell=False)
    raw = (proc.stdout or b"").decode("ascii", "ignore").strip()
    if not raw:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError("PowerShell 没输出（返回码 %s）：%s"
                           % (proc.returncode, err[:200] or "stderr 也是空的"))
    obj = json.loads(base64.b64decode(raw).decode("utf-8", "replace"))
    if not isinstance(obj, dict):
        raise RuntimeError("亮度那条路返回的不是预期格式：%s" % (raw[:120],))
    if not obj.get("ok"):
        raise RuntimeError(str(obj.get("error") or "没说为什么失败"))
    return obj


# ---------- 怎么回话 ----------
#
# 和音量那边一个口径：**回话里的数字一律是读回来的**（`after`），不是我们让它设的。
# 「你说的是 0%，我停在 5%」也要写出来——那是下限在起作用，不说的话用户会以为坏了。

def say_query(r):
    n = int(r.get("monitors") or 1)
    tail = "（这台机器上认到 %d 块屏）" % n if n != 1 else ""
    return "✅ 屏幕亮度现在 %d%%。%s" % (r["after"], tail)


def say_set(r, asked=None):
    txt = "✅ 屏幕亮度：%d%% → %d%%。" % (r["before"], r["after"])
    if asked is not None and abs(int(asked) - r["after"]) > 1:
        txt += "（你说的是 %d%%，屏幕实际落在 %d%%）" % (int(asked), r["after"])
    return txt


def say_nudge(r):
    if r["before"] == r["after"]:
        edge = ("已经是 100%（最亮）" if r["after"] >= MAX_BRIGHTNESS else
                "已经是最低下限 %d%%（再低就看不清了）" % MIN_BRIGHTNESS
                if r["after"] <= MIN_BRIGHTNESS else "没动（屏幕没接住？）")
        return "🔕 %s。" % edge
    return "✅ 屏幕亮度：%d%% → %d%%。" % (r["before"], r["after"])


class BrightnessControl(object):
    """读数 / 设值 / 相对调整。每个方法回 `(话, 处理了没)`。

    处理了没 = False 的意思是「这件事我做不了，把消息交给 Claude」——和
    MediaControl、VolumeMixer、PowerControl 一个约定。注意这里**没有按键兜底**
    （亮度键的虚拟码在各家键盘上不统一，不像媒体键那样有标准），所以失败就是失败，
    照实说出来，不假装已经调过了。
    """

    def __init__(self, runner=None, step=DEFAULT_STEP):
        self.runner = runner or self._default_runner
        self.step = max(1, min(50, abs(int(step or DEFAULT_STEP))))

    @staticmethod
    def _default_runner(action, arg, lo, hi):
        return _run_powershell(action, arg, lo, hi)

    def _ask(self, action, arg=0):
        """问一次。失败不抛，回 None 并把原因记在日志里——上层统一说「没成」。"""
        try:
            return self.runner(action, int(arg), MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        except Exception as exc:                      # noqa: BLE001
            log.warning("亮度动作 %s 失败：%s", action, exc)
            self.last_error = str(exc)
            return None

    def get(self):
        """读现在的亮度。读不到返回 None。"""
        return self._ask("get")

    def set(self, percent):
        """设成某个值（下限 MIN_BRIGHTNESS，上限 100）。"""
        return self._ask("set", max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS,
                                                        int(percent))))

    # ---- 回话（`_try_brightness` 直接调这三个） ----

    def query_text(self):
        r = self.get()
        if r is None:
            return "❌ 读不到屏幕亮度：%s" % (self.last_error or "未知原因"), True
        return say_query(r), True

    def set_text(self, percent):
        r = self.set(percent)
        if r is None:
            return "❌ 亮度没调成：%s" % (self.last_error or "未知原因"), True
        return say_set(r, percent), True

    def nudge_text(self, direction):
        cur = self.get()
        if cur is None:
            return "❌ 读不到屏幕亮度：%s" % (self.last_error or "未知原因"), True
        r = self.set(int(cur["after"]) + direction * self.step)
        if r is None:
            return "❌ 亮度没调成：%s" % (self.last_error or "未知原因"), True
        return say_nudge(r), True
