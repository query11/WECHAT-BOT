# -*- coding: utf-8 -*-
"""绝对音量：读系统主音量、直接设到某个百分比、真正分得清静音和取消静音。

app_actions.py 里的音量键只能相对调（一下 2%），也读不到当前值——这是键本身的限制。
这个文件走 Core Audio 的 `IAudioEndpointVolume`，于是能做键做不到的三件事：
知道现在是多少、直接设到 30%、真的「取消静音」（而不是切一下开关碰运气）。

**这一层是加分项，不是必需品。** 任何一步失败（COM 不给、C# 编译不过、PowerShell 被策略
挡住）都会退回音量键，所以 `MediaControl(mixer=...)` 里的 mixer 挂了不影响原有功能。

两个实现上的取舍：

1. **走 `-EncodedCommand` 而不是 `-Command`。** 别的模块用 `-Command` + `$q=[char]34`
   躲双引号，但这里要塞一整段 C#（`[Guid("...")]` 到处是双引号），拼字符串会变成一团。
   `-EncodedCommand` 收的是 UTF-16LE 的 base64，脚本正文根本不经过命令行解析，双引号和
   编码问题一次消掉。**脚本里仍然一个中文都不写**，中文只在 Python 这边拼——沿用项目规矩。
2. **每次调用都重新起 PowerShell 并 `Add-Type` 编译一遍**，所以一次调用大约 1~3 秒
   （首次更慢，csc.exe 要跑）。这条和 app_launcher 解析快捷方式一样跑在轮询线程上，
   期间收不到新消息。可以接受，但**别把它挪进循环里连着调**。

`IAudioEndpointVolume` 的方法顺序是接口契约的一部分：COM 靠 vtable 位置调用，不靠名字。
所以下面那 18 个方法一个都不能删、不能换位置，哪怕根本用不到——删一个，后面全部错位，
调用 `SetMute` 实际执行的会是别的东西。
"""

import base64
import json
import logging
import os
import re
import subprocess

log = logging.getLogger("bridge.volume")

PS_TIMEOUT = 25          # 含 C# 编译，给宽一点；超了就退回按键
MAX_LEN = 30             # 比这还长的消息不当音量指令看，交给 Claude

# ---------- 解析：什么话算「设成某个值」、什么话算「现在多少」 ----------
#
# 硬规矩：句子里必须出现音量词。「调到 30%」光看这几个字可能在说亮度、缩放、进度条，
# 不带音量词一律交给 Claude——和进会那边「光一串数字绝不进会」同一个道理。

_VOL_WORDS = ("音量", "声音", "volume", "vol")
_NOISE = " \t　:：,，、。!！?？~的了吧呀啊呢哦嘛谢谢"
_LEAD = ("请", "帮我", "帮忙", "麻烦", "给我", "你", "来", "先", "把", "现在")
# 「设成」这类动词不是必需的（「音量 30%」也认），但出现了就更确定
_SET_HINTS = ("调到", "调成", "设到", "设成", "设为", "改成", "调至", "设置成", "到",
              "调", "设", "改", "=", "：", ":")
_QUERY_WORDS = ("多少", "多大", "几", "什么水平", "状态", "现在是")
_MAX_WORDS = ("最大", "最高", "拉满", "开到最大", "max")
_MIN_WORDS = ("最小", "最低", "min")
_HALF_WORDS = ("一半", "百分之五十")     # 「50」不放这儿：那样会绕过下面的挨着检查
# 「调到」这类**多字**动词才算锚点。光一个「调」「改」不算——「30 分钟后把音量调好」
# 里就有个「调」，认了就等于把时间当音量。
_SET_HINTS = ("调到", "调成", "调至", "调节到", "设到", "设成", "设为", "设置成",
              "改成", "改到", "=")
# 数字后面紧跟这些词，说明那个数字在说别的事（时间、次数、倍数），一律不认
_UNIT_AFTER = ("分钟", "小时", "秒", "天", "点", "分", "次", "遍", "倍", "个", "月",
               "号", "首", "条", "米", "度")
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


def _has_vol_word(low):
    return any(w in low for w in _VOL_WORDS)


def _usable(body):
    """长度、外链这类硬门槛。返回处理过的小写正文，不合格返回 None。"""
    text = (body or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    low = _strip_lead(text).lower()
    if not low or "http" in low or "\\" in low or "/dm/" in low:
        return None
    if not _has_vol_word(low):
        return None
    return low


def parse_query(body):
    """「音量多少」这类问句。是就返回 True。"""
    low = _usable(body)
    if not low:
        return False
    if parse_set(body) is not None:
        return False                     # 「音量调到 30」是指令不是问句
    return any(w in low for w in _QUERY_WORDS) or low in _VOL_WORDS


def _anchored(low, i, end):
    """数字必须**挨着**音量词或「调到」这类动词才算数。

    这条检查是这个解析器唯一真正拦得住误判的地方：不带它，「30 分钟后把音量调好」
    「音量在第 3 首歌」都会被当成设成某个百分比。
    """
    for w in _VOL_WORDS + _SET_HINTS:
        p = low.find(w)
        while p >= 0:
            q = p + len(w)
            if 0 <= i - q <= 4:            # 锚点在前、数字紧跟：「音量调到 30」
                return True
            if 0 <= p - end <= 1:          # 数字在前、紧接着锚点：「30% 音量」
                return True
            p = low.find(w, p + 1)
    return False


def parse_set(body):
    """「音量调到 30%」→ 30。认不出返回 None（交给 Claude，绝不瞎猜）。

    只认 0~100 的阿拉伯数字，外加「最大/最小/一半」。**句子里出现两个数字就不猜**
    ——「音量从 30 调到 50」看着清楚，但也可能是「音量 30 分钟后调 50」这类话，
    宁可漏认。数字后面跟着「分钟」「次」这类量词的也一律不认。
    """
    low = _usable(body)
    if not low:
        return None
    if any(w in low for w in _MAX_WORDS):
        return 100
    if any(w in low for w in _MIN_WORDS):
        return 0
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
        return None                      # 「音量 300」不像在说百分比
    i = low.find(nums[0])
    end = i + len(nums[0])
    rest = low[end:end + 4].lstrip(" %　")
    if any(rest.startswith(u) for u in _UNIT_AFTER):
        return None                      # 「音量 30 分钟」在说时间
    if "%" in low:
        return val
    return val if _anchored(low, i, end) else None


# 静音/取消静音的词表**只在 app_actions.py 里有一份**，不在这里重复：方向由动作名
# （mute / unmute）带过来，混音器照着做就行。中文里同义词太多，两个文件各存一份必然
# 走岔——「开始会话」漏进词表那次就是这么栽的。


# ---------- C# 那一段：Core Audio 的三个接口 ----------
#
# **这 18 个方法一个都不能删、不能换顺序**（见模块开头）。用不到的也得占位。
# 让 C# 把活干完、只把 float/bool 递出来，PowerShell 一行 COM 都不碰——
# 接口是 internal 的，PowerShell 拿到也调不动。

_CSHARP = r"""
using System;
using System.Runtime.InteropServices;

[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int NotUsed_RegisterControlChangeNotify(IntPtr pNotify);
  int NotUsed_UnregisterControlChangeNotify(IntPtr pNotify);
  int NotUsed_GetChannelCount(out uint pnChannelCount);
  int NotUsed_SetMasterVolumeLevel(float fLevelDB, Guid ctx);
  int SetMasterVolumeLevelScalar(float fLevel, Guid ctx);
  int NotUsed_GetMasterVolumeLevel(out float pfLevelDB);
  int GetMasterVolumeLevelScalar(out float pfLevel);
  int NotUsed_SetChannelVolumeLevel(uint nChannel, float fLevelDB, Guid ctx);
  int NotUsed_SetChannelVolumeLevelScalar(uint nChannel, float fLevel, Guid ctx);
  int NotUsed_GetChannelVolumeLevel(uint nChannel, out float pfLevelDB);
  int NotUsed_GetChannelVolumeLevelScalar(uint nChannel, out float pfLevel);
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, Guid ctx);
  int GetMute([MarshalAs(UnmanagedType.Bool)] out bool pbMute);
  int NotUsed_GetVolumeStepInfo(out uint pnStep, out uint pnStepCount);
  int NotUsed_VolumeStepUp(Guid ctx);
  int NotUsed_VolumeStepDown(Guid ctx);
  int NotUsed_QueryHardwareSupport(out uint pdwMask);
  int NotUsed_GetVolumeRange(out float pflMin, out float pflMax, out float pflInc);
}
// IMMDevice: only Activate is used, the rest are vtable placeholders.
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  int Activate(ref Guid iid, int dwClsCtx, IntPtr pParams,
               [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
  int NotUsed_OpenPropertyStore(int stgmAccess, out IntPtr ppProperties);
  int NotUsed_GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
  int NotUsed_GetState(out int pdwState);
}

[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
  int NotUsed_EnumAudioEndpoints(int dataFlow, int dwStateMask, out IntPtr ppDevices);
  int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
}

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumeratorObject { }

public class WxVol {
  // dataFlow 0 = eRender, role 1 = eMultimedia, CLSCTX_ALL = 23
  private static IAudioEndpointVolume Dev() {
    IMMDeviceEnumerator en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorObject());
    IMMDevice dev;
    Marshal.ThrowExceptionForHR(en.GetDefaultAudioEndpoint(0, 1, out dev));
    Guid iid = typeof(IAudioEndpointVolume).GUID;
    object o;
    Marshal.ThrowExceptionForHR(dev.Activate(ref iid, 23, IntPtr.Zero, out o));
    return (IAudioEndpointVolume)o;
  }
  public static float Get() {
    float f; Marshal.ThrowExceptionForHR(Dev().GetMasterVolumeLevelScalar(out f)); return f;
  }
  public static bool GetMute() {
    bool b; Marshal.ThrowExceptionForHR(Dev().GetMute(out b)); return b;
  }
  public static void Set(float f) {
    if (f < 0f) f = 0f; if (f > 1f) f = 1f;
    Marshal.ThrowExceptionForHR(Dev().SetMasterVolumeLevelScalar(f, Guid.Empty));
  }
  public static void SetMute(bool b) {
    Marshal.ThrowExceptionForHR(Dev().SetMute(b, Guid.Empty));
  }
}
"""

# ---------- PowerShell 外壳 ----------
#
# 脚本正文里**一个中文都没有**（沿用项目规矩），也**一个单引号都没有**——C# 是用
# @'...'@ 原样塞进去的，里面出现单引号会提前把 here-string 收掉。改 C# 时留意这条。
# 输出统一是 base64(UTF-8 JSON)：Windows 的报错信息常常是中文，控制台编码不可靠，
# 编一层就不用管它。

_PS = """$ErrorActionPreference='Stop'
try {
  Add-Type -TypeDefinition @'
__CSHARP__
'@
  $act='__ACTION__'
  $arg=__ARG__
  $before=[int][math]::Round([WxVol]::Get()*100)
  $bmute=[bool][WxVol]::GetMute()
  if($act -eq 'set'){ [WxVol]::Set([float]($arg/100.0)) }
  elseif($act -eq 'nudge'){
    $t=$before+$arg
    if($t -lt 0){$t=0}
    if($t -gt 100){$t=100}
    [WxVol]::Set([float]($t/100.0))
  }
  elseif($act -eq 'mute'){ [WxVol]::SetMute([bool]$arg) }
  $r=@{ok=$true;before=$before;beforemuted=$bmute;
       volume=[int][math]::Round([WxVol]::Get()*100);muted=[bool][WxVol]::GetMute()}
} catch {
  $r=@{ok=$false;error=$_.Exception.Message}
}
$j=$r|ConvertTo-Json -Compress
[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)))
"""

ACTIONS = ("get", "set", "nudge", "mute")


def build_script(action, arg=0):
    """拼脚本。**action 必须是上面四个之一，arg 必须是整数**——这是唯一两处
    外部输入能碰到脚本正文的地方，都在这里卡死，用户的原话永远进不来。"""
    if action not in ACTIONS:
        raise ValueError("未知的音量动作：%r" % (action,))
    return (_PS.replace("__CSHARP__", _CSHARP)
               .replace("__ACTION__", action)
               .replace("__ARG__", str(int(arg))))


def _run_powershell(action, arg=0):
    """跑一次，返回解好的 dict。失败一律抛异常——上层照着退回按键。"""
    if os.name != "nt":
        raise RuntimeError("这台机器不是 Windows，读不了系统混音器")
    enc = base64.b64encode(build_script(action, arg).encode("utf-16-le")).decode("ascii")
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
    proc = subprocess.run(cmd, capture_output=True, timeout=PS_TIMEOUT, shell=False)
    raw = (proc.stdout or b"").decode("ascii", "ignore").strip()
    if not raw:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError("PowerShell 没输出（返回码 %s）：%s"
                           % (proc.returncode, err[:200] or "stderr 也是空的"))
    obj = json.loads(base64.b64decode(raw).decode("utf-8", "replace"))
    if not isinstance(obj, dict):
        raise RuntimeError("混音器返回的不是预期格式：%s" % (raw[:120],))
    if not obj.get("ok"):
        raise RuntimeError(str(obj.get("error") or "混音器没说为什么失败"))
    return obj


class VolumeMixer(object):
    """绝对音量。**每个方法失败都抛异常**，由 MediaControl 接住退回按键。

    `runner` 只为自测存在：真机上就是 `_run_powershell`。
    """

    def __init__(self, runner=None):
        self._run = runner or _run_powershell

    def _ask(self, action, arg=0):
        obj = self._run(action, arg)
        vol = int(obj.get("volume", -1))
        if not 0 <= vol <= 100:
            raise RuntimeError("混音器给的音量不像话：%r" % (obj.get("volume"),))
        return {"before": int(obj.get("before", vol)),
                "beforemuted": bool(obj.get("beforemuted")),
                "volume": vol,
                "muted": bool(obj.get("muted"))}

    def get(self):
        return self._ask("get")

    def set(self, percent):
        return self._ask("set", max(0, min(100, int(percent))))

    def nudge(self, delta):
        return self._ask("nudge", max(-100, min(100, int(delta))))

    def mute(self, on):
        return self._ask("mute", 1 if on else 0)

    # ---------- 直接给出回复文本 ----------
    # app_actions.py **不 import 这个模块**（那边要保持零依赖），只按名字调下面四个。
    # 改名字等于改契约，改了记得同步 app_actions.MediaControl.run。

    def nudge_text(self, delta):
        return say_nudge(self.nudge(delta))

    def mute_text(self, on):
        return say_mute(self.mute(on), on)

    def set_text(self, percent):
        return say_set(self.set(percent), percent)

    def query_text(self):
        return say_query(self.get())


# ---------- 怎么回话 ----------
#
# 这一层唯一比按键强的地方就是**能说出具体数字、能说出「本来就没静音」**，
# 所以回话一律带上前后两个值。反过来说：混音器挂了退回按键时，回话里就不会有数字，
# 用户能一眼看出走的是哪条路——这是故意的，别为了「好看」给按键那条路编个数出来。

def _mute_tail(r):
    return "（不过现在是静音，得先说「取消静音」才听得见）" if r.get("muted") else ""


def say_query(r):
    if r.get("muted"):
        return "✅ 系统音量 %d%%，但现在是静音状态。" % r["volume"]
    return "✅ 系统音量现在 %d%%。" % r["volume"]


def say_set(r, asked=None):
    txt = "✅ 系统音量：%d%% → %d%%。" % (r["before"], r["volume"])
    if asked is not None and abs(int(asked) - r["volume"]) > 1:
        txt += "（你说的是 %d%%，系统实际落在 %d%%）" % (int(asked), r["volume"])
    return txt + _mute_tail(r)


def say_nudge(r):
    if r["before"] == r["volume"]:
        edge = "最大" if r["volume"] >= 100 else ("最小" if r["volume"] <= 0 else "没动")
        return "🔕 系统音量已经是 %d%%，%s了。%s" % (r["volume"], edge, _mute_tail(r))
    return "✅ 系统音量：%d%% → %d%%。%s" % (r["before"], r["volume"], _mute_tail(r))


def say_mute(r, on):
    """`on` 是这次想要的方向。**「本来就没静音」这句只有这一层说得出来**——
    按键那条路只能说「切了一下」。"""
    if on:
        if r.get("beforemuted"):
            return "🔕 本来就是静音的，现在还是静音（音量 %d%%）。" % r["volume"]
        return "✅ 已静音（音量还是 %d%%，取消静音就回来）。" % r["volume"]
    if not r.get("beforemuted"):
        return "🔕 本来就没静音，现在音量 %d%%。" % r["volume"]
    return "✅ 已取消静音，现在 %d%%。" % r["volume"]


