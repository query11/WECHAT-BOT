# -*- coding: utf-8 -*-
"""按名字打开 / 关闭本机应用。识别桌面、开始菜单中有效的应用启动项，以及已安装的 Microsoft Edge。

为什么不交给 Claude 去执行命令：无头模式下 Bash 类命令一律要审批，微信那头点不了；
而给它开 `Start-Process:*` 之类的白名单等于把整台机器的命令行交给微信。
这里改成桥接层自己干，只认名字、不收任何命令行参数，顺带把 40 秒的等待变成 1 秒。

安全边界（改代码前先读这几条）：
  - 候选集为桌面 .lnk / .url / .exe 和固定识别的已安装 Edge，用户文字只用于匹配
  - 关闭走 taskkill 不带 /F（普通关闭，未存的文档还有机会弹保存框）
  - PROTECTED 里的进程名一律拒关：桥自己是 python、Claude 是 claude.exe，
    杀了就等于自断经脉；explorer 之类杀了用户得重登桌面
"""

import json
import logging
import os
import re
import subprocess

log = logging.getLogger("bridge.apps")

SCAN_EXTS = (".lnk", ".url", ".exe")

# 即使桌面上真有指向它们的快捷方式，也不许关
PROTECTED = frozenset([
    "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "svchost.exe", "csrss.exe", "winlogon.exe", "wininit.exe", "smss.exe",
    "lsass.exe", "services.exe", "dwm.exe", "taskmgr.exe", "rundll32.exe",
    "msiexec.exe", "regedit.exe", "mmc.exe", "system", "system idle process",
    # 桥自己和 Claude 自己
    "python.exe", "pythonw.exe", "py.exe", "claude.exe", "node.exe",
])

# 动词表：中文的允许紧跟名字（「打开微信」），英文的必须有空格或冒号隔开，
# 否则 "startup" 会被拆成 start + up。长的写在前面，正则的选择分支是有序的。
#
# 客套/重试口气都算前缀：真机上「再试一下打开腾讯会议」就是因为「再试一下」不在
# 这张表里、而正则从句首锚定，整条不匹配、白花了一次额度。长的必须排在短的前面
# （「再试一下」要先于「再」被吃掉），否则 `再` 先匹配、剩下「试一下打开…」又不匹配。
_LEAD_WORDS = ("请", "帮我", "帮忙", "麻烦", "给我", "能不能", "可不可以", "可以",
               "再试一下", "再试试", "试一下", "试试", "现在", "顺便", "你", "再", "快")
_LEAD = r"^(?:(?:%s)\s*){0,2}" % "|".join(sorted(_LEAD_WORDS, key=len, reverse=True))
_CN_OPEN = "打开|开启|启动|开一下|运行|跑一下|开"
_EN_OPEN = "open|start|launch"
_CN_CLOSE = "关闭|关掉|关一下|退出|结束|停掉|关"
_EN_CLOSE = "close|quit|exit|stop|kill"


def _verb_re(cn, en):
    return re.compile(
        _LEAD + r"(?:(?:%s)(?:一下)?\s*[:：]?\s*|(?:%s)(?:\s+|\s*[:：]\s*))(.+)$" % (cn, en),
        re.IGNORECASE)


OPEN_RE = _verb_re(_CN_OPEN, _EN_OPEN)
CLOSE_RE = _verb_re(_CN_CLOSE, _EN_CLOSE)

# 名字后面常跟的语气词和废话，匹配前先剃掉
TAIL_NOISE = ("一下", "程序", "客户端", "软件", "应用程序", "这个应用", "这个软件", "这个")
TAIL_CHARS = " \t　:：,，、。!！?？~。的了吧呀啊呢哦嘛谢谢"

# 一旦出现这些，就说明用户想说的不是「开个应用」，交回 Claude
NOT_AN_APP = ("\\", "/", "http", "然后", "并且", "以及", "，", ",", "。", ";", "；",
              "文件夹", "目录", "网页", "链接", "网址", "文档")
MAX_QUERY_LEN = 20


# ---------- 名字清洗与句式解析 ----------

def _norm(s):
    """比名字时忽略大小写、空格和连接符：「CC Switch」和「ccswitch」算同一个。"""
    return re.sub(r"[\s\-_.·]+", "", (s or "").strip().lower())


def _basename(path):
    """快捷方式里的目标永远是 Windows 路径，别指望 os.path 在非 Windows 上会拆反斜杠。"""
    return re.split(r"[\\/]", (path or "").strip().strip('"'))[-1]


def _strip_tail(q):
    """剃掉尾巴上的语气词和废话，直到不再变短。"""
    q = (q or "").strip()
    while True:
        before = q
        q = q.strip(TAIL_CHARS)
        for w in TAIL_NOISE:
            if len(q) > len(w) and q.endswith(w):
                q = q[:-len(w)]
        if q == before:
            return q.strip()


def _looks_like_app_name(q):
    """只有像「一个应用的名字」才走快速通道，像句子或路径就交回 Claude。"""
    if not q or len(q) > MAX_QUERY_LEN:
        return False
    low = q.lower()
    if any(bad in low for bad in NOT_AN_APP):
        return False
    if len(q.split()) > 3:
        return False
    return True


def parse_command(body):
    """解析成 (动作, 名字)；不像开关应用就返回 (None, None)。

    显式的 /open、/close 不做句式判断（用户明说了），但名字仍要过白名单。
    """
    text = (body or "").strip()
    if not text:
        return None, None
    low = text.lower()
    for cmd, action in (("/open", "open"), ("/close", "close"), ("/kill", "close")):
        if low == cmd or low.startswith(cmd + " ") or low.startswith(cmd + "　"):
            return action, _strip_tail(text[len(cmd):])
    for pattern, action in ((CLOSE_RE, "close"), (OPEN_RE, "open")):
        m = pattern.match(text)
        if not m:
            continue
        q = _strip_tail(m.group(1))
        return (action, q) if _looks_like_app_name(q) else (None, None)
    return None, None


# ---------- 桌面在哪 ----------

def find_desktop_dirs(extra=None):
    """桌面可能被重定向（这台机器就重定向到 D:\\OneDrive\\Desktop），所以先问注册表。"""
    cands = [str(x) for x in (extra or []) if str(x).strip()]
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        for hive, sub, key in (
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", "Desktop"),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders", "Desktop"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
             "Common Desktop"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val = winreg.QueryValueEx(k, key)[0]
            except OSError:
                continue
            if val:
                cands.append(os.path.expandvars(val))
    home = os.path.expanduser("~")
    cands += [os.path.join(home, "Desktop"), os.path.join(home, "OneDrive", "Desktop")]
    if os.environ.get("OneDrive"):
        cands.append(os.path.join(os.environ["OneDrive"], "Desktop"))
    if os.environ.get("PUBLIC"):
        cands.append(os.path.join(os.environ["PUBLIC"], "Desktop"))
    out, seen = [], set()
    for d in cands:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(os.path.abspath(d))
    return out


# Only this installed executable is added; message text never becomes a command.
def installed_browser_apps():
    candidates=[]
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                            0, winreg.KEY_READ | flag) as key:
                        candidates.append(winreg.QueryValue(key, None))
                except OSError:
                    pass
    except ImportError:
        pass
    for name in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        if os.environ.get(name):
            candidates.append(os.path.join(os.environ[name], 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        path=os.path.expandvars(raw.strip().strip('"'))
        if os.path.isabs(path) and _basename(path).lower() == 'msedge.exe' and os.path.isfile(path):
            return {'Microsoft Edge': os.path.abspath(path)}
    return {}


_BROWSER_ALIASES = {
    'edge浏览器': 'edge', '微软edge': 'edge', '微软edge浏览器': 'edge',
    'microsoftedge浏览器': 'microsoftedge', '微软浏览器': 'microsoftedge',
}


def find_start_menu_dirs():
    roots=[]
    for key in ('APPDATA', 'ProgramData'):
        value=os.environ.get(key)
        if value:
            path=os.path.join(value, 'Microsoft', 'Windows', 'Start Menu', 'Programs')
            if os.path.isdir(path):roots.append(path)
    return roots


_NOT_LAUNCH_ENTRY = re.compile(
    r'卸载|uninstall|unins|installer|(?:^|[ _-])setup|安装|帮助|help|manual|documentation|'
    r'readme|release notes|官方网站|官方论坛|意见反馈|语言首选项|software development kit', re.I)
_APP_FAMILIES = (
    ({"edge","microsoftedge"}, {"edge","microsoftedge"}),
    ({'微信','wechat','weixin','微信客户端','微信电脑版'}, {'微信','wechat','weixin','wechatforwindows','微信电脑版'}),
    ({'chrome','googlechrome','谷歌','谷歌浏览器','chrome浏览器','googlechrome浏览器'}, {'chrome','googlechrome','谷歌浏览器'}),
    ({'word','微软word'}, {'word','microsoftword'}),
    ({'excel','微软excel','excel表格'}, {'excel','microsoftexcel'}),
    ({'powerpoint','ppt','微软ppt','幻灯片软件'}, {'powerpoint','microsoftpowerpoint'}),
    ({'onenote','微软笔记'}, {'onenote','microsoftonenote'}),
    ({'7zip','7z','7zipfilemanager','解压软件'}, {'7zipfilemanager','7zip'}),
    ({'clash','clashverge','clashvergerev'}, {'clashverge','clashvergerev'}),
    ({'vscode','visualstudiocode'}, {'visualstudiocode','vscode'}),
    ({'火狐','火狐浏览器','firefox','firefox浏览器'}, {'firefox','mozillafirefox'}),
)


def _known_app_query(query):
    q=_norm(_strip_tail(query))
    return q in _BROWSER_ALIASES or any(q in aliases for aliases,_ in _APP_FAMILIES)


# ---------- 真正动手的三个动作（自测里会被替换掉） ----------

def _decode(raw):
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        import locale
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _default_open(path):
    """用 ShellExecute 打开，快捷方式自带的参数和工作目录才会照它自己的来。"""
    starter = getattr(os, "startfile", None)
    if starter is None:
        raise RuntimeError("这台机器不是 Windows，没有 os.startfile")
    starter(path)


def _default_close(exe_name):
    """不带 /F：这是「请你关掉」，未保存的东西还能弹保存框。返回 (返回码, 输出)。"""
    proc = subprocess.run(["taskkill", "/IM", exe_name],
                          capture_output=True, timeout=30, shell=False)
    return proc.returncode, (_decode(proc.stdout) + _decode(proc.stderr)).strip()


# 中文路径不能直接写进 PowerShell 命令行（编码一乱就全废），所以进出都走 base64；
# 整条命令里一个双引号都没有，免得 Windows 那套 argv 引号规则再插一脚
_PS_RESOLVE = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$paths=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')) -split [char]10;"
    "$sh=New-Object -ComObject WScript.Shell;"
    "$out=@{};"
    "foreach($p in $paths){if($p.Length -gt 0){$t=$sh.CreateShortcut($p).TargetPath;"
    "if($t.Length -gt 0){$out[$p]=$t}}};"
    "$j=$out|ConvertTo-Json -Compress;"
    "if($j.Length -eq 0){$j='{}'};"
    "[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)))"
)


def _resolve_targets_via_powershell(paths):
    """一次 PowerShell 把多个 .lnk 的目标全解析出来（只在要关程序时才用得上）。"""
    import base64
    if len(paths)>1 and len("\n".join(paths)) > 9000:
        result={}
        size=max(1,min(24,len(paths)//2))
        for i in range(0,len(paths),size):
            result.update(_resolve_targets_via_powershell(paths[i:i+size]))
        return result
    if not paths:
        return {}
    blob = base64.b64encode("\n".join(paths).encode("utf-8")).decode("ascii")
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_RESOLVE % blob]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("解析快捷方式目标失败：%s", exc)
        return {}
    raw = _decode(proc.stdout).strip()
    if not raw:
        log.warning("解析快捷方式目标：PowerShell 没输出，stderr=%s",
                    _decode(proc.stderr).strip()[:300])
        return {}
    try:
        obj = json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("解析快捷方式目标：输出不是预期格式（%s）：%s", exc, raw[:120])
        return {}
    return {k: v for k, v in (obj or {}).items() if isinstance(v, str) and v}


class AppLauncher(object):
    """只接受已发现的启动项；用户文字不成为路径或命令参数。"""

    def __init__(self, dirs=None, extra_dirs=None, allow_close=True, cache_file=None,
                 opener=None, closer=None, resolver=None, installed_apps=None, start_menu_dirs=None, validate_shortcuts=False, preferences_file=None):
        self.dirs = list(dirs) if dirs is not None else find_desktop_dirs(extra_dirs)
        self.start_menu_dirs = list(start_menu_dirs or [])
        self.validate_shortcuts = bool(validate_shortcuts)
        self._identities = {}
        self.preferences_file = preferences_file
        self.allow_close = bool(allow_close)
        self.cache_file = cache_file
        self._open = opener or _default_open
        self._close = closer or _default_close
        self._resolve = resolver or _resolve_targets_via_powershell
        self._installed_apps = dict(installed_apps or {})
        self._apps = []                 # [(显示名, 完整路径)]，顺序即优先级
        self._targets = self._load_cache()
        self.refresh()

    # ---------- 扫桌面 ----------

    def refresh(self):
        entries=[]
        for directory in self.dirs:
            try:
                for filename in sorted(os.listdir(directory)):
                    name,ext=os.path.splitext(filename)
                    path=os.path.join(directory,filename)
                    if ext.lower() in SCAN_EXTS and name.strip() and os.path.isfile(path):
                        entries.append((name,path,False))
            except OSError:
                log.warning('无法读取一个应用目录')
        for directory in self.start_menu_dirs:
            if os.path.basename(os.path.normpath(directory)).lower() in ('startup','启动'):continue
            for base,dirs,files in os.walk(directory,followlinks=False):
                dirs[:]=[d for d in dirs if d.lower() not in ('startup','启动') and not os.path.islink(os.path.join(base,d))]
                for filename in sorted(files):
                    name,ext=os.path.splitext(filename)
                    if ext.lower()=='.lnk' and not _NOT_LAUNCH_ENTRY.search(name):
                        entries.append((name,os.path.join(base,filename),True))
        pending=[p for _,p,menu in entries if p.lower().endswith('.lnk') and
                 (menu or self.validate_shortcuts) and self._cached(p) is None]
        if pending:
            resolved=self._resolve(list(dict.fromkeys(pending)))
            for path in pending:
                self._targets[path]={'target':resolved.get(path,''),'mtime':self._mtime(path)}
            self._save_cache()
        apps=[];seen=set();identities={}
        for name,path,menu in entries:
            target=path
            if path.lower().endswith('.lnk') and (menu or self.validate_shortcuts):
                target=self._cached(path) or ''
                if not os.path.isfile(target):continue
                if menu and (not target.lower().endswith('.exe') or _basename(target).lower() in PROTECTED or
                             _NOT_LAUNCH_ENTRY.search(_basename(target))):continue
            ident=os.path.normcase(os.path.realpath(target))
            key=(_norm(name),ident)
            if key in seen:continue
            seen.add(key);identities[path]=ident;apps.append((name,path))
        for name,path in self._installed_apps.items():
            ident=os.path.normcase(os.path.realpath(path));key=(_norm(name),ident)
            if key not in seen and os.path.isfile(path):
                seen.add(key);identities[path]=ident;apps.append((name,path))
        self._base_names={path:_norm(name) for name,path in apps}
        counts={}
        for name,path in apps:counts[_norm(name)]=counts.get(_norm(name),0)+1
        ordinals={};named=[]
        for name,path in apps:
            normalized=_norm(name)
            if counts[normalized]>1:
                ordinals[normalized]=ordinals.get(normalized,0)+1
                name='%s（%d）'%(name,ordinals[normalized])
            named.append((name,path))
        self._apps=named;self._identities=identities
        return named

    def _unique_targets(self, hits):
        found=[];seen=set()
        for name,path in hits:
            ident=self._identities.get(path,os.path.normcase(os.path.abspath(path)))
            if ident not in seen:
                seen.add(ident);found.append((name,path))
        return found

    def _preferences(self):
        if not self.preferences_file:return {'favorites': [], 'aliases': {}}
        from app_preferences import load_preferences
        return load_preferences(self.preferences_file)

    def _custom_alias(self,query):
        from app_preferences import validate_preferences
        normalized=_norm(_strip_tail(query))
        matching=[(a,t) for a,t in self._preferences()['aliases'].items() if _norm(a)==normalized]
        if not matching:return None
        if len(matching)!=1:return []
        alias,target=matching[0]
        try:validate_preferences({'favorites': [], 'aliases': {alias:target}},self)
        except ValueError:return []
        return self._unique_targets([(n,p) for n,p in self._apps if self._identities.get(p)==target])

    def names(self):
        return [n for n, _ in self._apps]

    def describe(self):
        if not self.dirs and not self.start_menu_dirs and not self._apps:
            return "❌ 没找到应用目录，请检查桌面和开始菜单。"
        if not self._apps:
            return "❌ 没找到有效的应用启动项（扫描目录：%s）。" % "、".join(self.dirs + self.start_menu_dirs)
        favored=set(self._preferences()["favorites"])
        favorites=[n for n,p in self._apps if self._identities.get(p) in favored]
        lead="常用应用："+"、".join(favorites)+"\n\n" if favorites else ""
        return "%s我能直接打开的应用（%d 个）：\n%s\n\n说「打开<名字>」就行%s。" % (
            lead, len(self._apps), "、".join(self.names()),
            "，「关闭<名字>」也认" if self.allow_close else "")

    # ---------- 匹配 ----------

    def match(self, query):
        """返回候选 [(名字, 路径)]：空=没这个应用，多个=有歧义，得回去问。"""
        custom = self._custom_alias(query)
        if custom is not None:return custom
        q = _norm(_strip_tail(query))
        if not q or not self._apps:
            return []
        display_exact=[(n,p) for n,p in self._apps if _norm(n)==q]
        if display_exact:return self._unique_targets(display_exact)
        pairs = [(n, p, self._base_names.get(p,_norm(n))) for n, p in self._apps]
        exact = [(n,p) for n,p,k in pairs if k==q]
        if exact:return self._unique_targets(exact)
        q = _BROWSER_ALIASES.get(q,q)
        exact = [(n,p) for n,p,k in pairs if k==q]
        if exact:return self._unique_targets(exact)
        for aliases,targets in _APP_FAMILIES:
            if q in aliases:
                return self._unique_targets([(n,p) for n,p,k in pairs if k in targets])
        # Never guess a typo. Partial names may yield several explicit candidates.
        return self._unique_targets([(n,p) for n,p,k in pairs if q in k])

    def _ambiguous(self, query, hits):
        return "⚠️ 找到 %d 个像「%s」的：%s。说全一点我再动手。" % (
            len(hits), query, "、".join(n for n, _ in hits[:6]))

    # ---------- 快捷方式 → 进程名（只有关程序才需要） ----------

    def _load_cache(self):
        if not self.cache_file:
            return {}
        try:
            with open(self.cache_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (IOError, OSError, ValueError):
            return {}

    def _save_cache(self):
        if not self.cache_file:
            return
        try:
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._targets, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cache_file)
        except (IOError, OSError) as exc:
            log.debug("写快捷方式缓存失败（忽略）：%s", exc)

    @staticmethod
    def _mtime(path):
        try:
            return int(os.path.getmtime(path))
        except OSError:
            return 0

    def _cached(self, path):
        rec = self._targets.get(path)
        if isinstance(rec, dict) and rec.get("mtime") == self._mtime(path):
            return rec.get("target") or ""
        return None

    def exe_name(self, path):
        """.exe 就是自己；.lnk 要解析出目标；.url 没有进程可言。"""
        low = path.lower()
        if low.endswith(".exe"):
            return _basename(path)
        if not low.endswith(".lnk"):
            return None
        target = self._cached(path)
        if target is None:
            pending = [p for _, p in self._apps
                       if p.lower().endswith(".lnk") and self._cached(p) is None]
            if path not in pending:
                pending.append(path)
            got = self._resolve(pending)
            for p in pending:
                self._targets[p] = {"target": got.get(p, ""), "mtime": self._mtime(p)}
            self._save_cache()
            target = self._cached(path) or ""
        return _basename(target) if target else None

    # ---------- 对外的两个动作 ----------

    def open(self, query):
        """返回 (回复文本, 是否算命中)。没命中回 (None, False)，调用方照旧交给 Claude。"""
        hits = self.match(query)
        if not hits:
            if _known_app_query(query) or self._custom_alias(query) is not None:
                return "❌ 没找到「%s」的可用启动项。可发 /apps 查看应用列表，或检查快捷方式是否失效。" % query, True
            return None, False
        if len(hits) > 1:
            return self._ambiguous(query, hits), True
        name, path = hits[0]
        if not os.path.exists(path):
            self.refresh()
            return "❌ 「%s」的启动项不在了，可能被挪走或改名了。" % name, True
        try:
            self._open(path)
        except Exception as exc:                      # 启动失败的花样太多，全兜住
            log.exception("启动失败 %s", path)
            return "❌ 启动「%s」失败：%s" % (name, exc), True
        log.info("启动应用 name=%s path=%s", name, path)
        return "⏳ 已请求打开 %s；尚未确认窗口是否出现。可发 /see %s 查看窗口。" % (name, name), True

    def close(self, query):
        if not self.allow_close:
            return None, False
        hits = self.match(query)
        if not hits:
            if _known_app_query(query) or self._custom_alias(query) is not None:
                return "❌ 没找到「%s」的可用启动项。可发 /apps 查看应用列表，或检查快捷方式是否失效。" % query, True
            return None, False
        if len(hits) > 1:
            return self._ambiguous(query, hits), True
        name, path = hits[0]
        exe = self.exe_name(path)
        if not exe:
            return "❌ 解析不出「%s」对应的进程，这个得你手动关。" % name, True
        if not re.fullmatch(r'[A-Za-z0-9_ .()\-\u0080-\uffff]+\.exe',exe,re.I):
            return "❌ 「%s」没有可安全匹配的程序进程名，请手动关闭。" % name, True
        if exe.lower() in PROTECTED:
            return "❌ 「%s」指向的是 %s——系统进程或者我自己赖以运行的东西，不能关。" % (name, exe), True
        try:
            code, out = self._close(exe)
        except Exception as exc:
            log.exception("关闭失败 %s", exe)
            return "❌ 关「%s」的时候出错了：%s" % (name, exc), True
        log.info("关闭应用 name=%s exe=%s 返回码=%s", name, exe, code)
        if code == 0:
            return "✅ 已请求关闭 %s（%s）。它可能会弹保存框，没反应就手动关一下。" % (name, exe), True
        if code == 128:
            return "🔕 %s 现在没在跑。" % name, True
        return "❌ 没关掉 %s（返回码 %s）：%s" % (name, code, (out or "无输出")[-200:]), True

