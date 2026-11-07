# -*- coding: utf-8 -*-
"""开关应用 + 播放控制 + 系统音量 + 屏幕亮度：四条都不经过 Claude，1 秒就回。

从 wechat_bridge.py 拆出来的（2026-09-02），逻辑一行没改，注释原样搬。

这里没有 log：原来这些方法里也没有一行日志——走没走这条通道是在 handle 那边记的
（「走音量通道」「走亮度通道」「走应用快速通道」），拆开之后那几行还留在
wechat_bridge.py 里。
"""

import app_actions
import app_launcher
import brightness
import volume


class MediaMixin(object):
    """要靠 Bridge 提供这些：media（MediaControl 或 None）、brightness
    （BrightnessControl 或 None）、launcher（AppLauncher 或 None）。都可能是 None
    ——功能关掉、或者初始化失败就退回给 Claude。"""

    def _try_volume(self, body):
        """「音量调到 30%」「音量多少」——这两件只有系统混音器做得到，所以单独一条路。

        放在 `_try_app` 前面：这些句子里没有应用名，不会跟开关应用抢。
        相对调整（「大声点」「静音」）**不在这里**，仍然走 `_try_app` 的动作词表——
        那条路在没有混音器时也能用，顺带还能和「打开波点音乐并大声点」拼在一起。

        混音器要起一次 PowerShell 编 C#，大约 1~3 秒，这几秒轮询线程收不到新消息
        （和解析快捷方式同一个代价，见 volume.py 开头）。
        """
        if self.media is None:
            return None
        pct = volume.parse_set(body)
        if pct is not None:
            text, handled = self.media.set_volume(pct)
            return text if handled else None
        if volume.parse_query(body):
            text, handled = self.media.query_volume()
            return text if handled else None
        return None

    def _try_brightness(self, body):
        """「亮度调到 50%」「亮度多少」「屏幕亮一点」——屏幕背光，走 WMI。

        和音量并排放在这里，是因为两条路长得一样（绝对数值 + 相对调整 + 问现在多少），
        而且**词表不重叠**：这边要「亮度 / 屏幕」，那边要「音量 / 声音」，
        「音量调到 30%」不会被这里认走（自测里正反两面都钉着）。

        和音量要紧的差别是**这条路没有按键兜底**：混音器挂了还能按音量键，而亮度没有
        那个标准键码，所以读不到就照实说读不到，不假装调过了。

        代价和音量同量级：起一次 PowerShell（不用编 C#，约 1 秒），这几秒轮询线程收不到
        新消息——别挪进循环里连着调。
        """
        if self.brightness is None:
            return None
        pct = brightness.parse_set(body)
        if pct is not None:
            return self.brightness.set_text(pct)[0]
        direction = brightness.parse_nudge(body)
        if direction:
            return self.brightness.nudge_text(direction)[0]
        if brightness.parse_query(body):
            return self.brightness.query_text()[0]
        return None

    def _try_app(self, body):
        """像「打开波点音乐并播放」「下一首」就自己办了，1 秒回；认不出就返回 None 交给 Claude。

        判定故意做得保守：句式对、名字短、能在桌面上匹配到唯一一个，三条都满足才拦。
        「打开 D:\\xx\\a.md 看看写了什么」这种必须落回 Claude，所以宁可漏拦不误拦。
        动作（播放/下一首之类）同理：只在「整条消息就是个动作」或者「前半句真能匹配到
        一个应用」时才按键。「帮我看看怎么开始播放」的前半句不像应用名，照旧交给 Claude。
        """
        head, actions = body, []
        if self.media is not None:
            head, actions = app_actions.split_tail(body)
        if actions and app_actions.is_lead_only(head):
            text, handled = self.media.run(actions)          # 整条消息就是个播放动作
            return text if handled else None
        if not actions:
            head = body                       # 没摘到动作就按原样解析，老路径完全不变
        if self.launcher is None:
            return None
        action, query = app_launcher.parse_command(head)
        if not action:
            return None
        if not query:
            # 「/open」后面什么都没跟，直接给清单，比交给 Claude 猜有用
            return self.launcher.describe() if head.startswith("/") else None
        if action == "close":
            text, handled = self.launcher.close(query)
            return text if handled else None
        text, handled = self.launcher.open(query)
        if not handled:
            return None
        if actions:
            # 应用刚起来还接不住按键，等几秒；这一等不能占住轮询线程
            self.media.run_later(actions)
            name = app_actions.label_of(actions)
            text += "\n%d 秒后按「%s」键。没反应就再说一句「%s」。" % (
                int(self.media.delay), name, name)
        return text
