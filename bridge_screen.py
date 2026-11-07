# -*- coding: utf-8 -*-
"""看画面与点鼠标：只看当前活动窗口，而且每一次点击都要在微信里再确认一次。

从 wechat_bridge.py 拆出来的（2026-09-02），逻辑一行没改，注释原样搬。

原来这些方法在 wechat_bridge.py 里是被「对话模式」那一段隔成两块的
（_see_target…_plan 在前，_resolve_pending…_run_vision 在后）。搬过来时并成了一块，
块内顺序没动，两块之间那条分界线保留成一行注释。
"""

import logging
import time

import screen_control

log = logging.getLogger("bridge")


class ScreenMixin(object):
    """要靠 Bridge 提供这些：screen（ScreenControl 或 None）、queue、runner、reply，
    以及 _pending——每个用户最多一条待确认的点击，就存在那里。"""

    def _see_target(self, body):
        """「/see 波点音乐」里的窗口名；不是这种句式就返回 None。

        `/see` 后面可以不空格（手机上打中文常常直接连着写），但不能因此把
        `/seek`、`/screenshot` 这种词吃掉——所以要么跟着分隔符，要么跟着中文。
        """
        b = (body or "").strip()
        low = b.lower()
        for cmd in ("/see", "/screen", "/窗口", "看窗口"):
            if not low.startswith(cmd) or len(b) <= len(cmd):
                continue
            nxt = b[len(cmd)]
            if nxt not in " :：,，" and ord(nxt) < 128:
                continue
            rest = b[len(cmd):].strip(" :：,，")
            if rest and len(rest) <= 24 and not any(x in rest for x in "\\/"):
                return rest
        return None

    def _see(self, target=None):
        """当前窗口（或指定窗口）叫什么、上面有哪些能点的东西。只读控件树，不上传画面。

        带名字时会**先把那个窗口切到前面**：点击用的是绝对屏幕坐标，窗口不在最上面
        点下去就落在挡着它的那个窗口上了。人不在电脑前的时候前台窗口往往是桌面，
        所以这条路是常态而不是例外。
        """
        if self.screen is None:
            return "❌ 看画面的功能没启用（config.json 里 screen_control.enable）。"
        if target:
            cands = self.screen.find_window(target)
            if not cands:
                wins = self.screen.windows()
                return ("❌ 没有哪个窗口叫「%s」。现在开着的是：\n%s"
                        % (target, self.screen.describe_windows(wins)))
            if len(cands) > 1:
                return ("⚠️ 有 %d 个窗口像「%s」，说得再准一点：\n%s"
                        % (len(cands), target,
                           self.screen.describe_windows(cands)))
            ok, win = self.screen.activate(cands[0])
            if not ok:
                return ("❌ 我把「%s」往前切了，但系统没让它到最前面（现在最前面是"
                        "「%s」）。你在电脑上点它一下，或者过一会儿再发一次。"
                        % (cands[0].get("title") or "?",
                           (win or {}).get("title") or "读不出来"))
        else:
            win = self.screen.active()
            if not win:
                return "❌ 读不到前台窗口。发「/see 窗口名」我去把那个窗口切到前面。"
            if self.screen.is_desktop(win):
                wins = self.screen.windows()
                return ("❌ 现在最前面是桌面（%s），上面没什么可点的。\n"
                        "开着的窗口有：\n%s\n"
                        "发「/see 波点音乐」这样带上名字，我会把它切到前面再看。"
                        % (win.get("title") or "?",
                           self.screen.describe_windows(wins)))
        info = {}
        text = self.screen.describe(win, self.screen.elements(win["hwnd"], info),
                                   info=info)
        bad = self.screen.refuse_reason(win.get("title"))
        dpi = self.screen.dpi_problem()
        return text + ("\n" + bad if bad else "") + ("\n" + dpi if dpi else "")

    def _try_click(self, msg, uid, body):
        """像「点一下 X」就找控件、报出打算点哪儿、等确认；认不出返回 None 交给 Claude。

        控件树里找不到时才退回截图让 Claude 看（那一步慢、要花额度、还会把当前窗口的
        画面送到模型服务端），所以走的是 worker 线程，不占轮询。
        """
        if self.screen is None:
            return None
        query, double = screen_control.parse_click(body)
        if not query:
            return None
        dpi = self.screen.dpi_problem()
        if dpi:
            return dpi
        win = self.screen.active()
        if not win:
            return "❌ 读不到前台窗口，没法点。发「/see 窗口名」我去把它切到前面。"
        if self.screen.is_desktop(win):
            # 桌面的控件树是空的，去截图也只会截到一张桌面——白花一次额度
            wins = self.screen.windows()
            return ("❌ 现在最前面是桌面，不是应用窗口，所以「%s」无从下手。\n"
                    "开着的窗口有：\n%s\n"
                    "先发「/see 波点音乐」这样带上窗口名（我会把它切到前面），"
                    "再说「点一下%s」。" % (query, self.screen.describe_windows(wins),
                                            query))
        bad = self.screen.refuse_reason(win.get("title"))
        if bad:
            return bad
        if win.get("minimized"):
            return "❌ 「%s」现在是最小化的，先把它切到前面。" % win.get("title")

        info = {}
        cands = self.screen.find(query, self.screen.elements(win["hwnd"], info))
        if cands:
            return self._plan(uid, win, query, cands[:5], double,
                              "wide" if info.get("widened") else "tree")
        why = "控件树里没有「%s」这个东西" % query
        if info.get("error"):
            why = ("读控件树失败了（%s），所以不是「%s」不存在"
                   % (info["error"], query))
        elif info.get("surface_only"):
            why = ("这个窗口只报出一块画布（%s），控件名一个都没有"
                   % info["surface_only"])
        if not getattr(getattr(self, "runner", None), "supports_vision", True):
            return "❌ 当前模型不支持图片理解；可发 /see 读取控件树，或切换 deepseek-flash。"
        if not self.screen.vision_fallback:
            return "❌ " + why + "。发 /see 看看它到底叫什么名字。"
        full = self._enqueue((msg, body, {"query": query, "double": double, "uid": uid}),
                             msg)
        if full is not None:
            return full
        return ("⏳ " + why + "，我截个图让当前模型看一眼再告诉你点哪儿"
                "（这一步慢一些，也会花掉一次额度）。")

    def _plan(self, uid, win, query, cands, double, source):
        """把「打算点哪儿」存下来并说给用户听。真正的点击只在确认之后发生。"""
        secs = self.screen.confirm_seconds
        self._pending[uid] = {"cands": cands, "win": win, "query": query,
                              "double": double, "source": source,
                              "at": time.time()}
        guess = {"vision": "\n坐标是看图猜的，可能偏几十像素。",
                 "wide": "\n这是从自绘控件里翻出来的，不一定真是个按钮。"}.get(source, "")
        how = "双击" if double else "点"
        if len(cands) == 1:
            c = cands[0]
            return ("👆 打算%s「%s」（%s · %s · 屏幕 %d,%d）。%s\n"
                    "回「确认」我就点，回「取消」作罢；%d 秒不回就自动作废。"
                    % (how, c["name"], win.get("title") or "?", c.get("type", "控件"),
                       c["x"], c["y"], guess, secs))
        lines = ["⚠️ 有 %d 个像「%s」的：" % (len(cands), query)]
        for i, c in enumerate(cands, 1):
            lines.append("%d %s（%s）" % (i, c["name"], c.get("type", "控件")))
        lines.append("回数字我就%s那个（回数字就等于确认），回「取消」作罢；"
                     "%d 秒不回自动作废。" % (how, secs))
        return "\n".join(lines)

    # ---------- 确认之后才真的点 ----------

    def _resolve_pending(self, uid, body):
        """待确认状态下收到一条消息。返回要回的话；返回 None 表示这条跟点击无关。"""
        p = self._pending.get(uid)
        if not p:
            return None
        kind, idx = screen_control.read_reply(body)
        if kind is None:
            # 说的是别的事：那次点击就作废，免得过一会儿一句「好」把它点了
            self._pending.pop(uid, None)
            log.info("待确认的点击作废（用户说了别的）user=%s", uid)
            return None
        secs = self.screen.confirm_seconds
        if secs and time.time() - p["at"] > secs:
            self._pending.pop(uid, None)
            return "🔕 刚才那个点击超过 %d 秒没确认，已经作废了。要点就再说一次。" % secs
        if kind == "no":
            self._pending.pop(uid, None)
            return "✅ 好，不点了。"
        cands = p["cands"]
        if kind == "pick":
            if not (0 <= idx < len(cands)):
                return "⚠️ 只有 %d 个候选，回 1 到 %d 之间的数字。" % (len(cands), len(cands))
            return self._do_click(uid, p, cands[idx])
        if len(cands) > 1:
            return "⚠️ 有 %d 个像的，回数字告诉我点哪个。" % len(cands)
        return self._do_click(uid, p, cands[0])

    def _do_click(self, uid, p, cand):
        """确认之后真去点。点之前重新核对前台窗口——存的是绝对屏幕坐标。"""
        self._pending.pop(uid, None)
        old = p["win"]
        cur = self.screen.active()
        if not cur:
            return "❌ 现在读不到前台窗口了，没点。"
        if cur.get("hwnd") != old.get("hwnd"):
            return ("❌ 前台窗口已经不是刚才那个了（现在是「%s」），坐标就不准了，我没点。"
                    "把「%s」切到前面再说一次。"
                    % (cur.get("title") or "?", old.get("title") or "?"))
        moved = any(cur.get(k) != old.get(k)
                    for k in ("left", "top", "width", "height"))
        if moved:
            return ("❌ 「%s」被挪过位置或者改过大小了，刚才那个坐标已经不对，我没点。"
                    "再说一次「点一下%s」，我重新找。"
                    % (old.get("title") or "?", p.get("query") or ""))
        bad = self.screen.refuse_reason(cur.get("title"))
        if bad:
            return bad
        dpi = self.screen.dpi_problem()
        if dpi:
            return dpi
        if not self.screen.in_window(cur, cand["x"], cand["y"]):
            return "❌ 算出来的坐标落在窗口外面，我没点。发 /see 看看那个东西叫什么。"
        try:
            self.screen.click(cand["x"], cand["y"], p.get("double"))
        except Exception as exc:
            log.exception("点击失败")
            return "❌ 点的时候出错了：%s" % exc
        return ("✅ %s了「%s」（%d,%d）。要看结果就发 /see。"
                % ("双击" if p.get("double") else "点", cand["name"],
                   cand["x"], cand["y"]))

    def _ask_look(self, msg, uid):
        """「看看画面」：截当前窗口，让 Claude 说说上面有什么。真正干活在 worker 线程。

        为什么需要这条路：控件树对 Flutter 和自绘应用是不通的（真机上波点音乐整棵树
        只有一个 FLUTTERVIEW 节点），你连「点一下什么」都说不出来。所以先让它念一遍
        画面上有什么，你再照着说。**这里的检查都在轮询线程上做**，是为了让「桌面在最
        前面」「窗口最小化」这类情况 1 秒就得到回答，而不是白花一次额度才发现。
        """
        if self.screen is None:
            return "❌ 看画面的功能没启用（config.json 里 screen_control.enable）。"
        if not getattr(getattr(self, "runner", None), "supports_vision", True):
            return "❌ 当前模型不支持图片理解；可发 /see 读取控件树，或切换 deepseek-flash。"
        if not self.screen.vision_fallback:
            return ("❌ 看图这条路是关着的（config.json 里 screen_control.vision_fallback），"
                    "所以我只能读控件树。发 /see 看看它读到了什么。")
        win = self.screen.active()
        if not win:
            return "❌ 读不到前台窗口，没法截图。发「/see 窗口名」我去把它切到前面。"
        if self.screen.is_desktop(win):
            return ("❌ 现在最前面是桌面，截它没意义（也白花一次额度）。\n"
                    "开着的窗口有：\n%s\n"
                    "先发「/see 波点音乐」这样带上名字，我会把它切到前面。"
                    % self.screen.describe_windows(self.screen.windows()))
        bad = self.screen.refuse_reason(win.get("title"))
        if bad:
            return bad
        if win.get("minimized"):
            return "❌ 「%s」现在是最小化的，截不到。" % (win.get("title") or "?")
        full = self._enqueue((msg, "", {"look": True, "uid": uid}), msg)
        if full is not None:
            return full
        return ("⏳ 好，我截一张「%s」的画面让当前模型看一眼，再告诉你上面有什么"
                "（这一步慢些、会花掉一次额度，画面也会送到模型服务端）。"
                % (win.get("title") or "?"))

    def _shoot_now(self, msg, what):
        """两个「看图」入口共用的五道守卫 + 截图。已经回过话就返回 None。

        为什么要抽出来：`_run_look` 和 `_run_vision` 原来各抄了一份一模一样的守卫，而
        改文案时改一份漏一份是常态——2026-09 那句「读不到前台窗口，没法截图。」两处各有
        一份，只改了一处，是自测报语法错才发现的。文案只留一份，就不会再漏。

        `what` 是「桌面在最前面」那句里要看的对象（「要看的窗口」/「要操作的窗口」）——
        两个入口只有这个词不一样，其余五道守卫完全相同。
        """
        win = self.screen.active()
        if not win:
            self.reply(msg, "❌ 读不到前台窗口，没法截图。", outcome='failed')
            return None
        if self.screen.is_desktop(win):
            self.reply(msg, "❌ 现在最前面是桌面，截它没意义（也白花一次额度）。\n"
                            "先发「/see 窗口名」把%s切到前面。" % what, outcome='failed')
            return None
        bad = self.screen.refuse_reason(win.get("title"))
        if bad:
            self.reply(msg, bad, outcome='failed')
            return None
        if win.get("minimized"):
            self.reply(msg, "❌ 那个窗口现在是最小化的，截不到。", outcome='failed')
            return None
        try:
            path, iw, ih = self.screen.shoot(win)
        except Exception as exc:
            log.exception("截图失败")
            self.reply(msg, "❌ 截图失败：%s" % exc, outcome='failed')
            return None
        return win, path, iw, ih

    def _run_look(self, msg, job):
        """「看看画面」的实际执行：截图 → 让 CLI 念一遍画面上有什么。**不点任何东西。**"""
        uid = job["uid"]
        shot = self._shoot_now(msg, "要看的窗口")
        if shot is None:
            return None
        win, path, iw, ih = shot
        prompt = screen_control.look_prompt(path, win.get("title") or "?", iw, ih)
        key = "screen:" + uid          # 单独一段会话，别把画面描述混进正常上下文
        try:
            out = self.runner.run(key, prompt)
        finally:
            self.runner.reset_session(key)
        from runner_result import result_failed
        if result_failed(out):
            return self.reply(msg, out, outcome='failed')
        text = (out or "").strip()
        if not text:
            return self.reply(msg, "❌ 截到图了，但 Claude 那边没回话。再说一次「看看画面」。", outcome='failed')
        self.reply(msg, ("「%s」现在的画面：\n%s\n\n"
                         "想点哪个就说「点一下<上面的名字>」——我会再看一次图算坐标"
                         "（又是一次额度），点之前照旧要你回「确认」。"
                         % (win.get("title") or "?", text[:1200])))

    def _run_vision(self, msg, job):
        """控件树没找到时的退路：截当前窗口 → 让 CLI 读图给坐标 → 照旧等确认。"""
        uid, query = job["uid"], job["query"]
        shot = self._shoot_now(msg, "要操作的窗口")
        if shot is None:
            return None
        win, path, iw, ih = shot
        prompt = screen_control.vision_prompt(path, win.get("title") or "?",
                                              iw, ih, query)
        key = "screen:" + uid          # 单独一段会话，别把这些坐标问答混进正常上下文
        try:
            out = self.runner.run(key, prompt)
        finally:
            self.runner.reset_session(key)
        from runner_result import result_failed
        if result_failed(out):
            return self.reply(msg, out, outcome='failed')
        hit = screen_control.parse_vision(out)
        if not hit:
            return self.reply(msg, "❌ 看了画面也没找到「%s」。发 /see 看看控件都叫什么，"
                                   "或者换个更准的说法。" % query, outcome='failed')
        x, y = self.screen.to_screen(win, iw, ih, hit["x"], hit["y"])
        cand = {"name": hit.get("what") or query, "type": "看图认出来的", "x": x, "y": y}
        self.reply(msg, self._plan(uid, win, query, [cand], job.get("double"),
                                   "vision"))
