# -*- coding: utf-8 -*-
"""「打开波点音乐并开始播放」的后半句：把话变成系统媒体键。

为什么用媒体键、而不是去点应用界面里的按钮：媒体键是 Windows 全局的，正在放音的那个
应用自己会接住。所以不需要看见窗口、不需要知道按钮在哪、也不可能点错东西。代价是只能
做播放器普遍支持的那几件事——播放/暂停、上一首、下一首、停止；搜某首歌、切歌单做不到。

音量也在这里（2026-08-31 加，用户要在微信里控制电脑音量）。音量键管的是**系统主音量**，
不是某个应用的音量，这一点和播放键不同（播放键归最近放音的那个应用）。

**音量键只能相对调，这是键本身的限制**：Windows 一下音量键 = 2%，所以「大声点」就是连按
几下（见 `presses_for`）。想说「调到 30%」或者想知道「现在多少」，键做不到——那要读写
系统混音器，在 volume.py 里，由 `MediaControl(mixer=...)` 接进来。**混音器那条路挂了会自动
退回按键**，所以这个文件永远是能用的那一层。

静音的坑和播放/暂停一样：**系统只有一个 VK_VOLUME_MUTE，静音和取消静音是同一个键**。
所以纯按键的情况下只能说「切了一下」，不能假装真的分得清；能分清是 volume.py 读到当前
静音状态之后的事。

安全边界：发出去的永远是下面这七个固定的虚拟键码。用户给的字符串只用来查表，
不会被拼进任何命令，也不带任何参数。
"""

import logging
import os
import threading
import time

log = logging.getLogger("bridge.media")

VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

KEY_PERCENT = 2          # Windows 按一下音量键就是 2%，这个数不是我定的
DEFAULT_VOL_STEP = 10    # 一句「大声点」调多少（用户选的），换成 5 下键


def presses_for(step_percent):
    """把「调 10%」换成「按 5 下」。至少按一下，不然说了等于没说。"""
    try:
        step = abs(float(step_percent))
    except (TypeError, ValueError):
        step = DEFAULT_VOL_STEP
    return max(1, int(round(step / float(KEY_PERCENT))))


# (动作, 键码, 回话时怎么称呼, 用户可能怎么说)
# 「继续」「开始」单独出现时太像在跟 Claude 说话（「/cc 继续」多半是接着上一件事），
# 所以只收「继续播放」「开始播放」这类带「播放」「放歌」的说法。
# 音量同理不收光秃秃的「大点」「小点」——那两个字更可能在说字号、图片、说话声音。
# 「大声」「小声」收，这两个词单独出现基本只可能是在说音量。
_SPEC = (
    ("play", VK_MEDIA_PLAY_PAUSE, "播放",
     ("开始播放", "继续播放", "播放音乐", "开始放歌", "继续放歌", "放首歌", "开始放",
      "放歌", "播放", "play")),
    ("pause", VK_MEDIA_PLAY_PAUSE, "暂停",
     ("暂停播放", "暂停一下", "先暂停", "暂停", "停一下", "pause")),
    ("next", VK_MEDIA_NEXT, "下一首",
     ("下一首", "下一曲", "换一首", "换首歌", "下首", "切歌", "next")),
    ("prev", VK_MEDIA_PREV, "上一首",
     ("上一首", "上一曲", "前一首", "上首", "previous", "prev")),
    ("stop", VK_MEDIA_STOP, "停止播放", ("停止播放", "停止音乐", "停止放歌")),
    ("vol_up", VK_VOLUME_UP, "调大音量",
     ("调大音量", "音量调大", "音量加大", "把音量调大", "声音调大", "调大声音",
      "声音大一点", "声音大点", "音量大一点", "音量大点", "大声一点", "大声点",
      "大点声音", "大点声", "音量加", "加音量", "音量增大", "大声",
      "volume up", "louder")),
    ("vol_down", VK_VOLUME_DOWN, "调小音量",
     ("调小音量", "音量调小", "音量减小", "把音量调小", "声音调小", "调小声音",
      "声音小一点", "声音小点", "音量小一点", "音量小点", "小声一点", "小声点",
      "小点声音", "小点声", "音量减", "减音量", "音量降低", "降低音量", "小声",
      "volume down", "quieter")),
    ("mute", VK_VOLUME_MUTE, "静音",
     ("静音一下", "先静音", "静音", "别出声", "别出声音", "关掉声音", "关闭声音",
      "把声音关掉", "闭麦", "mute")),
    ("unmute", VK_VOLUME_MUTE, "取消静音",
     ("取消静音", "解除静音", "关掉静音", "恢复声音", "恢复音量", "把声音打开",
      "打开声音", "unmute")),
)

VK = dict((a, vk) for a, vk, _, _ in _SPEC)
LABEL = dict((a, name) for a, _, name, _ in _SPEC)
# 长的排前面：「暂停播放」必须先于「播放」被认出来，「取消静音」必须先于「静音」
_PHRASES = sorted(((p.lower(), a) for a, _, _, ps in _SPEC for p in ps),
                  key=lambda x: -len(x[0]))

VOL_ACTIONS = ("vol_up", "vol_down")
MUTE_ACTIONS = ("mute", "unmute")
_SIGN = {"vol_up": 1, "vol_down": -1}

# 同一个键管播放和暂停，说清楚比装作两个键更省事。静音是同一类坑。
_CAVEAT = {"play": "这个键是开关，本来就在放的话会变成暂停。",
           "pause": "同一个键管播放和暂停，再说一次「播放」就继续。",
           "mute": "系统只有一个静音键，它是开关：本来没静音就变静音，本来静音就恢复。",
           "unmute": "系统只有一个静音键，它是开关：本来静音就恢复，本来没静音就变静音。"}

_NOISE = " \t　:：,，、。!！?？~的了吧呀啊呢哦嘛谢谢"
# 名字和动作之间可能夹的连接词。长的排前面。
_JOIN = ("并且", "然后", "接着", "顺便", "顺手", "并", "再", "就", "，", ",", "、", "；", ";",
         "：", ":", " ", "\t", "　")
_LEAD = ("请", "帮我", "帮忙", "麻烦", "给我", "你", "来", "先")
MAX_LEN = 30                 # 比这还长的消息，多半是在跟 Claude 说事，不当动作看


def _clean(s):
    return (s or "").strip().strip(_NOISE).strip()


def _strip_join(head):
    """摘掉名字和动作之间的连接词：「打开波点音乐，然后下一首」中间那一截。"""
    while True:
        before = head
        head = head.strip().strip(_NOISE)
        for w in _JOIN:
            if head.endswith(w):
                head = head[:-len(w)]
                break
        if head == before:
            return head


def split_tail(body):
    """从消息尾巴上摘掉一个动作短语，返回 (剩下的头部, [动作])。

    只摘一个：连着说两件事的收益不大，多摘一次就多一分误判。
    没摘到就原样返回，调用方那条老路径完全不受影响。
    """
    text = (body or "").strip()
    if not text or len(text) > MAX_LEN:
        return text, []
    cleaned = _clean(text)
    for phrase, action in _PHRASES:
        n = len(phrase)
        if len(cleaned) < n or cleaned[-n:].lower() != phrase:
            continue
        return _strip_join(cleaned[:-n]), [action]
    return text, []


def is_lead_only(head):
    """整条消息就是个动作（前面顶多有「帮我」这类客套），没有应用名。"""
    h = _clean(head)
    changed = True
    while changed and h:
        changed = False
        for w in _LEAD:
            if h.startswith(w):
                h, changed = _clean(h[len(w):]), True
                break
    return not h


def label_of(actions):
    return "、".join(LABEL.get(a, a) for a in actions)


# ---------- 真正按键的那一下（自测里会被替换掉） ----------

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


def _default_send_keys(vks):
    """按下再抬起。媒体键的扫描码属于扩展码，所以要带 EXTENDEDKEY，否则有的机器收不到。"""
    if os.name != "nt":
        raise RuntimeError("这台机器不是 Windows，发不了媒体键")
    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for vk in vks:
        scan = user32.MapVirtualKeyW(vk, 0)
        user32.keybd_event(vk, scan, KEYEVENTF_EXTENDEDKEY, 0)
        time.sleep(0.05)
        user32.keybd_event(vk, scan, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


def _default_spawn(job):
    t = threading.Thread(target=job)
    t.daemon = True
    t.start()


class MediaControl(object):
    """播放控制（播放/暂停/上一首/下一首/停止）+ 系统音量（加/减/静音）。

    音量在这一层只能相对调：`vol_step` 是一句「大声点」调多少个百分点，换算成按几下
    音量键（一下 2%）。绝对音量和「现在多少」不在这里——见模块开头。
    """

    def __init__(self, delay=6.0, sender=None, sleeper=None, spawn=None,
                 vol_step=DEFAULT_VOL_STEP, mixer=None):
        self.delay = max(0.0, float(delay or 0))
        # 0 当没填（步长 0 没有意义），负数按幅度看——方向由动作决定，不由符号决定
        self.vol_step = max(KEY_PERCENT, min(100, abs(int(vol_step or DEFAULT_VOL_STEP))))
        self.presses = presses_for(self.vol_step)
        # 按键换算回来的真实幅度：说 11% 其实按了 6 下 = 12%，回话要报这个数而不是 11
        self.vol_actual = self.presses * KEY_PERCENT
        self._send = sender or _default_send_keys
        self._sleep = sleeper or time.sleep
        self._spawn = spawn or _default_spawn
        # 混音器（volume.py 里的 VolumeMixer，可有可无）。**这里只按名字调
        # nudge_text / mute_text**，不 import volume：那边挂了这个文件还得能用。
        self._mixer = mixer

    def _expand(self, actions):
        """动作列表 → 真正要按的键序列。音量键要连按几下，其余一下。"""
        vks = []
        for a in actions:
            if a not in VK:
                continue
            vks.extend([VK[a]] * (self.presses if a in VOL_ACTIONS else 1))
        return vks

    def _describe(self, actions):
        first = actions[0] if actions else ""
        name = label_of(actions)
        if first in VOL_ACTIONS:
            head = "✅ 已按 %d 下音量键（每下 %d%%，一共约 %s%d%%）。" % (
                self.presses, KEY_PERCENT,
                "+" if _SIGN[first] > 0 else "-", self.vol_actual)
            return head + "系统现在具体是多少我这条路读不到，不够就再说一句。"
        return ("✅ 已按下「%s」键。%s" % (name, _CAVEAT.get(first, ""))).strip()

    def _via_mixer(self, actions):
        """能读写系统混音器时走这条：回话里有具体数字，静音也分得清方向。

        任何异常都往上抛给 run()，由它退回按键——**混音器坏了不能让功能跟着坏**。
        """
        first = actions[0]
        if first in VOL_ACTIONS:
            return self._mixer.nudge_text(_SIGN[first] * self.vol_step)
        return self._mixer.mute_text(first == "mute")

    def run(self, actions):
        """返回 (回复文本, 是否算命中)——和 AppLauncher 同一个约定。"""
        first = actions[0] if actions else ""
        if self._mixer is not None and first in (VOL_ACTIONS + MUTE_ACTIONS):
            try:
                text = self._via_mixer(actions)
                log.info("走混音器 %s：%s", actions, text)
                return text, True
            except Exception as exc:      # COM / PowerShell 出岔子的花样太多，全兜住
                log.warning("混音器没走通，退回音量键：%s", exc)
        vks = self._expand(actions)
        if not vks:
            return None, False
        name = label_of(actions)
        try:
            self._send(vks)
        except Exception as exc:              # 按键失败的花样太多，全兜住
            log.exception("发媒体键失败 %s", actions)
            return "❌ 按「%s」键失败：%s" % (name, exc), True
        log.info("发媒体键 %s（%d 下）", actions, len(vks))
        return self._describe(actions), True

    def run_later(self, actions):
        """等应用起来再按。轮询线程里绝不能 sleep，所以扔给一个短命线程。"""
        def job():
            if self.delay:
                self._sleep(self.delay)
            text, _ = self.run(actions)
            log.info("延迟按键结果：%s", text)
        self._spawn(job)

    # ---------- 绝对音量：**只有混音器能做**，没有就直说 ----------
    #
    # 按键那条路读不到当前值，也就算不出「从现在到 30%」该按几下，硬按等于瞎调。
    # 所以这两个方法没有退回按键的分支：宁可回一句「这条路没通」，也不假装做到了。

    def _mixer_say(self, tmpl, exc):
        return tmpl % (str(exc)[:120],)

    def set_volume(self, percent):
        """「音量调到 30%」。返回 (回复文本, 是否算命中)。"""
        pct = max(0, min(100, int(percent)))
        if self._mixer is None:
            return None, False            # 交给 Claude，别在这儿瞎猜
        try:
            return self._mixer.set_text(pct), True
        except Exception as exc:
            log.warning("设绝对音量失败：%s", exc)
            return ("❌ 想把音量设到 %d%%，但我读不到系统混音器（%s）。"
                    "说「大声点」「小声点」这类相对调整还是能用的。"
                    % (pct, str(exc)[:120])), True

    def query_volume(self):
        """「音量多少」。返回 (回复文本, 是否算命中)。"""
        if self._mixer is None:
            return None, False
        try:
            return self._mixer.query_text(), True
        except Exception as exc:
            log.warning("读当前音量失败：%s", exc)
            return self._mixer_say("❌ 读不到当前音量（%s）。这条路要系统混音器，"
                                   "只有按键的话是读不出数的。", exc), True
