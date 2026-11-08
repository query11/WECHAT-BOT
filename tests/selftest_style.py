# -*- coding: utf-8 -*-
"""回话文案的规矩：会发到微信的每一句话都要带状态记号。

为什么单独一组：2026-09 给回话加 ✅/⏳/❌ 那一轮是**手工**改的三十多处、漏了六处，
靠人眼复看才逮出来——同一条「读不到前台窗口，没法截图。」在两个方法里各有一份，
改了一份漏了另一份。漏了不报错、不崩，只是用户偶尔看见一句没有记号的话，而这种事
没人会专门去发现。所以把它变成一条会红的断言。

判据（只管「这句话会不会发到微信」）：
  · `self.reply(msg, "...")` / `self._push(uid, "...")` 的第二个参数是字面量；
  · `return "..."` 的字面量（这些文件里的 return 基本就是把回话交回给桥）；
  · `.append("...")` 的字面量（多行回话就是一行行拼出来再 join 的）；
  · 且字面量里有中文字符。
命中的每一句必须以九个记号之一开头，或者在 `_NOT_A_REPLY` 里写明理由。

那个名单还要**反向核对**（见文件末尾第二个 check）：留着一条已经用不上的，就说明文案
改了而名单没跟着清——名单一旦开始发霉，这条断言就会退化成「照旧绿」的摆设。

**这个检查管不到的地方**（别当成全覆盖）：
  · 用 `+` 拼起来的（`return head + "\n控件树里只有一块画布…"`）——第一个字面量不是
    句首，抓不到；screen_control.describe 那几个诊断块就是这种；
  · 一条回话的第 2..n 段（`return ("❌ 第一行"\n "第二行")`）——只看句首，后面几段
    本来就不该带记号；
  · 不是一条独立回话的片段：`power._dur` 的「1 分钟」、`volume._mute_tail` 的括号、
    `app_actions._CAVEAT` 那种字典值——单独给它们加记号会拼出
    「✅ ……（不过现在是静音）」这种怪话。
"""

import io
import os
import re

from tests.selftest_base import check

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

# 十个记号，和 使用速查.md 的「它回话的排版」那一节一一对应。
# ⏹ 是 2026-09 加 /stop 时补的：叫停和「没办成」（❌）是两回事，一个是你自己喊停的，
# 一个是它没干成；混成一个记号，回头看记录就分不出当时发生了什么。
MARKS = (u"\u2705", u"\u23f3", u"\u274c", u"\U0001f515", u"\u26a0",
         u"\U0001f446", u"\u23f0", u"\U0001f4ce", u"\u23f1", u"\u23f9")

# 会发回话的文件。故意不含 selftest_*（那些是检查用的假回话）和 ilink_client.py
# （那里的中文是登录时打在控制台给人看的，不经过微信）
SCAN = ["wechat_bridge.py", "bridge_chat.py", "bridge_meeting.py",
        "bridge_files.py", "bridge_media.py", "bridge_power.py",
        "bridge_screen.py", "bridge_remind.py", "bridge_admin.py", "bridge_inbox.py",
        "power.py", "volume.py", "app_launcher.py", "app_actions.py", "wemeet.py",
        "claude_runner.py", "screen_control.py"]

_HAN = re.compile(u"[\u4e00-\u9fff]")
# 第二个参数紧跟字面量的回话调用；第一个参数最多 60 字（`msg` / `uid` 这种）
_CALL = re.compile(r"\.(?:reply|_push)\([^,]{1,60},\s*\(?\s*\"", re.S)
# 缩进过的 return 字面量（模块级的 return 不会是回话）
_RET = re.compile(r"^[ \t]+return\s+\(?\s*\"", re.M)
# 多行回话是一行行 append 出来再 join 的（_plan 的候选列表就是），也当回话看
_APPEND = re.compile(r"\.append\(\s*\(?\s*\"")

# 不是（整条）回话的东西：清单、提问、片段、引导语、控制台输出……
# 每条都要真的被命中一次，否则文件末尾那个反向核对会红。
_NOT_A_REPLY = [
    (u"当前用户最近收到的附件", "模型输入上下文，不是发到微信的回话"),
    # ---- 清单 / 提问：本来就不该带「成没成」的记号 ----
    (u"我能直接打开的应用", "清单：应用名罗列"),
    (u"开着的窗口：", "清单：窗口罗列"),
    (u"现在一个能操作的窗口都没有", "清单：窗口罗列的空态"),
    (u"待进的会：", "清单：定下的会"),
    (u"没有定下的会。", "清单：定下的会的空态"),
    (u"· 正在等你确认", "清单项：正在等你确认的那一场"),
    (u"· %s 那场还差会议号", "清单项：还差会议号的那一场"),
    (u"把会议号发我", "提问：等用户给号"),
    (u"回数字我就", "提问：等用户回数字，跟在 ⚠️ 那行后面"),
    # ---- 提问 / 清单：不该带「成没成」的记号 ----
    (u"要提醒你什么？", "提问：只给了时间没给内容，回来问一句"),
    (u"要我到点跑什么？", "提问：定时任务只给了时间、没说要跑什么，回来问一句"),
    (u"没有记下的提醒。", "清单：空清单时的引导"),
    (u"说「取消提醒」全取消", "清单末尾的用法提示"),
    # ---- 片段：会被拼进别的话里，单独加记号会拼出怪话 ----
    (u"（%d 分钟不说话会自动退出", "片段：对话模式的空闲提示"),
    (u"（不过现在是静音", "片段：音量的括号补充"),
    (u"在对话模式里", "片段：/parse 报告的状态行，由桥拼成一行"),
    (u"不在对话模式", "片段：同上"),
    (u"有待确认的点击", "片段：同上"),
    (u"有一条问话在等你回", "片段：同上"),
    (u"记了时间、还等你的会议号", "片段：同上"),
    (u"正在等确认的那场", "片段：拼进「取消了：…」那句里"),
    (u"%s 那场", "片段：拼进「取消了：…」那句里"),
    (u"%s 那个还没给号的", "片段：拼进「取消了：…」那句里"),
    (u"（类型分布", "片段：诊断的括号补充"),
    (u"（还有 %d 个没列）", "片段：窗口清单的省略提示"),
    (u"%d 秒", "片段：power._dur 的时长说法（「分 %d 秒」也含在这儿）"),
    (u"%d 小时", "片段：power._dur 的时长说法"),
    (u"%d 分钟", "片段：power._dur 的时长说法"),
    # ---- 诊断块最后那行罗列 ----
    (u"能点的", "诊断：控件罗列（screen_control.describe 的最后一行）"),
    # ---- 答案 / 引导语：问什么答什么，不是「成没成」 ----
    (u"你的用户 ID：", "/id 的答案，也是白名单为空时那句引导的一部分"),
    (u"工作目录：", "/where 的答案"),
    # ---- bridge_admin：/status /log /queue 这几条是「问什么答什么」，不是「成没成」
    # （和 /id、/where、「开着的窗口：」同理），头一行不带记号 ----
    (u"📊 桥的状态", "报告：/status 的头一行，内容是状态罗列"),
    (u"📄 bridge.log 最后", "报告：/log 的头一行，后面直接跟日志原文"),
    (u"📋 队列", "清单：/queue 的头一行，内容是队列罗列"),
    # ---- 片段：拼进上面那几条报告里，单独加记号会拼出怪话 ----
    (u"现在没有在跑的任务", "片段：拼进 /status 和 /queue 的「正在跑：…」那一行"),
    (u"正在跑：%s（跑了 %s）", "片段：同上"),
    (u"定下的会 %d 条", "片段：和「记下的提醒」俩拼成一行写进 /status"),
    (u"记下的提醒 %d 条", "片段：同上"),
    (u"另一个用户的任务", "片段：别人的任务不露原话，只报这一句"),
    # ---- 片段：/status、/queue 里「正在跑的那条」叫什么（_job_title）----
    (u"把文件发到微信", "片段：_job_title 给发文件那条活儿起的名字"),
    (u"取回任务产物", "片段：_job_title 给取回产物那条活儿起的名字"),
    (u"看看画面（截图让它念）", "片段：_job_title 给看看画面那条活儿起的名字"),
    (u"看画面找坐标（点一下那条）", "片段：_job_title 给看图找坐标那条活儿起的名字"),
    # ---- /reload 那条 ✅ 回话的续行 ----
    (u"凭据（token / 账号 / 地址）和日志设置都没动。", "片段：/reload 成功那条回话的最后一行"),
    (u"（认不出来的队列项）", "片段：队列里出现认不出的项时的占位"),
    (u"%d 字节", "片段：_human_kb 的字节说法"),
    (u"上下文已清空", "/new 的回执"),
    (u"「%s」现在的画面", "「看看画面」念回来的内容，和 Claude 的回复一样不带记号"),
    # ---- bridge_inbox：收附件那几条回话的续行 / 片段 ----
    (u"要我看就说一句", "回话末尾的引导语：告诉你怎么让我读那个文件"),
    (u"（下载加解密花了", "片段：下载耗时的括号补充，跟在落盘那条后面"),
    # ---- bridge_inbox：腾地方那条问话、以及 _ago 的时长说法 ----
    (u"不知道多久前", "片段：_ago 说不清多久以前时的那句"),
    (u"%d 天前", "片段：_ago 的时长说法（分钟/小时那两条沿用 power._dur 的名单）"),
    (u"还有 %d 个", "片段：问话里「最老的三个之外还有 N 个」"),
    (u"回「删掉」我就删了再收这条", "片段：⚠ 问话的最后一行（告诉用户回什么）"),
]


def _literals():
    """扫出所有「会发到微信的」字面量开头。返回 [(文件, 行号, 开头 30 字)]。"""
    out = []
    for name in SCAN:
        path = os.path.join(HERE, name)
        text = io.open(path, encoding="utf-8").read()
        for rx in (_CALL, _RET, _APPEND):
            for m in rx.finditer(text):
                head = text[m.end():m.end() + 30].split("\n")[0]
                if not _HAN.search(head):
                    continue
                line = text.count("\n", 0, m.start()) + 1
                out.append((name, line, head))
    return out


def test_reply_marks():
    """每一句回话要么带记号，要么在名单里写明为什么不用带。"""
    found = _literals()
    check("扫得到回话字面量（扫不到说明判据写崩了）", len(found) > 60, len(found))

    unmarked, used = [], set()
    for name, line, head in found:
        if any(head.startswith(m) for m in MARKS):
            continue
        hit = None
        for i, (snippet, _why) in enumerate(_NOT_A_REPLY):
            if snippet in head:
                hit = i
                break
        if hit is None:
            unmarked.append("%s:%d %s" % (name, line, head[:24]))
        else:
            used.add(hit)

    check("没有漏记号、也不在名单里的回话",
          not unmarked,
          "｜".join(unmarked[:12]))
    stale = [_NOT_A_REPLY[i][0] for i in range(len(_NOT_A_REPLY)) if i not in used]
    check("名单里没有已经用不上的条目（文案改了要顺手清名单）",
          not stale, "｜".join(stale[:12]))
