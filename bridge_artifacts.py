"""Task-bound output catalogs and explicit retrieval; never re-run a model."""
import logging
import os

from bridge_config import HERE
from bridge_files import file_send_reason
from bridge_text import human_size
from task_artifacts import collect_artifacts, verified_artifact_snapshot
from task_journal import TaskJournal
from bridge_result_checks import run_result_checks, format_result_checks

log = logging.getLogger('bridge')
ACTIVE_STATES = ('queued', 'running', 'waiting', 'recoverable')
ARTIFACT_ALIASES = {
    '查看刚才的产物': '/artifacts latest',
    '把刚才的产物发给我': '/getfile latest all',
    '把刚才生成的文件发给我': '/getfile latest all',
}


class ArtifactMixin:
    def _artifact_options(self):
        protected = [os.path.join(HERE, '.runtime'),
                     os.path.join(HERE, 'BOT_MEMORY.md')]
        cfg = getattr(self, 'cfg', {})
        if cfg.get('bot_memory_file'):
            protected.append(os.path.join(HERE, cfg['bot_memory_file']))
        journal = getattr(self, 'journal', None)
        if getattr(journal, 'path', None):
            protected.extend(journal.path + suffix for suffix in ('', '-wal', '-shm', '-journal'))
        return dict(roots=getattr(self, 'send_roots', []),
                    workdir=getattr(self, 'workdir', HERE), here=HERE,
                    protected_paths=protected, max_mb=100)

    @staticmethod
    def _artifact_listing(catalog, *, retrieval=True):
        lines = ['📎 任务产物 · 编号：' + catalog['id']]
        checks = format_result_checks(catalog.get('result_checks', []))
        if not catalog['records']:
            return '\n'.join(lines + ['没有保存的产物声明；不会从普通文字猜测文件，也不会重新运行任务。'] + ([checks] if checks else []))
        for number, record in enumerate(catalog['records'], 1):
            label = record.get('label') or record.get('name') or '未命名文件'
            status = ('基础读取通过（未核验格式）' if record.get('format') == 'unverified' else '基础检查通过') if record.get('status') == 'valid' else record.get('reason', '未通过检查')
            lines.append('%d. %s · %s · %s' % (number, label, human_size(record.get('size') or 0), status))
            if record.get('path'):
                lines.append('   ' + record['path'])
            if retrieval and record.get('status') == 'valid':
                lines.append('取回这个文件：\n/getfile %s %d' % (catalog['id'], number))
        lines.append('记录时检查存在、非空及可读；格式检查范围随类型而异，不证明内容正确或能在手机打开。')
        if retrieval:
            lines.append('发送前重新核对原文件。')
            if len(catalog['records']) > 1 and all(r.get('status') == 'valid' for r in catalog['records']):
                lines.append('取回全部（受单次发送数量上限限制）：\n/getfile %s all' % catalog['id'])
        if checks:
            lines.append(checks)
        return '\n'.join(lines)

    def _capture_artifacts(self, msg, text):
        self._set_stage(msg, '正在核验声明文件和已配置的结果条件', phase='check')
        options = self._artifact_options()
        if getattr(msg, '_task_output_dir', None):
            options['roots'] = [msg._task_output_dir]
        for record in getattr(msg, '_batch_frozen', []):
            options['protected_paths'].extend((os.path.dirname(record['path']), record['original_path']))
        clean, records = collect_artifacts(str(text or ''), **options)
        for record in records:
            record['label'] = self._mask(record.get('label', ''))
        msg._artifacts = records
        msg._artifact_saved = False
        msg._result_checks_saved = False
        control = getattr(msg, '_task_control', None)
        msg._result_checks = run_result_checks(getattr(msg, '_template_checks', ''), records, options,
                                               check_cancelled=control.check if control else None)
        for report in msg._result_checks:
            report['file'] = self._mask(report['file'])
            report['detail'] = self._mask(report['detail'])
        journal, ident = getattr(self, 'journal', None), getattr(msg, '_journal_id', None)
        if (records or msg._result_checks) and isinstance(journal, TaskJournal) and ident:
            try:
                if msg._result_checks:
                    msg._artifact_saved = journal.save_artifacts(msg.from_user_id, ident, records,
                                                                 result_checks=msg._result_checks)
                    msg._result_checks_saved = msg._artifact_saved
                else:
                    msg._artifact_saved = journal.save_artifacts(msg.from_user_id, ident, records)
            except Exception as exc:
                log.warning('产物清单未能保存（%s）', type(exc).__name__)
        # Also expose inspected files through the existing local history locator.
        outputs = getattr(msg, '_output_files', [])
        for record in records:
            if record.get('status') == 'valid' and record.get('path') not in outputs:
                outputs.append(record['path'])
        msg._output_files = outputs
        return clean

    def _artifact_notice(self, msg):
        records = getattr(msg, '_artifacts', [])
        reports = getattr(msg, '_result_checks', [])
        if not records and not reports:
            return ''
        ident = getattr(msg, '_journal_id', '')
        saved = bool(ident and getattr(msg, '_artifact_saved', False))
        notice = self._artifact_listing(dict(id=ident or '本次任务', records=records,
                                            result_checks=reports), retrieval=saved)
        if not saved:
            notice += '\n⚠️ 产物清单未保存，暂不能按任务编号取回；请保留以上路径。'
        if reports and not getattr(msg, '_result_checks_saved', False):
            notice += '\n⚠️ 检查结论未保存到产物记录，请保留本条回复。'
        return notice

    def _artifact_command(self, msg, text):
        text = ARTIFACT_ALIASES.get(text.strip(), text.strip())
        parts = text.split()
        command = parts[0].lower() if parts else ''
        if command not in ('/artifacts', '/getfile'):
            return False
        if getattr(msg, 'attachments', None):
            self.reply(msg, '🔕 产物指令请单独发送；本条附件未接收。')
            return True
        if not (len(parts) in ((1, 2) if command == '/artifacts' else (2, 3))):
            self.reply(msg, '📎 用法：/artifacts 编号（或 latest）；/getfile 编号 序号（或 all）。')
            return True
        journal = getattr(self, 'journal', None)
        if not isinstance(journal, TaskJournal):
            self.reply(msg, '🔕 任务记录未启用，无法按任务编号查看或取回产物。')
            return True
        try:
            catalog = journal.artifacts(msg.from_user_id, parts[1] if len(parts) > 1 else 'latest')
            if catalog is None or catalog['state'] in ACTIVE_STATES:
                self.reply(msg, '🔕 未找到自己的已结束任务；执行中或未开始的任务暂不能取产物。')
                return True
            if command == '/artifacts' or not catalog['records']:
                self.reply(msg, self._mask(self._artifact_listing(catalog)), result=False)
                return True
            uid = msg.from_user_id
            if getattr(self, 'standby_enabled', False) and (uid in self._draining or uid not in self._awake):
                self.reply(msg, '🔕 助手正在待机或收尾，本条附件取回未入队。请先说“开始对话”，再发送取回指令；产物清单仍可随时查看。')
                return True
            if not getattr(self, 'send_file_enable', False):
                self.reply(msg, '🔕 发文件功能未启用；可以用 /artifacts 查看保存的产物清单。')
                return True
            selector = parts[2].lower() if len(parts) == 3 else ''
            records = catalog['records']
            if selector == 'all':
                chosen = records
            elif selector.isascii() and selector.isdigit() and 1 <= int(selector) <= len(records):
                chosen = [records[int(selector) - 1]]
            elif not selector and len(records) == 1:
                chosen = records
            else:
                self.reply(msg, self._mask(self._artifact_listing(catalog)) + '\n请指定上面清单中的序号或 all。', result=False)
                return True
            if len(chosen) > self.send_file_limit:
                self.reply(msg, '🔕 共 %d 个产物，超过单次 %d 个的发送上限；请按清单序号分别取回。' %
                           (len(chosen), self.send_file_limit))
                return True
            if any(r.get('status') != 'valid' for r in chosen):
                self.reply(msg, '🔕 所选产物有文件未通过记录时的基础检查；请用 /artifacts 查看原因。')
                return True
            paths = [r['path'] for r in chosen]
            for path in paths:
                why = file_send_reason(path, self.send_roots, self.send_file_max_mb)
                if why:
                    self.reply(msg, why)
                    return True
            job = dict(sendfile=paths, artifacts=chosen, uid=msg.from_user_id,
                       artifact_task_id=catalog['id'])
            full = self._enqueue((msg, '', job), msg)
            self.reply(msg, full or '📎 已排队取回产物；会核对原文件指纹，通过后才上传，不重新执行任务。')
        except Exception as exc:
            log.warning('产物查询暂不可用（%s）', type(exc).__name__)
            self.reply(msg, '⚠️ 产物记录或文件暂时无法读取，请稍后重试；本次没有重新执行任务。')
        return True

    def _verified_artifact_snapshot(self, record, *, task_id=None):
        return verified_artifact_snapshot(record, staging_dir=os.path.join(HERE, '.runtime', 'artifact-transfers'),
                                          staging_roots=[HERE],
                                          task_id=task_id,
                                          registry_dir=os.path.join(HERE, '.runtime', 'snapshot-lifecycle'),
                                          **self._artifact_options())
