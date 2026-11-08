"""Deterministic conversation shortcuts for explicitly collected attachments."""
import re

from attachment_batches import BatchError
from task_templates import TemplateError, validate_snapshot


def parse_batch_dialog(text):
    text = text.strip().rstrip('。！？!?').rstrip()
    if text in ('接下来这几份文件一起处理', '这些文件一起处理', '开始收集附件'):
        return 'start', '本次附件'
    match = re.fullmatch(r'开始收集附件[\s：:]+(.{1,80})', text)
    if match:
        return 'start', match[1]
    match = re.fullmatch(r'(?:附件)?(?:发完了|传完了)(?:[\s，,:：]+(.+))?', text, re.DOTALL)
    if match:
        return 'prepare', (match[1] or '').strip()
    if text in ('确认执行这批附件', '确认执行'):
        return 'confirm', ''
    if text == '取消本次附件处理':
        return 'cancel', ''
    return None


def handle_batch_dialog(bridge, msg, text):
    command = parse_batch_dialog(text)
    if command is None:
        return False
    action, value = command
    uid = msg.from_user_id
    try:
        if getattr(msg, 'attachments', None):
            raise BatchError('这条指令请单独发送；本条附件未接收。')
        store = getattr(bridge, 'batch_store', None)
        if store is None:
            raise BatchError('批次记录未启用，暂不能收集或执行附件任务。')
        if getattr(bridge, 'standby_enabled', False) and (uid not in bridge._awake or uid in bridge._draining):
            raise BatchError('请先说“开始对话”，再操作附件批次。')
        if action == 'start':
            batch = store.create(uid, value)
            answer = ('📦 已开始收集“%s”。请逐份发送附件，最后说“发完了，任务要求”，'
                      '例如“发完了，按周报模板整理”。\n%s' % (batch['name'], bridge._batch_listing(batch)))
        elif action == 'cancel':
            batch = store.active(uid) or store.dialog_batch(uid)
            if not batch or not store.cancel(uid, batch['id']):
                raise BatchError('没有正在收集或待确认的附件任务。')
            answer = '✅ 已取消本次附件处理，已保存的文件仍然保留。'
        elif action == 'prepare':
            batch = store.active(uid) or store.dialog_batch(uid) or store.unique_ready(uid)
            if not batch:
                raise BatchError('没有明确的附件批次。请先说“开始收集附件”，或用 /batch list 查看。')
            if not value:
                answer = bridge._batch_listing(batch) + '\n请补充任务要求，例如“发完了，合并这些表格”。'
            else:
                if len(value) > 8000:
                    raise BatchError('任务要求过长，请控制在 8000 字以内。')
                plan = {'body': value}
                template = re.fullmatch(r'按([^\s/\\]{1,40}?)模板(?:整理|处理|运行|执行)(?:[\s，,:：]+(.+))?', value, re.DOTALL)
                if template:
                    from bridge_templates import template_output_dir
                    template_store = getattr(bridge, 'template_store', None)
                    snapshot = template_store.get(uid, template[1]) if template_store else None
                    if snapshot is None:
                        raise BatchError('没有找到“%s”模板，请用 /template list 查看。' % template[1])
                    snapshot = validate_snapshot(snapshot, uid)
                    snapshot['definition']['output_dir'] = template_output_dir(snapshot['definition']['output_dir'], bridge.workdir)
                    plan = {'body': template[2] or '', 'template': snapshot}
                batch = store.prepare_dialog(uid, batch['id'], plan)
                rows = ['%d. %s%s' % (r['index'], r.get('orig') or r['name'],
                                     '（格式模板）' if r['role'] == 'template' else '') for r in batch['files']]
                description = ('%s · 第 %d 版' % (plan['template']['name'], plan['template']['revision'])
                               if plan.get('template') else value)
                answer = ('📋 请核对本次 %d 个附件：\n%s\n任务：%s%s\n'
                          '确认后发送“确认执行这批附件”（15 分钟内有效）；取消请发“取消本次附件处理”。' %
                          (len(rows), '\n'.join(rows), description,
                           '\n补充要求：' + plan['body'] if plan.get('template') and plan['body'] else ''))
        else:
            batch, plan = store.confirm_dialog(uid)
            job = {'batch': batch}
            if plan.get('template'):
                job['template'] = validate_snapshot(plan['template'], uid)
            answer = bridge._enqueue((msg, plan['body'], job), msg)
            answer = answer or ('✅ 已按确认清单加入队列，共 %d 个附件。\n查看结果：\n/result %s' %
                                (len(batch['files']), msg._journal_id))
        bridge.reply(msg, bridge._mask(answer), result=False)
    except (BatchError, TemplateError) as exc:
        bridge.reply(msg, '🔕 ' + str(exc), result=False)
    except Exception:
        bridge.reply(msg, '⚠️ 附件任务操作暂不可用，请用 /batch list 和 /tasks 核对后再操作。', result=False)
    return True
