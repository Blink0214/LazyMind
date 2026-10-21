"""Readable evidence tables and separate, lossless analysis provenance."""
import json
import re


OPERATIONS = {
    'count': '有效非空记录数', 'mean': '均值', 'difference': '独立组均值差',
    'paired_difference': '配对均值差', 'paired_t': '配对均值差', 'welch_t': '独立组均值差',
    'ols_coefficient': 'OLS 系数', 'pearson': 'Pearson r', 'spearman': 'Spearman rho',
    'beta_mean': '后验均值', 'beta_probability': '后验事件概率',
}
INTERVALS = {'confidence': '置信区间', 'credible': '后验可信区间', 'prediction': '预测区间'}


def cell(value):
    text = str(value) if value is not None else '—'
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('|', '&#124;').replace('\r', '').replace('\n', ' / '))


def scope_label(filters):
    symbols = {'eq': '=', 'ne': '≠', 'gt': '>', 'ge': '≥', 'lt': '<', 'le': '≤',
               'in': '∈', 'not_in': '∉', 'not_null': '非缺失', 'is_null': '缺失'}
    return '；'.join(f'{rule["column"]} {symbols.get(rule["operator"], rule["operator"])} '
                    f'{rule["value"] if rule["value"] is not None else ""}'.rstrip() for rule in filters)


def finding_label(finding, job=None):
    if finding.get('probability') and not job:
        return finding['probability']['event'] + ' · 后验事件概率'
    if not job or not job.get('column'):
        return re.sub(r'[:：]\s*[+-]?[\d.eE+-]+\s*$', '', finding['statement'])
    metric = job['column']
    if '.' not in metric:
        metric = job['table_id'] + '.' + metric
    if job.get('other_column'):
        metric += ' / ' + job['other_column']
    if job['operation'] == 'ols_coefficient' and job.get('parameter'):
        metric += ' · ' + job['parameter']
    if finding.get('probability'):
        metric = finding['probability']['event']
    label = metric + ' · ' + OPERATIONS.get(job['operation'], job['operation'])
    selected, baseline = job.get('filters', []), job.get('baseline_filters', [])
    if baseline:
        common = [rule for rule in selected if rule in baseline]
        left = scope_label([rule for rule in selected if rule not in common]) or '全体'
        right = scope_label([rule for rule in baseline if rule not in common]) or '全体'
        label += '；' + left + ' − ' + right
        if common:
            label += '；共同范围：' + scope_label(common)
    elif selected:
        label += '；' + scope_label(selected)
    return label


def interval_text(finding):
    interval = finding['interval']
    if not interval:
        return '—'
    return (f'{interval["level"] * 100:g}% {INTERVALS[interval["kind"]]} '
            f'[{interval["lower"]:.5g}, {interval["upper"]:.5g}]')


def numeric_table(findings, jobs=None, anchors=None, compact=False):
    """Numbers come exclusively from verified findings, never model-written numeric prose."""
    jobs = jobs or {}
    headers = ['指标与比较', '估计值', '区间'] if compact else [
        '结果', '指标与比较', '估计值', '有效样本 / 排除', '区间', '原始 p / 校正 p']
    lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |']
    for index, finding in enumerate(findings, 1):
        value = f'{finding["value"]:.5g}' + (' ' + finding['unit'] if finding['unit'] else '')
        label = finding_label(finding, jobs.get(finding['id']))
        if compact:
            row = [label, value, interval_text(finding)]
        else:
            size = finding['sample_size']
            sample = f'n={size["n"]} {size["unit"]}；排除={size["excluded_n"]}'
            test = finding['test']
            pvalue = '—'
            if test:
                pvalue = f'{test["p_value"]:.5g}'
                if test['adjusted_p_value'] is not None:
                    pvalue += f' / {test["adjusted_p_value"]:.5g} ({test["adjustment"]})'
                else:
                    pvalue += ' / —'
            row = [f'R{(anchors or {}).get(finding["id"], index)}', label, value, sample,
                   interval_text(finding), pvalue]
        lines.append('| ' + ' | '.join(cell(item) for item in row) + ' |')
    return '\n'.join(lines)


def result_jobs(analysis, plan=None):
    jobs = {job['id']: job for job in (plan or {}).get('jobs', [])}
    for finding in analysis['findings']:
        if finding['id'] in jobs:
            continue
        try:
            job = json.loads(finding['calculation'])
        except (TypeError, ValueError):
            continue  # Earlier reports store a human-readable calculation string.
        if isinstance(job, dict) and job.get('id') == finding['id'] and job.get('operation') and job.get('column'):
            jobs[finding['id']] = job
    return jobs


def complete_results(analysis, plan=None):
    """Full result table is a separate section, grouped by the original research question."""
    jobs = result_jobs(analysis, plan)
    anchors = {finding['id']: index for index, finding in enumerate(analysis['findings'], 1)}
    lines = ['<a id="block-complete-results"></a>', '', '## 完整统计结果', '']
    for target in analysis['targets']:
        selected = [finding for finding in analysis['findings'] if finding['target_id'] == target['id']]
        if selected:
            lines += ['### ' + target['question'], '', numeric_table(selected, jobs, anchors), '']
    return '\n'.join(lines)


def provenance(analysis, plan=None):
    """Keep every original identifier, exact value, method, prior, diagnostic and optional scope."""
    result = {key: value for key, value in analysis.items() if key != 'summary'}
    if plan is not None:
        result['locked_plan'] = plan
    return result


def provenance_markdown(analysis, plan=None):
    """Keep the editable report free of unsupported HTML and duplicate machine records."""
    return ('## 追溯记录\n\n'
            f'本次 {len(analysis["findings"])} 项结果的精确数值、内部编号、计算依据、模型与检查'
            '保存在结构化数据分析材料池 `data_analysis_pool.json` 中。')
