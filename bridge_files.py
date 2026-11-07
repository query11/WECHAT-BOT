# -*- coding: utf-8 -*-
"""把本机的文件发回微信：能不能发的判断，加上真正的上传。

从 wechat_bridge.py 拆出来的（2026-09-02），逻辑一行没改，注释原样搬。

allowed_roots / file_send_reason 是纯函数，跟着一起搬过来了——全项目只有这里用它们。
wechat_bridge.py 再把这两个名字 re-export 出去，所以 selftest.py 里
`B.file_send_reason(...)`、`B.allowed_roots(...)` 照旧管用。

file_send_reason 的 `here=HERE` 是在 def 的时候就绑好的，拆分前后都一样（HERE 本来
就是从 bridge_config 导进来的）；selftest 要换那个目录时是显式传 here= 的，不靠改
模块变量，所以这次搬家动不到它。
"""

import logging
import os
import time

from bridge_config import HERE
from bridge_text import (_SEND_DENY_EXT, _SEND_DENY_HERE, _SEND_DENY_NAMES,
                         _norm_path, human_size, looks_like_path,
                         parse_send_file)
from ilink_client import ILinkError

log = logging.getLogger("bridge")


class ArtifactTransferError(OSError):
    pass


def allowed_roots(cfg, workdir):
    """能发哪些目录下的文件：workdir，加上 claude_extra_args 里 --add-dir 给的那些。

    口径故意和 Claude 自己的可读范围一致——它能读的才可能被要求发出去。
    注意 --add-dir 可以跟多个值，所以一直吃到下一个以 - 开头的参数为止。
    """
    roots = [workdir]
    args = [str(a) for a in (cfg.get("claude_extra_args") or [])]
    i = 0
    while i < len(args):
        if args[i] == "--add-dir":
            i += 1
            while i < len(args) and not args[i].startswith("-"):
                roots.append(args[i])
                i += 1
            continue
        i += 1
    out = []
    for r in roots:
        r = (r or "").strip()
        if r and os.path.isdir(os.path.expanduser(r)):
            out.append(os.path.expanduser(r))
    return out


def file_send_reason(path, roots, max_mb, here=HERE):
    """能不能把这个文件发到微信。返回拒绝的理由；能发就返回 ""。"""
    if os.path.isdir(path):
        return ("❌ 「%s」是个文件夹，我只能发单个文件。要整个目录的话先打包成 zip。"
                % os.path.basename(path.rstrip("\\/")))
    if not os.path.isfile(path):
        return "❌ 找不到这个文件：%s" % path
    name = os.path.basename(path).lower()
    if name in _SEND_DENY_NAMES or name.endswith(_SEND_DENY_EXT):
        return ("❌ 「%s」看着像密钥或凭据，这类文件我不发。真要发就自己复制一份改个名。"
                % os.path.basename(path))
    if _norm_path(os.path.dirname(path)) == _norm_path(here) and name in _SEND_DENY_HERE:
        return ("❌ 「%s」是这个桥自己的凭据文件（里面有 bot_token 或会话 ID），发出去"
                "等于把这个机器人交给别人，我不发。" % os.path.basename(path))
    ok = False
    for r in roots:
        rn, pn = _norm_path(r), _norm_path(path)
        if pn == rn or pn.startswith(rn.rstrip("\\/") + os.sep):
            ok = True
            break
    if not ok:
        return ("❌ 这个文件不在我能碰的范围里（%s）。先把它复制到 %s 下面。"
                % ("、".join(roots) or "（没有）", roots[0] if roots else "工作目录"))
    size = os.path.getsize(path)
    if size <= 0:
        return "❌ 「%s」是个空文件（0 字节），发过去也打不开。" % os.path.basename(path)
    if max_mb and size > max_mb * 1024 * 1024:
        return ("❌ 「%s」有 %s，超过了 %s MB 的上限（config.json 里 send_file.max_mb）。"
                % (os.path.basename(path), human_size(size), max_mb))
    return ""


class SendFileMixin(object):
    """要靠 Bridge 提供这些：queue、client.send_file、reply，以及配置来的
    send_file_enable、send_file_max_mb、send_file_limit、send_roots。"""

    def _try_send_file(self, msg, uid, body):
        """「发给我 D:\\x\\a.pptx」就自己传；说得含糊或路径不存在则返回 None 交给 Claude。

        为什么路径不存在也交给 Claude：手机上打不全路径是常态，Claude 能去找，
        找到了它会在回复里写「[[发文件]]<路径>」，那一行照样由这里真正发出去。
        检查都在轮询线程上做（都是 os.stat 级别的开销），加密和上传才进 worker。
        """
        target = parse_send_file(body)
        if not target or not looks_like_path(target):
            return None
        path = os.path.abspath(os.path.expanduser(target))
        if not os.path.exists(path):
            return None                   # 交给 Claude 去找，别急着报错
        if not self.send_file_enable:
            return ("❌ 发文件的功能是关着的（config.json 里 send_file.enable）。"
                    "现在我只能告诉你路径：%s" % path)
        why = file_send_reason(path, self.send_roots, self.send_file_max_mb)
        if why:
            return why
        size = os.path.getsize(path)
        full = self._enqueue((msg, "", {"sendfile": [path], "uid": uid}), msg)
        if full is not None:
            return full
        return ("📎 正在传「%s」（%s）。上传后会报告发送结果；"
                "大文件要等一会儿。" % (os.path.basename(path), human_size(size)))

    def _upload_task_file(self, msg, path, options, artifact=None):
        if getattr(msg, '_task_output_dir', None):
            from task_artifacts import _within
            if not _within(path, msg._task_output_dir):
                raise ArtifactTransferError('本次任务只交付指定结果目录中的新文件。')
        find_input = getattr(self, '_batch_send_input', None)
        batch_input = find_input(msg, path) if callable(find_input) else None
        if batch_input is not None:
            from task_artifacts import verified_artifact_snapshot
            with verified_artifact_snapshot(batch_input, staging_dir=os.path.join(HERE, '.runtime', 'artifact-transfers'),
                                            staging_roots=[HERE],
                                            registry_dir=os.path.join(HERE, '.runtime', 'snapshot-lifecycle'),
                                            task_id=getattr(msg, '_journal_id', None) or getattr(msg, '_queue_id', None),
                                            **self._batch_options()) as (checked, snapshot):
                if snapshot is None:
                    raise ArtifactTransferError(checked.get('reason', '批次输入副本已变化'))
                return self.client.send_file(msg.from_user_id, snapshot, file_name=batch_input['name'], **options)
        if artifact is None:
            return self.client.send_file(msg.from_user_id, path, **options)
        with self._verified_artifact_snapshot(artifact, task_id=getattr(msg, '_journal_id', None)
                                               or getattr(msg, '_queue_id', None)) as (checked, snapshot):
            if snapshot is None:
                raise ArtifactTransferError(checked.get('reason', '产物检查未通过'))
            # Upload the bytes we checked, even if the original changes later.
            return self.client.send_file(msg.from_user_id, snapshot,
                                         file_name=checked['name'], **options)

    def _send_files(self, msg, paths, *, artifact_records=None):
        """真正的上传。跑在 worker 线程上：加密几 MB 要一两秒，网络更慢。"""
        accepted = failed = unconfirmed = 0
        previous = getattr(msg, '_receipt_outcome', 'completed')
        execution_previous = getattr(msg, '_execution_state', 'completed')
        control = getattr(msg, "_task_control", None)
        omitted = paths[self.send_file_limit:]
        for index, path in enumerate(paths[:self.send_file_limit], 1):
            if control is not None:
                control.check()
            self._set_stage(msg, "正在加密并上传第 %d 个文件" % index, notify=True, phase='send')
            find_input = getattr(self, '_batch_send_input', None)
            batch_input = find_input(msg, path) if callable(find_input) else None
            source_path = batch_input['original_path'] if batch_input else path
            validation_path = batch_input['path'] if batch_input else path
            name = os.path.basename(source_path)
            try:
                why = file_send_reason(validation_path, self.send_roots, self.send_file_max_mb)
            except OSError:
                why = '❌ 无法读取文件信息：%s。文件可能已移动或不可访问。' % path
            if why:
                failed += 1
                log.info("拒发文件 %s：%s", name, why)
                self.reply(msg, why)
                if getattr(msg, '_receipt_outcome', None) == 'uncertain':
                    previous = 'uncertain'
                continue
            started = time.time()
            try:
                options = {"context_token": msg.context_token}
                if control is not None and getattr(self.client, "supports_transfer_cancellation", False) is True:
                    options["control"] = control
                artifact = artifact_records[index - 1] if artifact_records is not None else None
                got, size = self._upload_task_file(msg, path, options, artifact)
            except ILinkError as exc:
                unconfirmed += 1
                msg._receipt_outcome = "uncertain"
                previous = "uncertain"
                log.error("发文件失败 %s：%s", name, self._mask(str(exc)))
                self.reply(msg, "⚠️ 「%s」未获得发送成功确认。\n请先检查微信附件，避免重复发送；详细原因见日志。" % name)
            except ArtifactTransferError as exc:
                failed += 1
                self.reply(msg, '⚠️ 「%s」没有上传：%s。请检查原文件，不会静默发送已改变的版本。' % (name, exc))
            except OSError as exc:
                failed += 1
                log.error("读文件失败 %s：%s", name, exc)
                self.reply(msg, "❌ 「%s」读不出来：%s" % (name, exc))
            else:
                accepted += 1
                if not hasattr(msg,"_output_files"):msg._output_files=[]
                msg._output_files.append(self._mask(source_path))
                log.info("发文件成功 %s（%d 字节，用时 %.1f 秒）", got, size,
                         time.time() - started)
                self.reply(msg, '📎 微信接口已接受「%s」（%s）。\n本机来源：%s\n请在微信附件中下载查看。'
                           % (got, human_size(size), source_path))
            if getattr(msg, '_receipt_outcome', None) == 'uncertain':
                previous = 'uncertain'
        msg._receipt_outcome = ('uncertain' if unconfirmed or previous == 'uncertain'
                                or getattr(msg, '_receipt_outcome', None) == 'uncertain'
                                else 'failed' if failed or omitted else previous)
        msg._execution_state = ('uncertain' if unconfirmed or execution_previous == 'uncertain'
                                else 'failed' if failed or omitted else execution_previous)
        if omitted:
            summary = ('本次请求 %d 个文件：微信接口接受 %d 个，失败 %d 个，未确认 %d 个。\n'
                       '受每次 %d 个的上限限制，另有 %d 个未发送：\n%s\n'
                       '请先检查已收到的附件，再单独请求未发送的文件。') % (
                           len(paths), accepted, failed, unconfirmed, self.send_file_limit,
                           len(omitted), '\n'.join(self._mask(str(p)) for p in omitted))
            msg._result_summary = self._mask(summary)[:1000]
            self.reply(msg, summary)
        return {'accepted': accepted, 'failed': failed, 'unconfirmed': unconfirmed,
                'requested': len(paths), 'not_sent': len(omitted)}

    def _run_sendfile(self, msg, job):
        self._send_files(msg, job.get("sendfile") or [], artifact_records=job.get('artifacts'))
