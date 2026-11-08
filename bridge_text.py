# -*- coding: utf-8 -*-
"""桥的纯文本解析层：把用户发来的一句话解析成意图。

从 wechat_bridge.py 拆出来的（2026-09-02）。这里全是纯函数——不读配置、不碰
文件系统、不依赖 HERE，所以能单独看、单独测。拆的时候一行逻辑都没改，注释原样搬。

wechat_bridge.py 仍然把这里的名字都 re-export 出去，所以 wechat_bridge.parse_chat_switch
这种老写法照旧能用，selftest.py 不用改。
"""

import os

HELP = """可用指令：

后台待机（启用 standby_on_start 时）
开始对话 / 结束对话    唤醒并聊天 / 退出并待机（启动助手、停止助手也认）
/status               查看在线状态；待机不接收附件，已设提醒仍会通知

💬 说话
开始对话 / 结束对话    直接开始聊天 / 结束聊天（/on、/off 也认）
/cc <内容>            让我干活（不在对话模式时必须带这个前缀，防手滑）
/tasks                查看最近任务与重启后结果不确定的记录
/resume <任务编号>    手动恢复未开始的文字或批次任务（保留原清单，先开始对话）
/result <任务编号>    查看保存的完整文字（保留 7 天）
/resend <任务编号>    只补发未获确认的文字部分，最后一段可能重复
/resolve <编号> checked|abandoned|archived    已核对 / 放弃恢复 / 归档
现在怎么样 / 还在处理吗 / /brief    简短查看当前任务、排队和最近记录；待机也能查
/new                  清掉上下文，重新开一段
/where   /id          看工作目录 / 看你自己的用户 ID
/version              当前版本，以及有没有新版本
/parse <一句话>       这句话会走哪条通道（不动手、不花额度；排查「为什么它没反应」用）

🎵 电脑
/cc 打开微信          开桌面上的应用，「关闭微信」也认；不走我，1 秒就回
/cc 下一首            播放、暂停、上一首、下一首、停止播放
/cc 大声点            音量：小声点、静音、取消静音（一次约 10%）
/cc 音量调到30%       设到某个值；「音量多少」问现在多少
/cc 亮度调到50%        屏幕亮度：「亮度多少」问现在多少、「屏幕亮一点」调一格
/cc 锁屏   /cc 关机   锁屏；关机默认留 1 分钟，说「取消关机」能拦住
/cc 睡眠   /cc 休眠   进睡眠 / 写进硬盘再断电；唤醒按一下电源键（手机叫不醒它）

📅 开会
/cc 进会 413231713    拉起腾讯会议直接入会（「413-231-713」也认）
/cc 进会 413231713 密码 8888     有入会密码就这么带上
/cc 六点四十进会 413231713       定时：到点我先问一句，回「确认」才进
/cc 明天九点提醒我交房租          定时提醒：到点直接发微信；「看提醒」看清单、「取消提醒」删掉
/cc 每天早上九点提醒我吃药        「每天」= 重复：到点发一条，然后自动排到第二天
/cc 明天九点帮我跑：把那个表处理一下   定时任务：到点把那句话交给 Claude 真去执行（花额度）
/cc + 整段腾讯邀请文案           里面有「#腾讯会议：<号>」就直接进会
/meetings             看有哪些定下的会；说「取消定时」全部取消

🖥 屏幕
/see                  看当前窗口叫什么、上面有哪些能点的东西（不上传画面）
/see 波点音乐         先把这个窗口切到前面再看
/windows              看现在开着哪些窗口
看看画面              截一张当前窗口的图，让我念一遍上面有什么（花额度、画面会送出去）
/cc 点一下 X          找到那个控件 → 先告诉你要点哪儿 → 你回「确认」才点

📁 文件
/cc 发给我 D:\\报告\\汇报.pptx    把本机的文件发到微信
发给我桌面上那个汇报的 ppt        说不清路径就这么说，我去找
/artifacts <编号或 latest>      查看任务产物清单
/getfile <编号> <序号或 all>    按清单核对并取回任务产物
/batch                多附件批次帮助：先列清单，确认后按固定输入处理
/template             常用任务模板：保存输入、步骤、输出目录和格式，按固定版本运行
/apps                 看桌面上哪些应用我能开

🧰 管桥自己（都不用带 /cc）
/status               看桥运行了多久、正在跑什么、后面排了几条、定下的会和提醒
/log [行数]           把 bridge.log 最后几行发过来（默认 30 行，发之前先抹掉凭据）
/queue                看排着哪些活儿；「/queue clear」清掉排着的
/queue cancel|edit|move <编号> [正文或位置]    管理自己的单条待执行任务
/reload               改完 config.json 不用重启，就地重读（写坏了就不换，旧的照跑）
/stop                 叫停正在跑的那条；/stopall 连排着的一起清

语音消息也认（微信自带转写）。收件功能开启时，图片和文件会保存到收件目录；需要分析时再告诉我。
我这边的文件不会自动回传，但你说「发给我 <路径>」我就把它发过来。"""

# 进对话模式时那条回话里带的「常用功能」。刻意做成一份**照着说就能用**的清单，
# 而不是把 /help 整份搬过来：刚进对话模式的人这会儿最需要知道「现在能直接说什么」，
# 不是读 36 行说明书。完整清单仍然在 /help（那份是兜底，这份是日常）。
#
# 两条硬约束，都是吃过教训留下来的：
#   · **不许出现「分钟」二字**。chat_mode.idle_minutes 填 0 时那条回话一个字都不该提
#     超时，而自测拿「有没有『分钟』」当判据（test_chat_mode_expiry 那条「关掉自动退出时
#     不提超时」）。所以这里举例子用「六点四十进会」，不用「二十分钟后」。
#   · **不列「关机」**。这条通道在对话模式下免前缀就生效，而关机是不可逆的——
#     一份写着「随便试」的清单里不该出现它。要关就自己说，或者看 /help。
#     （锁屏、睡眠留着：锁了/睡了也只是要你回电脑前动一下。）
CHAT_ON_TIPS = (
    "💬 直接说话：看一下 D 盘有哪些文件夹\n"
    "🎵 电脑：打开微信 · 下一首 · 大声点 · 音量调到30% · 亮度调到50% · 锁屏 · 睡眠\n"
    "📅 开会/提醒：进会 413231713 · 六点四十进会 413231713（到点先问你）\n"
    "             明天九点提醒我交房租（到点直接发）· 每天早上九点提醒我吃药（每天都发）\n"
    "             明天九点帮我跑：把那个表处理一下（到点真去执行，花额度）\n"
    "🖥 屏幕：/see 看窗口能点什么 · /windows 开着哪些 · 看看画面（截图让它念）\n"
    "👆 点按钮：点一下开始播放（先问你要点哪儿，回「确认」才点）\n"
    "📁 文件：发给我 D:\\报告\\汇报.pptx\n"
    "📋 其它：/new 清上下文 · /apps 有哪些应用 · /meetings 定下的会 · /help 全部")

# ---------- 对话模式（免 /cc 前缀）----------
#
# 「每句话都要打 /cc」在手机上太烦，所以给一对开关：开了之后这个用户的消息直接
# 进 Claude，不用前缀。刻意做成三条克制的规则，免得正常说话被误当成开关：
#   ① 整条消息就是这个短语（前面顶多有「帮我」这类客套）；
#   ② 全长不超过 _CHAT_MAX_CHARS 字；
#   ③ 只收「动词 + 对话/聊天/会话(模式)」这种带宾语的说法，不收光秃秃的「开始」「停止」
#      ——单独发这两个词更像在说「接着刚才那件事」，和 app_actions 里不收「继续」同理。
# 认不出来就当普通消息，照旧走原来的规则。
#
# 「会话」是 2026-08-30 真机上补的：当时只收了「对话/聊天」两个词根，一句
# 「开始会话」就被当成普通消息静默丢掉，看着和桥挂了一样。这三个词在中文里是一回事，
# 手滑打成哪个都得认。全等匹配，所以加词不会让「会话框的样式改一下」这种句子被误吞。
#
# 2026-09 把**枚举**换成了**组合**（动词表 × 名词表）。枚举天生配不齐：实测
# 「进入聊天模式」「打开对话」这一批 12 种说法全被静默丢掉——「对话模式/会话模式」
# 都收了，唯独漏了「聊天模式」，而丢掉是无声的，用户只会以为桥挂了。组合说的是同一件事，
# 而且以后再加名词（比如「私聊」）只用动一处，不必把动词再抄一遍。
# **安全绳一个字没动**：两头客套照剥、长度照限、剥完必须跟「动词+名词」全等。
_CHAT_ON_VERBS = ("开始", "开启", "进入", "打开")
_CHAT_OFF_VERBS = ("结束", "停止", "退出", "关闭")
# 名词必须点到「开关的是哪个东西」。刻意不收光秃秃的「模式」——「关闭模式」「开启模式」
# 听着更像在说别的功能，收了会误吞。
_CHAT_NOUNS = ("对话", "聊天", "会话", "对话模式", "聊天模式", "会话模式")
# 没有「动词+名词」结构的那些写法，照旧一条条列
_CHAT_ON_EXTRA = ("持续对话", "持续会话", "免前缀", "免前缀模式",
                  "/on", "/chat", "/开始对话", "/开始会话")
_CHAT_OFF_EXTRA = ("/off", "/end", "/结束对话", "/结束会话")
_CHAT_LEAD = ("帮我", "麻烦", "请", "现在", "那就", "那", "就", "先", "你")
# 尾巴上的客套。剥这些看着危险，其实不危险：剥完还要跟词表**全等**才算命中，
# 所以「结束对话框的样式」这种句子剥不出词表里的东西来。刻意不收「吗」「没」
# ——「开始对话吗」是在问，不是在下指令。
_CHAT_TAIL = ("谢谢", "谢了", "多谢", "吧", "了", "啊", "呀", "哈", "呗")
_CHAT_MAX_CHARS = 20

def _chat_norm(text):
    """归一化到能做全等比较：去空白、去句末标点、小写。斜杠要留着（/on）。"""
    s = (text or "").strip().lower()
    for ch in " \t　.。!！?？,，、;；~～":
        s = s.replace(ch, "")
    return s


def _chat_verb_noun(s, verbs):
    """整条消息是不是「动词+名词」。客套已经在外面剥掉了，这里必须**全等**。

    `s[len(v):] in _CHAT_NOUNS` 是个集合全等，所以动词和名词后面都不能再多一个字：
    「结束对话框的样式改一下」剩下「框的样式改一下」→ 不中；
    「打开微信」剩下「微信」→ 不中（那一句照旧归开应用那条通道）。
    名词部分为空也不行——光一个「开始」是刻意不认的。
    """
    for v in verbs:
        if s.startswith(v) and s[len(v):] in _CHAT_NOUNS:
            return True
    return False


def parse_chat_switch(text):
    """整条消息是不是对话模式的开关。返回 'on' / 'off' / None。

    判据是「动词+名词」的组合（见上面那一大段），不再是一条条枚举短语。两头客套照旧
    各剥三层，**剥完必须跟组合全等**——这一条是防误吞的命根，别放松成子串匹配或者
    模糊匹配，那会让「把开始对话这四个字写进 README」也被吞掉。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > _CHAT_MAX_CHARS:
        return None
    s = _chat_norm(raw)
    if not s:
        return None
    # 剥掉两头的客套，各最多三层（「那你先结束对话，谢谢」这种）
    for _ in range(3):
        for lead in _CHAT_LEAD:
            if s.startswith(lead) and len(s) > len(lead):
                s = s[len(lead):]
                break
        else:
            break
    for _ in range(3):
        for tail in _CHAT_TAIL:
            if s.endswith(tail) and len(s) > len(tail):
                s = s[:-len(tail)]
                break
        else:
            break
    if s in _CHAT_ON_EXTRA or _chat_verb_noun(s, _CHAT_ON_VERBS):
        return "on"
    if s in _CHAT_OFF_EXTRA or _chat_verb_noun(s, _CHAT_OFF_VERBS):
        return "off"
    return None

# ---------- 把本机的文件发回微信 ----------
#
# 两条路：① 消息里直接给了路径，桥自己传，1 秒回、不花额度；② 说得含糊
# （「桌面那个汇报的 ppt」），那就交给 Claude 去找，它在回复里留一行
# 「[[发文件]]<绝对路径>」，桥把这行摘掉再把文件传上去。
# 第 ② 条是必要的：用户在手机上根本打不出完整路径。
MARK_SEND_FILE = "[[发文件]]"
_SEND_LEAD = ("/file", "/发文件", "/send", "发文件", "发送文件", "发给我", "发我",
              "发送给我", "传给我", "传我", "发到微信")
_SEND_TAIL = ("发给我", "发我", "发送给我", "传给我", "传我", "发到微信",
              "发过来", "传过来", "发出来", "传上来")
_SEND_STRIP_LEAD = ("把", "将", "帮我", "请", "麻烦", "给我", "文件", "那个", "这个",
                    "一下", "：", ":", "、", "，", ",")
_SEND_STRIP_TAIL = ("谢谢", "谢了", "多谢", "吧", "啊", "呀", "哈", "呗", "一下",
                    "这个文件", "那个文件", "这个", "那个")
# 路径可以很长，所以这里的长度上限比别的快速通道宽得多
_SEND_MAX_CHARS = 240
# 明确不发的东西：凭据、密钥。真要发就自己复制一份改个名，别让一句口令就能取走
_SEND_DENY_EXT = (".pem", ".key", ".pfx", ".p12", ".ppk", ".jks", ".kdbx",
                  ".keystore")
_SEND_DENY_NAMES = ("task-history-before-details.sqlite","claude-api-sessions.json","deepseek-api.key", "deepseek-sessions.json","tasks.sqlite", "tasks.sqlite-journal", ".env", "id_rsa", "id_ed25519", ".netrc", "_netrc",
                    ".git-credentials", "credentials", "credentials.json",
                    ".npmrc", ".pypirc")
# 桥自己目录里的这几个文件带 bot_token / 会话 ID / 会议密码 / 你要提醒自己的事，发出去
# 等于把这些交出去
_SEND_DENY_HERE = ("app_preferences.json","config.json", "sessions.json", "send_style.txt",
                   "get_updates_buf.txt", "app_targets.json", "meetings.json",
                   "reminders.json", "inbox.json")


# 句末的标点和客套。剥客套这件事在这里也是安全的：剥完还得跟 _SEND_TAIL 里的
# 词**后缀相等**才算命中，光剥掉「谢谢」不会凭空造出一句「发给我」。刻意不收
# 「了」——「文件发出来了」是在陈述，不是在要文件。
_SEND_PUNCT = " 　。.!！?？,，、;；~～"
_SEND_POLITE = ("谢谢", "谢了", "多谢", "吧", "啊", "呀", "哈", "呗")

def _rstrip_polite(s, words=_SEND_POLITE, rounds=3):
    s = s.rstrip(_SEND_PUNCT)
    for _ in range(rounds):
        for w in words:
            if s.endswith(w) and len(s) > len(w):
                s = s[:-len(w)].rstrip(_SEND_PUNCT)
                break
        else:
            break
    return s


def _norm_path(p):
    return os.path.normcase(os.path.abspath(p))


def looks_like_path(s):
    """像不像一个真路径。不像就别拦，交给 Claude 去找那个文件。"""
    s = (s or "").strip()
    if not s or "\n" in s:
        return False
    if not any(sep in s for sep in ("\\", "/")):
        return False                      # 光一个文件名不算：得让 Claude 去找
    return s.startswith(("\\\\", "/", "~")) or (len(s) > 2 and s[1] == ":")

def parse_send_file(text):
    """「发给我 D:\\x\\a.pptx」「把 D:\\x\\a.pptx 发给我」→ 那个路径；认不出返回 None。

    只负责从句子里把路径抠出来，存不存在、能不能发由调用方判断。抠错了也不要紧：
    像不像路径这一关过不去就当没匹配，照旧交给 Claude。
    """
    s = (text or "").strip()
    if not s or len(s) > _SEND_MAX_CHARS or "然后" in s:
        return None
    low = s.lower()
    body = None
    for lead in sorted(_SEND_LEAD, key=len, reverse=True):
        if low.startswith(lead):
            body = s[len(lead):]
            break
    if body is None:
        t = _rstrip_polite(s)             # 「传给我，谢谢」「发过来吧」也要认出来
        for tail in sorted(_SEND_TAIL, key=len, reverse=True):
            if t.endswith(tail):
                body = t[:-len(tail)]
                break
    if body is None:
        return None
    body = body.strip()
    for _ in range(4):                    # 「把那个 D:\x 发给我」这类两头的杂字
        for lead in sorted(_SEND_STRIP_LEAD, key=len, reverse=True):
            if body.startswith(lead) and len(body) > len(lead):
                body = body[len(lead):].strip()
                break
        else:
            break
    body = _rstrip_polite(body)
    for _ in range(3):
        for tail in sorted(_SEND_STRIP_TAIL, key=len, reverse=True):
            if body.endswith(tail) and len(body) > len(tail):
                body = body[:-len(tail)].rstrip(_SEND_PUNCT)
                break
        else:
            break
    return body.strip().strip("「」《》“”\"'`") or None

def strip_file_marks(text, limit=3):
    """摘取文件标记。limit=None 保留全部请求，由发送端执行数量上限。"""
    keep, paths = [], []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith(MARK_SEND_FILE):
            p = s[len(MARK_SEND_FILE):].strip().strip("「」《》“”\"'`").strip()
            if p and (limit is None or len(paths) < limit) and p not in paths:
                paths.append(p)
            continue
        keep.append(line)
    return "\n".join(keep).strip(), paths


def human_size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d 字节" % n


# ---------- 收附件那边「要不要腾地方」的答复 ----------
#
# **「删」这一边从严**：只认「删掉」「删吧」两个说法。这是**不可逆动作**的确认，
# 少认几个说法只是多问一次；多认一个说法就可能删错东西。「确认」刻意**不算**——
# 它已经有两个主人了（进会到点、待确认的点击），再让第三个来抢，总有一天会抢错。
# 「不删」这一边宽松点没关系（不删最坏就是这条没收，没有后果）。
_INBOX_YES = ("删", "删掉")
_INBOX_NO = ("不删", "别删", "不要删")
# 这两个是整句，不参与剥后缀
_INBOX_NO_EXACT = ("算了", "不了")


def parse_inbox_answer(text):
    """收件那边问「要不要删掉最老的腾地方」时，这句是不是答复。

    返回 "yes" / "no" / None（不是答复）。只剥句末语气词（吧/了/呗/啊/呀），
    **不做别的模糊匹配**：「删除」不认（它更像名词/动词混用），
    「把这个文件删掉」也不认（那是让我去干活，不是回答我的话）。

    这一句只有在**确实有待回答的问话**时才会被用上（见 bridge_inbox 的
    `_resolve_inbox_ask`），所以词表窄一点不会误伤——窄了顶多多问一次。
    """
    s = (text or "").strip().strip("。.!！?？,，、;；~～ \t")
    if s in _INBOX_NO_EXACT:
        return "no"
    core = s.rstrip("吧了呗啊呀")
    if core in _INBOX_NO:
        return "no"
    if core in _INBOX_YES:
        return "yes"
    return None
