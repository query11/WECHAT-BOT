# -*- coding: utf-8 -*-
"""把一句话喂给各个**纯解析器**，看它们各自认成什么。

给人用的：微信里发「/parse 打开聊天」，它按分发顺序把这句话过一遍，告诉你哪条通道会
接走它、还是全都不认（全不认就是交给 Claude，花一次额度）。

**只调用纯函数**——一个动作都不做、一个文件都不碰、不花额度。所以它不能、也不该去
「试跑」一遍 handle()：那条路上有真开程序、真关机、真点鼠标。这里给的是「解析结果」；
跟状态有关的事（在不在对话模式、有没有待确认的东西）由桥填在报告最上面。

**改这里的顺序要小心**：它必须和 `wechat_bridge.handle()` 的分发顺序一致，否则这份报告
就成了骗人的。`selftest_parse_probe.py` 里有一组用例拿真桥的效果反着验这件事
（尤其「关闭电脑」归电源而不是开关应用这类顺序敏感的）。

这儿的判据只做「原样转述」：解析器返回什么就写什么，不替它下结论、不写「大概」。
"""

import time

import app_actions
import app_launcher
import brightness
import bridge_text
import power
import remind
import screen_control
import volume
import wemeet


def _try(fn, *args):
    """跑一个解析器。它自己抛异常也要看得见——那往往就是最有用的诊断。"""
    try:
        return fn(*args)
    except Exception as exc:                     # noqa: BLE001
        return _Boom(exc)


class _Boom(object):
    """解析器抛异常时的占位：报告里会把这句原文打出来。"""

    def __init__(self, exc):
        self.text = "解析时抛了异常：%s: %s" % (exc.__class__.__name__, exc)


# ---------- 免前缀那条链（顺序照 _dispatch_free）----------

def _row_timers(body, ctx):
    if remind.parse_list(body):
        return "认（看记下的提醒）"
    if remind.parse_cancel(body)[0]:
        return "认（取消提醒）"
    if _try(wemeet.parse_list_timer, body):
        return "认（看定下的会）"
    if _try(wemeet.parse_cancel_timer, body):
        return "认（把定下的会全取消）"
    return None


def _row_remind(body, ctx):
    """定时提醒。和进会一样先过 `remind.parse_remind`（它内部复用 wemeet 的时间解析）。"""
    plan = _try(remind.parse_remind, body)
    if isinstance(plan, _Boom):
        return plan.text
    if not plan:
        return None
    kind = plan.get("kind")
    if kind == "remind":
        return "认（%s 提醒你「%s」）%s" % (plan.get("label"), plan.get("text"),
                                          "，顺延过" if plan.get("slipped") else "")
    if kind == "empty":
        return "认（时间认成 %s，但没说要提醒什么，会回来问你）" % plan.get("label")
    return "认（%s：%s，只回一句说明）" % (kind, plan.get("label"))


def _row_task(body, ctx):
    """定时跑一句话。排在提醒后面：带「提醒我」的归提醒，两边词表不重叠。"""
    plan = _try(remind.parse_task, body)
    if isinstance(plan, _Boom):
        return plan.text
    if not plan:
        return None
    kind = plan.get("kind")
    if kind == "task":
        return ("认（%s 把那句「%s」交给 Claude 跑，会花额度）"
                % (plan.get("label"), plan.get("text")))
    if kind == "empty":
        return "认（时间认成 %s，但没说跑什么，会回来问你）" % plan.get("label")
    return "认（%s：%s，只回一句说明）" % (kind, plan.get("label"))


def _row_confirm(body, ctx):
    got = _try(wemeet.read_confirm, body)
    if isinstance(got, _Boom):
        return got.text
    kind = got[0] if isinstance(got, tuple) else got
    if kind is None:
        return None
    return "认（回上一条问话：%s）" % ("确认" if kind == "yes" else "取消")


def _row_click_reply(body, ctx):
    got = _try(screen_control.read_reply, body)
    if isinstance(got, _Boom):
        return got.text
    kind, idx = got
    if kind is None:
        return None
    # **别用 dict 的默认值**：`.get(kind, "选第 %d 个" % (idx + 1))` 里那个默认值
    # 是先算的，而 kind 是 yes/no 时 idx 是 None —— `/parse 确认` 会当场
    # TypeError: NoneType + int（2026-09-27 加收件那行时顺手撞出来的老 bug）。
    if kind in ("yes", "no"):
        return "认（回那次待确认的点击：%s）" % ("确认" if kind == "yes" else "取消")
    return "认（回那次待确认的点击：选第 %d 个）" % ((idx or 0) + 1)


def _row_chat_switch(body, ctx):
    got = _try(bridge_text.parse_chat_switch, body)
    if isinstance(got, _Boom):
        return got.text
    return {"on": "认（进对话模式）", "off": "认（退出对话模式）"}.get(got)


def _row_look(body, ctx):
    if _try(screen_control.parse_look, body):
        return "认（截当前窗口让 Claude 念一遍）"
    return None


# ---------- 要带 /cc 那条链（顺序照 _dispatch_gated）----------

def _row_meeting(body, ctx):
    """进会：桥是先试「定时」再试「立刻」，这里照同一个顺序。"""
    plan = _try(wemeet.parse_schedule, body, time.time())
    if isinstance(plan, _Boom):
        return plan.text
    if plan:
        kind = plan.get("kind")
        if kind == "schedule":
            return "认（定时 %s 进 %s）" % (plan.get("label"),
                                          wemeet.pretty_code(plan.get("code") or ""))
        if kind == "need_code":
            return "认（记下 %s 这个点，还等你给会议号）" % plan.get("label")
        if kind == "past":
            return "认（但那个点今天已经过去了，只回一句说明、不拉起）"
        if kind == "far":
            return "认（但超过 24 小时了，只回一句说明、不拉起）"
        return "认（定时：%s）" % kind
    code, pwd = _try(wemeet.parse_join, body)
    if code:
        return "认（立刻进 %s%s）" % (wemeet.pretty_code(code),
                                     "，带密码" if pwd else "")
    if _try(wemeet.parse_join_asking, body):
        return "认（在问你要会议号，不拉起）"
    return None


def _row_volume(body, ctx):
    if isinstance(_try(volume.parse_set, body), _Boom):
        return _try(volume.parse_set, body).text
    pct = _try(volume.parse_set, body)
    if pct is not None:
        return "认（把系统音量设到 %d%%）" % pct
    if _try(volume.parse_query, body):
        return "认（报现在多少）"
    return None


def _row_brightness(body, ctx):
    """屏幕亮度。和音量并排：两条词表不重叠，但报告里也得各占一行。"""
    pct = _try(brightness.parse_set, body)
    if pct is not None:
        return "认（把屏幕亮度设到 %d%%）" % pct
    d = _try(brightness.parse_nudge, body)
    if d:
        return "认（屏幕亮度%s一次）" % ("调亮" if d > 0 else "调暗")
    if _try(brightness.parse_query, body):
        return "认（报屏幕亮度现在多少）"
    return None


def _row_power(body, ctx):
    """顺序照 bridge_power：取消关机 → 查状态 → 锁屏 → 关机。"""
    if _try(power.parse_cancel_shutdown, body):
        return "认（取消关机）"
    if _try(power.parse_status, body):
        return "认（查屏幕锁没锁）"
    if _try(power.parse_lock, body):
        return "认（锁屏）"
    sd = _try(power.parse_shutdown, body)
    if isinstance(sd, _Boom):
        return sd.text
    if sd is None:
        return None
    if sd.get("problem"):
        return "认（但%s，只回一句说明，不真关）" % {
            "past": "那个点今天已经过去了", "far": "超过 24 小时了",
            "notime": "没给时间"}.get(sd.get("problem"), sd.get("problem"))
    return "认（关机%s）" % ("：%s" % sd.get("label") if sd.get("label") else "")

def _row_app(body, ctx):
    """开关应用 + 播放键 + 相对音量。

    **开头这几行是照抄 `bridge_media._try_app` 的**（先 split_tail 摘动作，整条就是动作时
    只按键，否则按 head 找应用）。抄它是因为那几行本身就是「判定」，没法当纯函数调用；
    改动 `_try_app` 时记得回来看这里，selftest_parse_probe 有用例盯着。

    `ctx["apps"]` 是桥传进来的「按名字查桌面图标」的回调。有它就能给出**确定结论**
    （查到几个）；没有就只能是「软认」——因为解析通过不等于真会接走：
    桌面上没有那个图标的话，这一条照样落回 Claude。
    """
    head, actions = _try(app_actions.split_tail, body)
    if isinstance(head, _Boom):
        return head.text
    if actions and _try(app_actions.is_lead_only, head):
        return "认（按%s键）" % app_actions.label_of(actions)
    if not actions:
        head = body
    action, query = _try(app_launcher.parse_command, head)
    if not action:
        if actions:
            return ("落回 Claude（摘出了动作「%s」，但前半句「%s」在桌面上找不到应用）"
                    % (app_actions.label_of(actions), head))
        return None
    verb = "关闭" if action == "close" else "打开"
    if not query:
        return "认（只给了个动作词，会回应用清单）"
    lookup = ctx.get("apps")
    if lookup is None:
        return "软认（解析成%s「%s」——桌面上真有没有这个图标还得另看）" % (verb, query)
    hits = lookup(query)
    if not hits:
        return "落回 Claude（解析成%s「%s」，但桌面上没有这个名字的图标）" % (verb, query)
    if len(hits) > 1:
        return "认（%s「%s」，但有 %d 个像的，它会回来问你要哪个）" % (verb, hits[0], len(hits))
    return "认（%s「%s」）" % (verb, hits[0])


def _row_send_file(body, ctx):
    """发文件。`ctx["files"]` 是桥传进来的「这个路径能不能发」，有它才能给确定结论。"""
    target = _try(bridge_text.parse_send_file, body)
    if isinstance(target, _Boom):
        return target.text
    if not target:
        return None
    if not _try(bridge_text.looks_like_path, target):
        return "落回 Claude（抠出了「%s」，但那不像一条路径：要盘符或者 / 开头）" % target
    probe = ctx.get("files")
    if probe is None:
        return "软认（路径对：%s——文件真在不在还得另看）" % target
    exists, why = probe(target)
    if not exists:
        return "落回 Claude（%s 那儿没有文件，它会去找）" % target
    if why:
        return "认（但会回绝：%s）" % why[:40]
    return "认（文件在，这就去传）"


def _row_click(body, ctx):
    got = _try(screen_control.parse_click, body)
    if isinstance(got, _Boom):
        return got.text
    query, double = got
    if not query:
        return None
    return "认（找「%s」%s，找到后先问你要点哪儿）" % (
        query, "双击" if double else "")


def _row_inbox_ask(body, ctx):
    """收件夹满了那句「删掉吗」。**只看词**：真删还得当时确实有待回答的问话。"""
    got = _try(bridge_text.parse_inbox_answer, body)
    if isinstance(got, _Boom):
        return got.text
    return {"yes": "认（回「删掉」：把最老的那几个清掉）",
            "no": "认（回「不删」：这条不收）"}.get(got)

FREE_CHAIN = (
    ("看定下的会 / 取消定时", _row_timers),
    ("回上一条问话", _row_confirm),
    ("回收件的「要腾地方吗」", _row_inbox_ask),
    ("回待确认的点击", _row_click_reply),
    ("对话模式开关", _row_chat_switch),
    ("看看画面", _row_look),
)

GATED_CHAIN = (
    ("进会", _row_meeting),
    ("提醒", _row_remind),
    ("定时跑一句话", _row_task),
    ("音量（调到/多少）", _row_volume),
    ("屏幕亮度（调到/多少）", _row_brightness),
    ("电源（锁屏/关机）", _row_power),
    ("开关应用/播放键", _row_app),
    ("发文件", _row_send_file),
    ("点一下", _row_click),
)


def explain(body, state=None, apps=None, files=None):
    """生成那份报告。

    `state` 是可选的状态行（桥填的真实状态）；`apps` / `files` 是两个只查不动的回调：
      · apps(名字) → 桌面上匹配到的图标名列表（空列表 = 没有）
      · files(路径) → (文件在不在, 不能发的理由或 "")
    桥把它们传进来，报告才能给出**确定结论**（「会接走」还是「落回 Claude」）；
    不传就是「软认」——解析通过、但还差一步。
    """
    if not (body or "").strip():
        return ("🔍 把你要问的那句话写在后面，比如：\n"
                "/parse 打开聊天\n"
                "/parse 关闭电脑\n"
                "/parse 六点四十进会 413231713\n\n"
                "它只把这句话喂给各个解析器、告诉你哪条通道会接走它，"
                "**不动手、不花额度**。")

    ctx = {"apps": apps, "files": files}
    lines = ["🔍 这句话会怎么走（只看判定，不动手、不花额度）"]
    if state:
        lines.append("状态：" + " ｜ ".join(str(s) for s in state))
    lines.append("")

    lines.append("免前缀那条链（不带 /cc 也认）")
    for name, fn in FREE_CHAIN:
        lines.append("· %s：%s" % (name, fn(body, ctx) or "不认"))
    lines.append("")
    lines.append("要带 /cc 的那条链（在对话模式里也免前缀）")
    for name, fn in GATED_CHAIN:
        lines.append("· %s：%s" % (name, fn(body, ctx) or "不认"))
    lines.append("")

    lines.append("每一条链里的第一条「认」就是会接走它的那条（顺序和实际分发一致）。")
    lines.append("标着「软认」的还差一步（桌面上有没有那个图标、文件在不在）——")
    lines.append("这一步是桥在外面查的，查不了才写成软认；真差这一步的话它会落回 Claude。")
    lines.append("「落回 Claude」就是交给模型去跑——花一次额度、也慢一些。")
    lines.append("带 /cc 的斜杠指令（/help、/see、/windows…）没列在这里，那些一看就知道。")
    # 附件不走这条路：它不是「一句话」，没有认不认的问题——收到就落盘。
    # 不写这一句的话，发张图再带 /parse 会让人以为附件那半边没人管。
    lines.append("（你发来的图片和文件不走这里：它们不看句式，收到就下载落盘，"
                 "路径会写在消息里。这一版还没做总配额和删除。）")
    return "\n".join(lines)
