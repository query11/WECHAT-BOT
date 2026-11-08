# -*- coding: utf-8 -*-
"""看一眼当前窗口、找到某个控件、（等你确认之后）点它一下。

三条硬规矩，改代码前先读：

1. **点击一律要在微信里再确认一次。** 这一层只负责「准备好一个动作」，
   没等到「确认」就什么都不做，隔 confirm_seconds 秒自动作废。
2. **确认那一刻要重新核对前台窗口是不是计划时的那个。** 存的是绝对屏幕坐标，
   窗口一换（或者被挪了位置）就等于在乱点，所以窗口不对就拒绝执行、让用户重说。
3. **优先 UI Automation，拿不到控件树才退回截图。** 控件树给的是精确矩形，
   截图那条路要把当前窗口的画面送到模型服务端，而且坐标经常偏几十像素。

为什么不 `pip install uiautomation`：.NET 自带 UIAutomationClient，PowerShell 直接能用，
这个项目从头到尾零第三方依赖，为了一个功能破例不值得。

中文路径不能直接写进 PowerShell 命令行，所以路径进去、JSON 出来都走 base64，
且整条命令里不出现双引号（Windows 的 argv 引号规则会再插一脚）。脚本里实在需要
双引号的地方（C# 源码里的 DllImport）用 `[char]34` 拼，别直接写。

Windows 相关的三件事（读窗口、截图、点鼠标）都能从外面注入，所以 Linux 上也能自测。
"""

import base64
import json
import logging
import os
import re
import subprocess
import time

log = logging.getLogger("screen")

# 优先找这些类型的控件：正常应用里能点的东西都在这儿
_CONTROL_TYPES = ("Button", "MenuItem", "ListItem", "TabItem", "CheckBox",
                  "RadioButton", "Hyperlink", "Edit", "ComboBox", "SplitButton",
                  "TreeItem", "Slider")

# 上面那些一个都没找到时，才看这些。自绘界面（Electron、CEF、自己画的播放器）
# 常把所有可点区域都报成 Custom/Text/Image——真机上微信和波点音乐都是这样，
# 只收上面那 12 类的话两个窗口都会显示「控件树是空的」，而其实里面有几百个节点。
# 只在第一批为空时启用，并且按面积从小到大排（小的更像按钮，大的多半是容器）
_EXTRA_TYPES = ("Custom", "Text", "Image", "Group", "Pane", "DataItem",
                "ToolBar", "Menu", "Thumb", "Spinner", "Document")
_ALL_TYPES = _CONTROL_TYPES + _EXTRA_TYPES

MAX_ELEMENTS = 300          # 控件树最多取这么多个，够用且不会让一次调用变很慢
MAX_SCAN = 1500             # 最多摸这么多个节点：自绘应用能报出上万个，读属性很慢
MAX_QUERY_LEN = 20          # 要点的东西名字超过这么长就不当指令看
PS_TIMEOUT = 40             # PowerShell 单次超时；控件树在个别应用上会慢

# 这些窗口上一律不点：终端类窗口里点一下等于替你按回车执行一条命令，
# 注册表/组策略这类改了不好恢复。桥自己就跑在终端里，尤其不能碰
REFUSE_TITLES = ("命令提示符", "cmd.exe", "windows powershell", "powershell",
                 "windows terminal", "终端", "注册表编辑器", "registry editor",
                 "regedit", "本地组策略", "group policy", "任务管理器",
                 "task manager", "用户账户控制", "user account control")

_DPI_DONE = False

# 这些不是应用窗口，是壳子：Program Manager 就是桌面本身，输入法那几个是隐形窗口。
# 人不在电脑前的时候前台窗口经常就是桌面，那时候「看当前窗口」毫无意义——
# 真机上第一次点击就栽在这儿：桌面的控件树是空的，还差点去截了桌面的图。
# 比的是 _norm 之后的全等，不用包含，免得把标题里带「搜索」的正经应用也毙了
_SHELL_EXACT = frozenset(["programmanager", "progman", "defaultime",
                          "msctfimeui", "windows输入体验", "任务视图", "taskview",
                          "搜索", "search", "开始", "start"])


def is_shell_window(title):
    """这个标题是不是桌面/输入法之类的壳子窗口。"""
    return _norm(title) in _SHELL_EXACT


def _norm(s):
    """比名字时忽略大小写、空格、连字符——「开始播放」和「开始 播放」是一个东西。"""
    return re.sub(r"[\s\-_.·]+", "", (s or "").strip().lower())


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _ensure_dpi():
    """让读窗口矩形、点鼠标、截图三件事用同一套物理像素，别被系统缩放骗了。

    没这一步的话，缩放不是 100% 的机器上 GetWindowRect 给的是逻辑像素、截图截的是
    物理像素，截出来的画面会整体往左上偏、内容还被放大了缩放倍数。真机上就是这么
    栽的一次，所以下面每一步都**必须看返回值**，不能调完就当成功。

    第一步那个 -4 是个句柄，要按指针宽度传：直接把 Python 的 -4 交给 ctypes 会被当成
    32 位 int，在 64 位 Windows 上凑不出 0xFFFFFFFFFFFFFFFC，函数会安安静静地失败。
    设一次就够，且只影响本进程。
    """
    global _DPI_DONE
    if _DPI_DONE:
        return
    _DPI_DONE = True
    try:
        import ctypes
    except ImportError:
        return
    try:
        # -4 = PER_MONITOR_AWARE_V2，Win10 1703 以后才有
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE。S_OK 是 0；已经设过会给
        # E_ACCESSDENIED（0x80070005），那也算设上了
        hr = int(ctypes.windll.shcore.SetProcessDpiAwareness(2))
        if hr in (0, -2147024891):
            return
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return
    except Exception:
        pass
    log.warning("没能把进程设成 DPI 感知，缩放不是 100%% 的话坐标会偏")


def dpi_scale():
    """1.0 = 我们读到的坐标就是物理像素。不是 1.0 就说明进程没设成 DPI 感知。

    量的是主屏的两个宽度：GDI 的 `DESKTOPHORZRES` 永远是物理像素，而
    `SM_CXSCREEN` 在非感知进程里会被系统按缩放虚拟化成逻辑像素。两者一比，
    既能判断有没有设上，又能顺手知道缩放是多少——比信 `SetProcess*` 的返回值可靠。
    """
    _ensure_dpi()
    try:
        import ctypes
    except ImportError:
        return 1.0
    try:
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        dc = u.GetDC(0)
        try:
            phys = int(g.GetDeviceCaps(dc, 118))     # DESKTOPHORZRES
        finally:
            u.ReleaseDC(0, dc)
        logical = int(u.GetSystemMetrics(0))         # SM_CXSCREEN
        if phys > 0 and logical > 0:
            return round(float(phys) / logical, 3)
    except Exception as exc:
        log.debug("量不出缩放：%s", exc)
    return 1.0



def _ps(script, timeout=PS_TIMEOUT):
    """跑一段 PowerShell，把它用 base64 吐回来的 JSON 解出来。

    整条命令里不出现双引号，脚本里的中文也全走 base64——见模块顶部的说明。
    """
    cmd = ["powershell", "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-Command", script]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout, shell=False)
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError("PowerShell 没有输出：%s" % (err[:200] or "（连报错都没有）"))
    try:
        return json.loads(base64.b64decode(out).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("PowerShell 的输出解不开（%s）：%s" % (exc, out[:200]))


def _default_window():
    """当前前台窗口的句柄、标题、屏幕矩形。最小化的窗口返回 minimized=True。"""
    import ctypes
    from ctypes import wintypes
    _ensure_dpi()
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    u.GetWindowTextW(hwnd, buf, n + 2)
    return {"hwnd": int(hwnd), "title": buf.value or "（没有标题）",
            "left": int(rect.left), "top": int(rect.top),
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
            # 最小化的窗口坐标是 -32000 那种，截也截不到
            "minimized": bool(rect.left < -20000 or rect.top < -20000)}


def _win_api():
    """user32，且把要用的几个函数的参数类型声明好。

    句柄必须按指针宽度传：不声明 argtypes 就把 Python 的 int 交出去，ctypes 会当成
    32 位 int。DPI 那个 -4 就是这么栽的，这里不再重复同一个错误。
    """
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    return u


def _is_cloaked(hwnd):
    """UWP 应用会留一堆「隐身」的空窗口，DWM 说它 cloaked 就别当成开着的窗口。"""
    import ctypes
    from ctypes import wintypes
    val = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return bool(val.value)


def _default_windows(cap=60):
    """现在开着的顶层窗口，按 Z 序（最前面的在前）。壳子窗口在上层过滤。"""
    import ctypes
    from ctypes import wintypes
    _ensure_dpi()
    u = _win_api()
    out = []

    def cb(hwnd, _lparam):
        if len(out) >= cap:
            return False
        h = wintypes.HWND(hwnd)
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        u.GetWindowTextW(h, buf, n + 2)
        title = (buf.value or "").strip()
        if not title or _is_cloaked(hwnd):
            return True
        rect = wintypes.RECT()
        if not u.GetWindowRect(h, ctypes.byref(rect)):
            return True
        w, hh = int(rect.right - rect.left), int(rect.bottom - rect.top)
        mini = bool(rect.left < -20000 or rect.top < -20000) or bool(u.IsIconic(h))
        if not mini and (w <= 1 or hh <= 1):
            return True
        out.append({"hwnd": int(hwnd), "title": title,
                    "left": int(rect.left), "top": int(rect.top),
                    "width": w, "height": hh, "minimized": mini})
        return True

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
    u.EnumWindows(proc, 0)
    return out


def _default_activator(hwnd):
    """把这个窗口切到前面。成不成由调用方回头看前台窗口来判断，不信返回值。

    后台进程调 `SetForegroundWindow` 经常被系统直接拒掉（前台锁），所以还补一手
    `SwitchToThisWindow`——Alt+Tab 用的就是它，未文档化但干的正是这件事。
    故意不发 Alt 假按键那种土办法：那会把按键送进目标应用，可能顺手拉开它的菜单。
    """
    from ctypes import wintypes
    _ensure_dpi()
    u = _win_api()
    h = wintypes.HWND(hwnd)
    u.ShowWindow(h, 9 if u.IsIconic(h) else 5)      # 9=SW_RESTORE 5=SW_SHOW
    u.SetForegroundWindow(h)
    try:
        u.SwitchToThisWindow(h, True)
    except Exception:
        pass
    u.BringWindowToTop(h)


# 控件类型写成固定的字面量，不用 [ControlType]::$name 那种动态取法——
# 按变量取静态属性在不同 PowerShell 版本上行为不一致。
# 开头那句 SetProcessDPIAware 不能省：控件矩形要和 Python 这边的窗口矩形、
# 和鼠标坐标处在同一套物理像素里，否则缩放不是 100% 的机器上点下去就是偏的
_CTS = ",".join("[System.Windows.Automation.ControlType]::%s" % t
                for t in _ALL_TYPES)
# 单引号在这里是安全的（禁的只有双引号），所以短名单直接写成 PowerShell 字符串数组
_NARROW = ",".join("'%s'" % t for t in _CONTROL_TYPES)

# 这段脚本**必须把失败说出来**。老版本开头是 ErrorActionPreference='SilentlyContinue'
# 且没有 try/catch，于是 Add-Type 失败、FromHandle 返回空、FindAll 抛异常，最后都
# 一样输出一个空数组——Python 这边收到「成功，0 个控件」，回给用户的是「控件树是空的，
# 这窗口大概是自己画界面的」。真机上连栽两次都被这句话挡住了：一次是前台窗口其实是
# 桌面，一次是控件类型没收全。**分不清「查失败」和「真没有」的诊断等于没有诊断。**
_PS_ELEMENTS = """
$ErrorActionPreference='Stop';
$q=[char]34;
$res=@{items=@();wide=@();total=0;scanned=0;kinds=@{};error=$null};
try{
  try{
    Add-Type -TypeDefinition ('using System;using System.Runtime.InteropServices;public class KDpi2{[DllImport(' + $q + 'user32.dll' + $q + ')]public static extern bool SetProcessDPIAware();}');
    [void][KDpi2]::SetProcessDPIAware();
  }catch{};
  Add-Type -AssemblyName UIAutomationClient;
  Add-Type -AssemblyName UIAutomationTypes;
  $A=[System.Windows.Automation.AutomationElement];
  $el=$A::FromHandle([IntPtr]__HWND__);
  if($el -eq $null){throw ('FromHandle returned null')};
  $narrow=@(__NARROW__);
  $conds=New-Object System.Collections.Generic.List[System.Windows.Automation.Condition];
  foreach($ct in @(__CTS__)){
    [void]$conds.Add((New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty,$ct)))
  };
  $found=$el.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.OrCondition($conds.ToArray())));
  $res.total=$found.Count;
  $keep=New-Object System.Collections.ArrayList;
  $other=New-Object System.Collections.ArrayList;
  foreach($e in $found){
    if($res.scanned -ge __SCAN__){break};
    $res.scanned=$res.scanned+1;
    $c=$null;
    try{$c=$e.Current}catch{continue};
    if($c -eq $null){continue};
    $t=($c.ControlType.ProgrammaticName -split '\\.')[-1];
    if($res.kinds.ContainsKey($t)){$res.kinds[$t]=$res.kinds[$t]+1}else{$res.kinds[$t]=1};
    if($c.IsOffscreen){continue};
    $r=$c.BoundingRectangle;
    if($r.Width -le 0 -or $r.Height -le 0){continue};
    $nm=$c.Name;
    if($nm -eq $null -or $nm.Trim().Length -eq 0){continue};
    $row=@{name=$nm;type=$t;x=[int]$r.X;y=[int]$r.Y;w=[int]$r.Width;h=[int]$r.Height;
      enabled=[bool]$c.IsEnabled};
    if($narrow -contains $t){
      if($keep.Count -lt __CAP__){[void]$keep.Add($row)}
    }else{
      if($other.Count -lt __CAP__){[void]$other.Add($row)}
    }
  };
  $res.items=@($keep);
  $res.wide=@($other);
}catch{
  $res.error=$_.Exception.Message;
};
$j=ConvertTo-Json -InputObject $res -Compress -Depth 4;
if($j -eq $null -or $j.Length -eq 0){$j='{}'};
[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)))
"""


def _default_prober(hwnd, cap=MAX_ELEMENTS):
    """走 UI Automation 列出这个窗口里能点的控件。

    返回 `{items, wide, total, scanned, kinds, error}`：`items` 是正常的可点控件，
    `wide` 是自绘界面里翻出来的兜底候选，`error` 有值就说明这次是**查失败**了，
    不是窗口里没东西——上层必须把这两件事分开说。

    脚本里的字符串一律写英文：整条命令是当作命令行参数交给 powershell.exe 的，
    中文在这条路上会被控制台编码搅乱（模块开头那条规矩），所以中文只在这里翻。
    """
    script = (_PS_ELEMENTS.replace("__HWND__", str(int(hwnd)))
              .replace("__CTS__", _CTS).replace("__NARROW__", _NARROW)
              .replace("__CAP__", str(int(cap))).replace("__SCAN__", str(MAX_SCAN)))
    data = _ps(script)
    if isinstance(data, dict) and data.get("error"):
        data["error"] = _PS_ERR_CN.get(str(data["error"]).strip(), data["error"])
    return data


# 脚本里自己抛的那几句英文翻回中文；PowerShell 自带的报错原样带回来
_PS_ERR_CN = {"FromHandle returned null":
              "FromHandle 没给出这个窗口的 UIA 元素（这个窗口不参与 UI Automation）"}


# 截图。$q=[char]34 是为了在不写双引号的前提下拼出 C# 里的 DllImport("user32.dll")；
# 不设 DPI 感知的话 CopyFromScreen 拿到的区域会和我们要的矩形错位
_PS_SHOT = """
$ErrorActionPreference='Stop';
$q=[char]34;
Add-Type -TypeDefinition ('using System;using System.Runtime.InteropServices;public class KDpi{[DllImport(' + $q + 'user32.dll' + $q + ')]public static extern bool SetProcessDPIAware();}');
[void][KDpi]::SetProcessDPIAware();
Add-Type -AssemblyName System.Drawing;
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'));
$bmp=New-Object System.Drawing.Bitmap(__W__,__H__);
$g=[System.Drawing.Graphics]::FromImage($bmp);
$g.CopyFromScreen(__X__,__Y__,0,0,$bmp.Size);
$g.Dispose();
$bmp.Save($p,[System.Drawing.Imaging.ImageFormat]::Png);
$j=ConvertTo-Json -InputObject @{w=$bmp.Width;h=$bmp.Height} -Compress;
$bmp.Dispose();
[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)))
"""


def _default_shooter(path, left, top, width, height):
    """把屏幕上这一块存成 PNG，返回 (图片宽, 图片高)。只截给定矩形，不截整屏。"""
    script = (_PS_SHOT.replace("__PATH__", _b64(path))
              .replace("__W__", str(int(width))).replace("__H__", str(int(height)))
              .replace("__X__", str(int(left))).replace("__Y__", str(int(top))))
    data = _ps(script)
    return int(data.get("w") or width), int(data.get("h") or height)


def _default_clicker(x, y, double=False, button="left"):
    """挪光标 + 按一下。用的是老的 mouse_event，够用且不需要构造 INPUT 结构体。"""
    import ctypes
    _ensure_dpi()
    u = ctypes.windll.user32
    if not u.SetCursorPos(int(x), int(y)):
        raise RuntimeError("光标挪不过去，(%d,%d) 可能不在任何一块屏幕上" % (x, y))
    time.sleep(0.05)
    down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    for i in range(2 if double else 1):
        if i:
            time.sleep(0.05)
        u.mouse_event(down, 0, 0, 0, 0)
        u.mouse_event(up, 0, 0, 0, 0)


# ---------------- 解析用户说的话 ----------------

# 「点/点一下/双击 + 名字」。「点开微信」这种更像开应用，靠 _bad_target 挡掉
_CLICK_RE = re.compile(r"^(?:请|帮我|帮忙|麻烦|给我|替我)?\s*"
                       r"(双击|点击|点一下|点下|单击|点)\s*[:：]?\s*(.+)$")

_CONFIRM = frozenset(["确认", "确定", "可以", "好", "好的", "行", "对", "是",
                      "就它", "就这个", "点", "点吧", "确认点", "ok", "okay",
                      "yes", "y", "sure"])
_CANCEL = frozenset(["取消", "算了", "不", "不要", "别", "别点", "不用", "停",
                     "撤销", "no", "n", "cancel"])

# 名字里出现这些就不当「要点的控件」看：一句话里还有别的事，或者带的是路径
_BAD_IN_TARGET = ("然后", "，", ",", "。", "\\", "/", "：", ":", "；", ";")


def _digits(s):
    """把全角数字也认成数字。用户在手机上打「１」不奇怪。"""
    return s.translate({ord(c): ord(d) for c, d in
                        zip("０１２３４５６７８９", "0123456789")})


def _bad_target(q):
    # 这里放宽一点也不危险：名字在控件树里找不到就照旧交回给 Claude，
    # 不会凭空点东西。所以只挡明显不是控件名的（太长、带路径、还有别的事）
    return (not q) or len(q) > MAX_QUERY_LEN or any(b in q for b in _BAD_IN_TARGET)


def parse_click(body):
    """认出「点一下 X」，返回 (要点的东西, 是不是双击)；不像就返回 (None, False)。

    宁可漏拦不误拦：句式不对、名字太长、带路径或连接词，一律交回给 Claude。
    """
    s = (body or "").strip().rstrip("。.!！?？")
    m = _CLICK_RE.match(s)
    if not m:
        return None, False
    verb, query = m.group(1), m.group(2).strip().strip("「」\"'“”")
    if _bad_target(query):
        return None, False
    return query, verb == "双击"


def read_reply(body):
    """待确认状态下这条消息是什么意思：('yes', None) / ('no', None) /
    ('pick', 序号从 0 开始) / (None, None) 表示跟这次确认无关。"""
    s = _digits((body or "").strip().rstrip("。.!！?？").strip("「」\"'“”"))
    low = s.lower()
    if low in _CANCEL:
        return "no", None
    if re.fullmatch(r"\d{1,2}", s):
        n = int(s)
        return ("pick", n - 1) if n >= 1 else (None, None)
    if low in _CONFIRM:
        return "yes", None
    return None, None


# ---------------- 看图兜底：让 CLI 读这张 PNG，只回坐标 ----------------

_VISION_TMPL = (
    "读一下这张截图：{path}\n"
    "它是「{title}」窗口现在的画面，图片尺寸 {w}x{h} 像素。\n"
    "在画面里找到「{query}」这个可以点的东西，只回一行 JSON，前后不要任何别的字：\n"
    "{{\"found\": true, \"x\": 图中横坐标整数, \"y\": 图中纵坐标整数, "
    "\"what\": \"你认出的是什么\"}}\n"
    "坐标要落在那个东西的中心，原点是图片左上角。\n"
    "找不到或者不确定就回 {{\"found\": false, \"why\": \"一句话说明\"}}，别猜。")


def vision_prompt(path, title, img_w, img_h, query):
    return _VISION_TMPL.format(path=path, title=title, w=img_w, h=img_h,
                               query=query)


def parse_vision(text):
    """把 CLI 回的那行 JSON 抠出来。它有时会包代码块或者前后带一句话。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text.replace("\n", " "), re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("found"):
        return None
    try:
        return {"x": int(round(float(data["x"]))),
                "y": int(round(float(data["y"]))),
                "what": str(data.get("what") or "")[:40]}
    except (KeyError, TypeError, ValueError):
        return None


# ---------------- 「看看画面」：截一张图让 CLI 说说上面有什么 ----------------
#
# 这条路是给**控件树走不通的应用**留的。真机上波点音乐是 Flutter 写的，整棵 UIA 树
# 只有一个 FLUTTERVIEW 节点（Flutter 默认不建语义树），微信也差不多——控件树里没有
# 名字可用，你就无从说出「点一下什么」。所以先让 Claude 看一眼画面、把上面有什么念给
# 你听，你再照着说要点哪个。代价和看图兜底一样：慢、花一次额度、**把当前窗口的画面
# 送到模型服务端**，所以回复里必须说清楚，而且它走 worker 线程。
_LOOK_WORDS = frozenset([
    "看看画面", "看画面", "看一下画面", "看一眼画面", "看看这个画面",
    "看看当前画面", "看看屏幕", "看屏幕", "看一下屏幕", "看一眼屏幕",
    "看看窗口画面", "截图看看", "截个图看看", "截图看一眼", "截个图看一眼",
    "/look", "/shot", "look"])

# 「帮我看看画面」这类客套开头，去掉之后再对表
_LOOK_LEAD = ("帮我", "帮忙", "请", "麻烦", "你", "先", "现在", "给我")


def parse_look(body):
    """认出「看看画面」这类整句请求。返回 True/False。

    只在**整条消息就是这个请求**时才算（前面顶多有「帮我」这类客套）。这一步要花额度、
    还会把画面送出去，所以宁可漏认不误认——「看看画面里那个按钮怎么写」这种带后文的
    照旧交给 Claude。
    """
    s = (body or "").strip().rstrip("。.!！?？~～ ").strip("「」\"'“”")
    if not s or len(s) > 20:
        return False
    low = s.lower()
    for lead in _LOOK_LEAD:
        if low.startswith(lead) and len(low) > len(lead):
            low = low[len(lead):].strip(" ，,:：")
    return low in _LOOK_WORDS


_LOOK_TMPL = (
    "读一下这张截图：{path}\n"
    "它是「{title}」窗口现在的画面，图片尺寸 {w}x{h} 像素。\n"
    "用中文告诉我这个窗口上现在有什么，分两部分，总共不超过 200 字：\n"
    "1. 一句话说这是什么界面、正在显示什么内容（有歌名、进度、状态就说出来）。\n"
    "2. 列出画面上看起来能点的东西的名字，每个一行，用「- 」开头，最多 12 个；"
    "有文字的就用它上面的原文，没文字的图标就描述它（例如「左下角的播放三角」）。\n"
    "不要给坐标，不要写你看不见的东西，不确定的就不写。")


def look_prompt(path, title, img_w, img_h):
    return _LOOK_TMPL.format(path=path, title=title, w=img_w, h=img_h)


_TYPE_CN = {"Button": "按钮", "MenuItem": "菜单项", "ListItem": "列表项",
            "TabItem": "标签页", "CheckBox": "勾选框", "RadioButton": "单选",
            "Hyperlink": "链接", "Edit": "输入框", "ComboBox": "下拉框",
            "SplitButton": "按钮", "TreeItem": "树节点", "Slider": "滑块"}


# 这些名字是「整个窗口的画布」，不是可点的东西。Flutter 报 FLUTTERVIEW、
# Chromium 系报 Chrome Legacy Window、D3D 游戏报 Intermediate D3D Window——
# 真机上波点音乐整棵 UIA 树就只有一个 FLUTTERVIEW，把它当按钮报给用户毫无用处：
# 点它的中心等于点窗口正中间。认出来之后要直接告诉用户「这个应用没把界面暴露出来」。
_SURFACE_NAMES = frozenset(["flutterview", "flutterviewwindow",
                            "chromelegacywindow", "intermediated3dwindow",
                            "canvas", "glcanvas", "surface", "renderwidget",
                            "chromerenderwidgethosthwnd"])


def _is_surface(name):
    """这个名字是不是「整块画布」而不是一个能点的东西。"""
    return _norm(name) in _SURFACE_NAMES


def _type_cn(programmatic):
    """把 'ControlType.Button' 说成「按钮」。"""
    short = (programmatic or "").split(".")[-1]
    return _TYPE_CN.get(short, short or "控件")


class ScreenControl(object):
    """看当前窗口 + 按名字找控件 + 点一下。Windows 那三件事都可以注入。"""

    def __init__(self, shots_dir, confirm_seconds=180, vision_fallback=True,
                 keep_shots=1, refuse_titles=None, max_elements=MAX_ELEMENTS,
                 window_fn=None, prober=None, shooter=None, clicker=None,
                 dpi_fn=None, windows_fn=None, activator=None):
        self.shots_dir = shots_dir
        self.confirm_seconds = max(0, int(confirm_seconds or 0))
        self.vision_fallback = bool(vision_fallback)
        self.keep_shots = max(0, int(keep_shots or 0))
        self.max_elements = int(max_elements or MAX_ELEMENTS)
        titles = list(REFUSE_TITLES) + list(refuse_titles or [])
        self.refuse_titles = tuple(_norm(t) for t in titles if _norm(t))
        self._window_fn = window_fn or _default_window
        self._prober = prober or _default_prober
        self._shooter = shooter or _default_shooter
        self._clicker = clicker or _default_clicker
        self._dpi_fn = dpi_fn or dpi_scale
        self._windows_fn = windows_fn or _default_windows
        self._activator = activator or _default_activator

    # ---- 坐标体系对不对 ----

    def dpi_problem(self):
        """坐标体系不一致时的说明；正常返回 None。

        宁可什么都不做也别在偏掉的坐标上点：偏出去的那一下可能落在别的按钮上。
        """
        try:
            scale = float(self._dpi_fn() or 1.0)
        except Exception as exc:
            log.warning("量缩放失败，当作正常处理：%s", exc)
            return None
        if abs(scale - 1.0) < 0.01:
            return None
        log.warning("进程没设成 DPI 感知，缩放=%.2f，坐标不可用", scale)
        return ("❌ 这台机器的显示缩放是 %d%%，而我这个进程没能设成 DPI 感知——"
                "读到的窗口坐标是逻辑像素，截图和鼠标用的却是物理像素，"
                "点下去会整体偏掉，所以我不点。把这句话告诉 Claude，"
                "让它去查 screen_control._ensure_dpi。" % int(round(scale * 100)))


    # ---- 窗口 ----

    def active(self):
        """当前前台窗口；读不出来返回 None。"""
        try:
            win = self._window_fn()
        except Exception as exc:
            log.warning("读前台窗口失败：%s", exc)
            return None
        if not win or not win.get("hwnd"):
            return None
        return win

    def is_desktop(self, win):
        """前台是桌面/输入法这类壳子窗口——人不在电脑前时经常如此。"""
        return bool(win) and is_shell_window(win.get("title"))

    def windows(self):
        """现在开着的应用窗口，按 Z 序。桌面和输入法那类壳子窗口不算。

        终端类窗口照旧会列出来（列表要如实），但它们上面永远不点——那是
        refuse_reason 的事，两件事别混在一起。
        """
        try:
            wins = self._windows_fn()
        except Exception as exc:
            log.warning("列窗口失败：%s", exc)
            return []
        out = []
        for w in wins or []:
            if not isinstance(w, dict) or not w.get("hwnd"):
                continue
            if is_shell_window(w.get("title")):
                continue
            out.append(w)
        return out

    def find_window(self, query, wins=None):
        """按名字找窗口：先精确、再前缀、再包含。返回候选列表。"""
        q = _norm(query)
        if not q:
            return []
        exact, prefix, part = [], [], []
        for w in (wins if wins is not None else self.windows()):
            n = _norm(w.get("title"))
            if not n:
                continue
            if n == q:
                exact.append(w)
            elif n.startswith(q):
                prefix.append(w)
            elif q in n:
                part.append(w)
        return exact or prefix or part

    def describe_windows(self, wins, limit=12):
        """给手机看的窗口清单。"""
        if not wins:
            return "现在一个能操作的窗口都没有（只剩桌面）。"
        lines = []
        for w in wins[:limit]:
            t = w.get("title") or "（没有标题）"
            lines.append("· %s%s" % (t if len(t) <= 24 else t[:23] + "…",
                                     "（最小化）" if w.get("minimized") else ""))
        if len(wins) > limit:
            lines.append("（还有 %d 个没列）" % (len(wins) - limit))
        return "\n".join(lines)

    def activate(self, win, wait=1.5):
        """把窗口切到前面，然后回头看前台窗口确认成了没。

        返回 (成了没, 现在的前台窗口)。**不信 SetForegroundWindow 的返回值**：
        后台进程调它经常被系统的前台锁拒掉，成不成只能量。
        """
        hwnd = win.get("hwnd")
        try:
            self._activator(hwnd)
        except Exception as exc:
            log.warning("切窗口失败 hwnd=%s：%s", hwnd, exc)
        deadline = time.time() + max(0.0, float(wait))
        cur = None
        while True:
            cur = self.active()
            if cur and cur.get("hwnd") == hwnd:
                return True, cur
            if time.time() >= deadline:
                return False, cur
            time.sleep(0.15)

    def refuse_reason(self, title):
        """在这个窗口上不许点的理由，可以点就返回 None。"""
        n = _norm(title)
        for bad in self.refuse_titles:
            if bad and bad in n:
                return ("❌ 「%s」这类窗口上我不点——终端里点一下等于替你执行命令，"
                        "注册表这种改了也不好恢复。" % (title or "?"))
        return None

    # ---- 控件树 ----

    def elements(self, hwnd, info=None):
        """列出这个窗口里能点的控件。失败返回 []（上层再决定要不要看图）。

        `info` 传一个 dict 进来的话会被填上这次探测的实情：`error`（查失败的原因）、
        `total`/`scanned`（摸到多少节点）、`kinds`（类型分布）、`widened`（这批名字是
        从自绘控件里翻出来的）。**「查失败」和「窗口里真没东西」必须能分开**，
        否则就会像真机上那样，两种情况都被那句「控件树是空的」盖过去。
        """
        if info is None:
            info = {}
        try:
            raw = self._prober(hwnd, self.max_elements)
        except subprocess.TimeoutExpired:
            log.warning("读控件树超时 hwnd=%s", hwnd)
            info["error"] = "读控件树超时（%d 秒）" % PS_TIMEOUT
            return []
        except Exception as exc:
            log.warning("读控件树失败 hwnd=%s：%s", hwnd, exc)
            info["error"] = str(exc)
            return []
        # 注入的假探测器（和老版本）直接给一个列表，那时没有诊断信息可填
        if isinstance(raw, dict):
            # PowerShell 的 ConvertTo-Json 在只有一条的时候可能给个对象而不是数组，
            # 这个坑不值得为它在脚本里绕，进来时兜一下就完了
            items, wide = raw.get("items") or [], raw.get("wide") or []
            if isinstance(items, dict):
                items = [items]
            if isinstance(wide, dict):
                wide = [wide]
            info["total"] = raw.get("total") or 0
            info["scanned"] = raw.get("scanned") or 0
            info["kinds"] = raw.get("kinds") or {}
            if raw.get("error"):
                log.warning("读控件树失败 hwnd=%s：%s", hwnd, raw["error"])
                info["error"] = str(raw["error"])
                return []
        else:
            items, wide = raw or [], []
        out = self._norm_elements(items)
        if not out and wide:
            rows = self._norm_elements(wide)
            # 渲染面不是按钮：点它的中心就是点窗口正中间，不是用户想要的那一下。
            # 真机上波点音乐整棵树就只有一个 FLUTTERVIEW，报给用户毫无用处
            keep = [e for e in rows if not _is_surface(e["name"])]
            dropped = [e["name"] for e in rows if _is_surface(e["name"])]
            if dropped and not keep:
                info["surface_only"] = dropped[0]
            # 自绘界面：小的更像按钮，大的多半是整块容器，所以按面积从小到大
            out = sorted(keep, key=lambda e: e["rect"][2] * e["rect"][3])
            if out:
                info["widened"] = True
        return out

    def _norm_elements(self, raw):
        """把探测器给的原始行整理成 {name,type,enabled,x,y,rect}，坐标取矩形中心。"""
        out = []
        for e in raw or []:
            if not isinstance(e, dict):
                continue
            name = (e.get("name") or "").strip()
            try:
                x, y = int(e["x"]), int(e["y"])
                w, h = int(e["w"]), int(e["h"])
            except (KeyError, TypeError, ValueError):
                continue
            if not name or w <= 0 or h <= 0:
                continue
            out.append({"name": name, "type": _type_cn(e.get("type")),
                        "enabled": e.get("enabled", True),
                        # 点的是矩形中心，不用 GetClickablePoint（它会抛异常）
                        "x": x + w // 2, "y": y + h // 2,
                        "rect": (x, y, w, h)})
        return out

    def find(self, query, elements):
        """按名字找：先精确、再前缀、再包含。返回候选列表（空 = 没找到）。"""
        q = _norm(query)
        if not q:
            return []
        exact, prefix, part = [], [], []
        for e in elements:
            n = _norm(e["name"])
            if not n:
                continue
            if n == q:
                exact.append(e)
            elif n.startswith(q) or q.startswith(n):
                prefix.append(e)
            elif q in n:
                part.append(e)
        hits = exact or prefix or part
        # 同名同位置的重复项去掉（有些应用会把一个按钮报两遍）
        seen, uniq = set(), []
        for e in hits:
            key = (_norm(e["name"]), e["x"] // 8, e["y"] // 8)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(e)
        return uniq

    def describe(self, win, elements, limit=24, info=None):
        """给手机看的一段话：窗口叫什么、有哪些能点的东西。不含画面，不上传任何图。

        空的时候要说清是**哪一种**空：查失败了、摸到节点但没有一个带名字、还是真的
        一个节点都没有。这三句话对应三种完全不同的下一步动作。
        """
        info = info or {}
        head = "当前窗口：%s" % (win.get("title") or "（没有标题）")
        if win.get("minimized"):
            return head + "\n（它现在是最小化的，看不到也点不了）"
        if not elements:
            if info.get("error"):
                return (head + "\n读控件树这一步本身失败了：%s\n"
                        "（所以不是这个窗口没东西可点，是我没查成。"
                        "在电脑上跑 python app\\wechat_bridge.py probe 能看到原始报错。）"
                        % info["error"])
            if info.get("surface_only"):
                # Flutter/Chromium 系：整个界面就是一块画布，按钮一个都没暴露给系统
                return (head + "\n控件树里只有一块画布（%s），窗口里的按钮一个都没暴露"
                        "给系统——Flutter 和自绘应用就是这样，点它的中心只会点到窗口"
                        "正中间，没意义。\n这种应用只能看图：说「看看画面」，我截一张"
                        "让 Claude 念一遍上面有什么。"
                        % info["surface_only"])
            total = info.get("total") or 0
            if total:
                return (head + "\n摸到 %d 个 UIA 节点，但没有一个是带名字、可见、"
                        "有大小的可点控件%s。这种界面基本是自己画的，"
                        "要我截图看一眼就说「看看画面」。"
                        % (total, self._kinds_text(info.get("kinds"))))
            return (head + "\n控件树一个节点都没有——这个窗口是自己画界面的（游戏、"
                    "部分播放器）。要我截图看一眼就说「看看画面」。")
        names, seen = [], set()
        for e in elements:
            n = e["name"].strip()
            key = _norm(n)
            if not key or key in seen:
                continue
            seen.add(key)
            names.append(n if len(n) <= 14 else n[:13] + "…")
            if len(names) >= limit:
                break
        tail = "" if len(names) < limit else "（还有更多，只列了前 %d 个）" % limit
        note = ("\n（这个窗口没有标准按钮，上面这些是从自绘控件里翻出来的，"
                "点起来不一定准；不行就说「看看画面」。）"
                if info.get("widened") else "")
        return "%s\n能点的：%s%s%s" % (head, "、".join(names), tail, note)

    @staticmethod
    def _kinds_text(kinds):
        """把类型分布写成「（Pane×3、Text×20）」这样一小段，没有就返回空串。"""
        if not kinds:
            return ""
        try:
            top = sorted(kinds.items(), key=lambda kv: -int(kv[1]))[:5]
        except Exception:
            return ""
        return "（类型分布：%s）" % "、".join("%s×%s" % (k, v) for k, v in top)

    # ---- 截图 ----

    def shoot(self, win):
        """截当前窗口（只截它，不截整屏），返回 (png 路径, 图宽, 图高)。"""
        if not os.path.isdir(self.shots_dir):
            os.makedirs(self.shots_dir)
        now = time.time()
        path = os.path.join(self.shots_dir, "shot-%s-%03d.png" % (
            time.strftime("%Y%m%d-%H%M%S", time.localtime(now)),
            int(now * 1000) % 1000))
        iw, ih = self._shooter(path, win["left"], win["top"],
                               win["width"], win["height"])
        self._prune_shots(path)
        return path, int(iw), int(ih)

    def _prune_shots(self, keep_path=None):
        """只留最近 keep_shots 张。截图里可能有聊天窗口、密码框，不该一直堆着。"""
        try:
            files = [os.path.join(self.shots_dir, f)
                     for f in os.listdir(self.shots_dir)
                     if f.startswith("shot-") and f.endswith(".png")]
        except OSError:
            return
        # 排序得有第二把尺子：连着截两张时 mtime 经常**完全并列**（Windows 的系统时钟
        # 约 15 ms 跳一次，比两次截图之间还长），并列时 sort 稳定，谁新谁旧就随 listdir
        # 的顺序，于是该删的那张反倒被当成最新的留下来了。文件名里带到毫秒，正好当尺子。
        files.sort(key=lambda p: (os.path.getmtime(p), os.path.basename(p)),
                   reverse=True)
        for p in files[max(0, self.keep_shots):]:
            if keep_path and os.path.abspath(p) == os.path.abspath(keep_path):
                continue
            try:
                os.remove(p)
            except OSError as exc:
                log.debug("删旧截图失败 %s：%s", p, exc)

    # ---- 坐标换算与点击 ----

    @staticmethod
    def to_screen(win, img_w, img_h, ix, iy):
        """图片里的像素 → 屏幕坐标。

        绝不能把图片坐标直接当屏幕坐标用：系统缩放、窗口不在原点、截图被缩放，
        任一条成立就会偏。所以一律按截图时的那个窗口矩形等比例换算回去。
        """
        iw = max(1, int(img_w))
        ih = max(1, int(img_h))
        x = win["left"] + int(round(float(ix) * win["width"] / iw))
        y = win["top"] + int(round(float(iy) * win["height"] / ih))
        # 夹回窗口范围内，模型给的坐标偶尔会越界
        x = min(max(x, win["left"]), win["left"] + win["width"] - 1)
        y = min(max(y, win["top"]), win["top"] + win["height"] - 1)
        return x, y

    def in_window(self, win, x, y):
        return (win["left"] <= x < win["left"] + win["width"]
                and win["top"] <= y < win["top"] + win["height"])

    def click(self, x, y, double=False):
        """真按下去。调用方必须已经拿到用户的确认并核对过前台窗口。"""
        self._clicker(int(x), int(y), double)
        log.info("点了 (%d,%d)%s", x, y, "（双击）" if double else "")
