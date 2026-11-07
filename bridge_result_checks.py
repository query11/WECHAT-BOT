"""Check declared output bytes without changing execution or delivery state."""
import logging
import os
import time

from task_artifacts import read_verified_artifact
from template_result_checks import parse_checks, evaluate_rule

log = logging.getLogger('bridge')
CHECK_MAX_BYTES = 10 * 1024 * 1024
CHECK_BUDGET_SECONDS = 10
KINDS = {'required_columns': '必需列', 'column_sum': '列合计', 'required_sections': '必需章节'}
STATUSES = {'passed': '通过', 'failed': '未通过', 'unverified': '未能检查'}


def format_result_checks(reports, *, heading=True):
    if not reports:
        return ''
    lines = ['结果检查（独立于文件生成状态）：'] if heading else []
    for report in reports[:12]:
        if not isinstance(report, dict):
            continue
        condition = str(report.get('condition', ''))[:1000]
        condition = '（' + condition + '）' if condition else ''
        lines.append('%s · %s · %s：%s' % (
            STATUSES.get(report.get('status'), '未能检查'),
            KINDS.get(report.get('kind'), '检查条件'), str(report.get('file', ''))[:255] + condition,
            str(report.get('detail', '检查记录不可用'))[:300]))
    if heading:
        lines.append('仅核对指定条件；未通过或未能检查的产物仍保留，不自动重跑。')
    return '\n'.join(lines)


def run_result_checks(text, records, options, *, check_cancelled=None):
    if not text:
        return []
    try:
        rules = parse_checks(text)
    except Exception:
        return [dict(file='', kind='', status='unverified', detail='检查条件不可用，请核对本次模板版本。')]
    reports = []
    deadline = time.monotonic() + CHECK_BUDGET_SECONDS
    # Read each distinct output once; release bytes before the next file.
    grouped = {}
    for index, rule in enumerate(rules):
        grouped.setdefault(rule['file'], []).append((index, rule))
    ordered = {}
    for name, group in grouped.items():
        if check_cancelled:
            check_cancelled()
        matches = [r for r in records if os.path.basename(r.get('path') or r.get('name', '')) == name]
        raw, problem = None, ''
        if len(matches) != 1:
            problem = '没有唯一匹配的已声明产物，请核对文件名与产物清单。'
        elif matches[0].get('status') != 'valid':
            problem = '产物未通过基础检查，无法核对内容。'
        elif (not isinstance(matches[0].get('size'), int)
              or matches[0]['size'] > CHECK_MAX_BYTES):
            problem = '文件超过结果检查的 10 MiB 上限或大小记录不可用。'
        elif time.monotonic() >= deadline:
            problem = '本次结果检查已到时间上限。'
        else:
            try:
                read_options = dict(options, max_mb=10)
                checked, raw = read_verified_artifact(matches[0], **read_options)
                if checked.get('status') != 'valid' or not isinstance(raw, bytes):
                    raw, problem = None, '文件已变化或无法安全读取登记版本，请核对原文件。'
            except Exception as exc:
                log.warning('结果检查读取失败（%s）', type(exc).__name__)
                problem = '无法安全读取登记版本，产物仍保留。'
        if check_cancelled:
            check_cancelled()
        for index, rule in group:
            if check_cancelled:
                check_cancelled()
            outcome = dict(status='unverified', detail=problem or '本次结果检查已到时间上限。')
            if raw is not None and not problem and time.monotonic() < deadline:
                try:
                    outcome = evaluate_rule(rule, raw)
                    if (not isinstance(outcome, dict) or outcome.get('status') not in STATUSES
                            or not isinstance(outcome.get('detail'), str)):
                        raise ValueError('invalid check outcome')
                except Exception as exc:
                    log.warning('结果检查未完成（%s）', type(exc).__name__)
                    outcome = dict(status='unverified', detail='检查器未能完成，产物仍保留。')
            if rule['kind'] == 'column_sum':
                condition = '工作表「%s」，列「%s」，目标合计 %s' % (
                    rule['sheet'], rule['column'], rule['expected'])
            elif rule['kind'] == 'required_columns':
                condition = '工作表「%s」，必需列：%s' % (
                    rule['sheet'], '、'.join(rule['columns']))
            else:
                condition = '必需章节：' + '、'.join(rule['sections'])
            report = dict(file=name, kind=rule['kind'], condition=condition[:1000],
                          status=outcome['status'], detail=outcome['detail'][:300])
            ordered[index] = report
            if len(matches) == 1:
                matches[0].setdefault('result_checks', []).append(report)
        raw = None
    reports = [ordered[index] for index in sorted(ordered)]
    return reports
