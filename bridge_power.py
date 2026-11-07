# -*- coding: utf-8 -*-
"""关机 / 锁屏 / 睡眠在桥这一层的落点。和音量、开关应用一样不经过 Claude。

真正的判断和动手都在 power.py 里，这儿只做三件事：**按固定顺序问**、把
`(话, 处理了没)` 翻译成「回这句 / 交给 Claude」、以及记一行日志。

顺序是硬的，换了就会出错：

    取消关机 → 查状态 → 睡眠/休眠 → 锁屏 → 关机

「取消关机」和「关机」都含「关机」两个字，「电脑锁了吗」既含「锁」也含「电脑」。
power.py 里每个 parse_* 自己也挡了一遍（parse_lock 先问 parse_status、parse_shutdown
先问 parse_cancel_shutdown），所以这里的顺序算第二道保险——但两道都得在，谁单独失手
都会变成「问一句话结果电脑关了」。睡眠排在锁屏/关机前面只是照「越来越重」排的，
词表本身不重叠。

**这一层会占住轮询线程**，不过这里的动作都是一两秒的量级（最慢是查锁屏状态那句
tasklist、或者查睡眠能力的 `powercfg /a`，各约 0.3 秒），比不上混音器那 1~3 秒的
代价，见 bridge_media.py 开头。

bridge_media.py 那边一行日志都没有（走没走那条通道是 handle 记的），这里破了例：
handle 只知道「走了电源通道」，动作名和关机秒数只有这儿知道，而关机是全项目唯一
不可逆的动作。这两个字段跟消息正文无关，所以不受 log_message_text 管，照记。
"""

import logging

import power

log = logging.getLogger("bridge.power")


class PowerMixin(object):
    """要靠 Bridge 提供 `power`（PowerControl 或 None）。

    None 的意思是功能关掉了或者初始化失败——那时候「关机」这句话照旧交给 Claude，
    它会解释怎么关机，而不是替你关。这是刻意的：功能没开的时候，宁可答非所问，
    也不要让一句话半灵半不灵。
    """

    def _try_power(self, body):
        """回一句话 = 我办了；回 None = 这不是电源指令，交给 Claude。"""
        if self.power is None:
            return None
        for what, hit, act in (
                ("取消关机", power.parse_cancel_shutdown, self.power.cancel_shutdown),
                ("查锁屏状态", power.parse_status, self.power.status),
                ("锁屏", power.parse_lock, self.power.lock)):
            if hit(body):
                text, handled = act()
                if not handled:
                    return None
                log.info("走电源通道：%s", what)
                return text
        kind = power.parse_sleep(body)
        if kind:
            text, handled = self.power.sleep(kind)
            if not handled:
                return None
            # 「睡眠」和「休眠」得分开记：回话里两个字都对，但盘上留下的差别是
            # 「内存还供着电」还是「已经写进 hiberfil.sys」，排查时要用
            log.info("走电源通道：%s", "休眠" if kind == "hibernate" else "睡眠")
            return text
        sd = power.parse_shutdown(body)
        if sd is None:
            return None
        text, handled = self.power.shutdown(sd.get("delay"), sd.get("problem"),
                                            sd.get("label"))
        if not handled:
            return None
        # 关机是这一整套里唯一不可逆的动作，日志里留下「谁让它多久之后关的」。
        # 秒数记原样（None = 用配置里的默认值），排查「怎么突然关机了」时要用。
        # problem 那三种一个字都没往 shutdown.exe 送，日志里也要能分出来。
        log.info("走电源通道：关机，delay=%s label=%s problem=%s",
                 sd.get("delay"), sd.get("label"), sd.get("problem"))
        return text
