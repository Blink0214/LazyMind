"""Independent numerical checks on original observations selected by the locked plan.

This is a verifier, not the production analysis program. Unsupported estimators are
rejected until a reference and its diagnostics have been registered.
"""
from __future__ import annotations

import math
import operator
import re
from datetime import datetime, timezone

from .views import materialize
from .plan_repair import AnalysisFeasibilityError


class InferenceUnavailable(ValueError):
    """A point estimate exists but its frequentist uncertainty is undefined."""


def reference_with_fallback(job, inputs, models, views, plan):
    """Keep the estimand and observations; disclose unavailable inference locally."""
    try:
        return reference(job, inputs, models, views)
    except InferenceUnavailable as error:
        import copy
        descriptive = copy.deepcopy(job)
        descriptive['operation'] = {'paired_t': 'paired_difference', 'welch_t': 'difference'}.get(
            job['operation'], job['operation'])
        descriptive['interval_level'] = None
        # Verify before committing the fallback; missing data, bad matching,
        # overlapping cohorts and unsupported methods still cannot be fabricated.
        result = reference(descriptive, inputs, models, views)
        note = (f'{job["id"]} ({job["operation"]}, {job["column"]}): {error}。'
                '仅交付相同观测范围的描述性点估计；原请求的置信区间/检验不可估计，'
                '不返回 p 值、不补造误差条，不能解释为无显著差异或证明真实效果。')
        job.update(descriptive)
        plan['limitations'].append(note)
        for figure in plan['figures']:
            if job['id'] in figure['finding_ids']:
                warning = (' Inference unavailable for ' + job['id']
                           + ': insufficient observations or zero variance. Descriptive estimate only; '
                           'do not display significance or invent uncertainty bounds for this finding.')
                figure['instructions'] += warning
                if 'Some requested intervals/tests are unavailable' not in figure['caption']:
                    figure['caption'] += ' Some requested intervals/tests are unavailable; affected estimates are descriptive only.'
        return result


def regression_support(job, table, selected, used, rank):
    from .profiles import support
    columns, rows = table['columns'], table['rows']
    return dict(selected_row_count=len(selected), complete_row_count=len(used),
                design_rank=rank, required_rank=len(job['predictors']) + 1,
                predictors={c: support(rows[i][columns.index(c)] for i in used) for c in job['predictors']})


def select(table, filters):
    indices = {c: i for i, c in enumerate(table['columns'])}
    ops = {'eq': operator.eq, 'ne': operator.ne, 'gt': operator.gt,
           'ge': operator.ge, 'lt': operator.lt, 'le': operator.le}

    def comparable(value, index):
        if value is None or table['column_types'][index] != 'datetime':
            return value
        if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value
        ):
            raise ValueError('时间筛选值必须为带时区的 RFC3339 时间')
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError('时间筛选值必须为有效的 RFC3339 时间') from exc

    prepared = []
    for rule in filters:
        index, op, expected = indices[rule['column']], rule['operator'], rule['value']
        if op == 'in':
            if not isinstance(expected, list):
                raise ValueError('in 筛选须提供列表')
            expected = [comparable(value, index) for value in expected]
        elif op != 'not_null':
            expected = comparable(expected, index)
        prepared.append((index, op, expected))

    def matches(row, rule):
        index, op, expected = rule
        value = row[index]
        if op == 'not_null':
            return value is not None
        value = comparable(value, index)
        if op == 'in':
            return value in expected
        if value is None or expected is None:
            return op == 'eq' and value is expected or op == 'ne' and value is not expected
        try:
            return ops[op](value, expected)
        except TypeError as exc:
            raise ValueError('筛选值类型不兼容') from exc
    return [i for i, row in enumerate(table['rows']) if all(matches(row, f) for f in prepared)]


def reference(job, inputs, models, views=None):
    import numpy as np
    from scipy import stats

    tables, lineage, dropped = views or materialize(inputs, [])
    table = tables[job['table_id']]
    columns, rows = table['columns'], table['rows']
    response = columns.index(job['column'])
    selected = select(table, job['filters'])
    comparison = job['operation'] in ('difference', 'paired_difference', 'paired_t', 'welch_t')
    base = select(table, job['baseline_filters']) if comparison else []
    if comparison and set(selected) & set(base):
        raise ValueError('比较组与基线观测范围重叠')
    required = [response, *[columns.index(c) for c in job['predictors']]]
    if job.get('other_column'):
        required.append(columns.index(job['other_column']))
    if job['trials_column']:
        required.append(columns.index(job['trials_column']))

    def usable(ids):
        return [i for i in ids if all(rows[i][c] is not None for c in required)]

    used, control = usable(selected), usable(base)
    excluded = len(selected) + len(base) - len(used) - len(control)
    if excluded and job['missing_policy'] == 'error':
        raise ValueError('分析范围包含缺失值，计划未允许完整观测筛选')
    if not used or comparison and not control:
        if job['operation'] == 'ols_coefficient':
            raise AnalysisFeasibilityError('OLS_NOT_IDENTIFIABLE', '筛选后没有可分析观测',
                                           regression_support(job, table, selected, used, 0))
        raise ValueError('筛选后没有可分析观测')
    op = job['operation']
    design = inputs['study_design'] or {}
    inference = (job['interval_level'] is not None or job.get('correlation_test', False)
                 or op in ('paired_t', 'welch_t', 'ols_coefficient'))
    if inference or op.startswith('beta_'):
        keys = design.get('unit_keys', [])
        if design.get('repeated_measures') and not keys:
            raise ValueError('重复测量推断需要明确对象标识，不得逐行当作独立样本')
        if keys:
            names = [key['column'] if key['table_id'] == job['table_id'] else
                     f'{key["table_id"]}.{key["column"]}' for key in keys]
            if any(name not in columns for name in names):
                raise ValueError('实验单位未包含在当前可验证的关联视图中')
            key_indices = [columns.index(name) for name in names]
            identities_by_group = []
            for selected_ids in [used, control]:
                identities = [tuple(rows[i][k] for k in key_indices) for i in selected_ids]
                if any(None in key for key in identities) or len(set(identities)) != len(identities):
                    raise ValueError('同一分析组包含重复或未知实验单位，不能按独立观测推断')
                identities_by_group.append(set(identities))
            if op in ('difference', 'welch_t') and identities_by_group[0] & identities_by_group[1]:
                raise ValueError('独立组推断包含跨组重复实验单位；须建模依赖关系')
    numeric = {'value': None, 'lower': None, 'upper': None, 'statistic': None, 'p_value': None}
    x = np.asarray([rows[i][response] for i in used], dtype=float) if op != 'count' else np.asarray([])
    y = np.asarray([rows[i][response] for i in control], dtype=float) if op != 'count' else np.asarray([])
    paired = op in ('paired_difference', 'paired_t')
    n, unit = len(used) + len(control), 'observation'
    if paired:
        indices = [columns.index(c) for c in job['pair_by']]

        def keyed(ids):
            pairs = {}
            for i in ids:
                key = tuple(rows[i][c] for c in indices)
                if None in key or key in pairs:
                    raise ValueError('配对键缺失或重复')
                pairs[key] = i
            return pairs
        first, second = keyed(used), keyed(control)
        common = [key for key in first if key in second]
        if not common:
            raise ValueError('没有匹配的观测对')
        excluded = len(set(keyed(selected)) | set(keyed(base))) - len(common)
        used, control = [first[k] for k in common], [second[k] for k in common]
        x = np.asarray([rows[i][response] for i in used], dtype=float)
        y = np.asarray([rows[i][response] for i in control], dtype=float)
        n, unit = len(common), 'pair'
    level = job['interval_level']
    df, se = None, None
    if op == 'count':
        numeric['value'] = len(used)
    elif op in ('pearson', 'spearman'):
        from .correlation import correlation
        y = np.asarray([rows[i][columns.index(job['other_column'])] for i in used], dtype=float)
        numeric = correlation(job, x, y)
    elif op in ('mean', 'difference', 'paired_difference', 'paired_t', 'welch_t'):
        numeric['value'] = float(x.mean() - y.mean()) if comparison else float(x.mean())
        if level or op in ('paired_t', 'welch_t'):
            if paired or not comparison:
                values = x - y if paired else x
                if len(values) < 2:
                    raise InferenceUnavailable('推断所需观测数不足')
                df = len(values) - 1
                se = float(values.std(ddof=1) / math.sqrt(len(values)))
            else:
                if min(len(x), len(y)) < 2:
                    raise InferenceUnavailable('两组推断所需观测数不足')
                a, b = x.var(ddof=1) / len(x), y.var(ddof=1) / len(y)
                se = float(math.sqrt(a + b))
                if a + b <= 0:
                    raise InferenceUnavailable('检验方差为零')
                df = float((a + b)**2 / (a * a / (len(x) - 1) + b * b / (len(y) - 1)))
            if se <= 0 or not math.isfinite(se):
                raise InferenceUnavailable('推断标准误为零或不可定义')
            if level:
                radius = float(stats.t.ppf((1 + level) / 2, df) * se)
                numeric.update(lower=numeric['value'] - radius, upper=numeric['value'] + radius)
            if op in ('paired_t', 'welch_t'):
                statistic = numeric['value'] / se
                alternative = job['alternative']
                p = (2 * stats.t.sf(abs(statistic), df) if alternative == 'two_sided' else
                     stats.t.sf(statistic, df) if alternative == 'greater' else stats.t.cdf(statistic, df))
                numeric.update(statistic=float(statistic), p_value=float(p))
    elif op == 'ols_coefficient':
        design = np.asarray([[1., *[rows[i][columns.index(c)] for c in job['predictors']]] for i in used], dtype=float)
        rank = int(np.linalg.matrix_rank(design))
        if len(used) <= design.shape[1] or rank != design.shape[1]:
            raise AnalysisFeasibilityError('OLS_NOT_IDENTIFIABLE', '回归样本不足或设计矩阵秩亏',
                                           regression_support(job, table, selected, used, rank))
        beta = np.linalg.lstsq(design, x, rcond=None)[0]
        residuals = x - design @ beta
        df = len(x) - design.shape[1]
        position = ['intercept', *job['predictors']].index(job['coefficient'])
        se = float(math.sqrt(float(residuals @ residuals / df) * np.linalg.inv(design.T @ design)[position, position]))
        numeric['value'] = float(beta[position])
        if level:
            radius = float(stats.t.ppf((1 + level) / 2, df) * se)
            numeric.update(lower=numeric['value'] - radius, upper=numeric['value'] + radius)
    elif op.startswith('beta_'):
        if job['trials_column']:
            trials = [rows[i][columns.index(job['trials_column'])] for i in used]
            if any(type(t) is not int or t <= 0 for t in trials):
                raise ValueError('聚合试验数必须为正整数')
            if any(not float(v).is_integer() or not 0 <= v <= t for v, t in zip(x, trials)):
                raise ValueError('成功数必须是 0..试验数 的整数')
            n = sum(trials)
            excluded = None if excluded else 0
        else:
            if any(v not in (0, 1) for v in x):
                raise ValueError('Beta–Bernoulli 只接受已声明的二元观测')
            n = len(x)
        unit = 'trial'
        prior = models[job['model_id']]['priors'][0]['parameters']
        alpha, beta = prior['alpha'] + float(x.sum()), prior['beta'] + n - float(x.sum())
        if op == 'beta_mean':
            numeric['value'] = alpha / (alpha + beta)
            if level:
                numeric.update(lower=float(stats.beta.ppf((1 - level) / 2, alpha, beta)),
                               upper=float(stats.beta.ppf((1 + level) / 2, alpha, beta)))
        else:
            if level:
                raise ValueError('解析后验事件概率不附加参数区间')
            function = stats.beta.sf if job['operator'] in ('gt', 'ge') else stats.beta.cdf
            numeric['value'] = float(function(job['threshold'], alpha, beta))
    if any(v is not None and not math.isfinite(v) for v in numeric.values()):
        raise ValueError('参考结果非有限值')
    provenance = {}
    for row_id in set(used + control):
        for table_id, source_row in lineage[job['table_id']][row_id].items():
            provenance.setdefault(table_id, set()).add(source_row)
    return numeric, {'source_rows': [{'table_id': key, 'count': len(provenance[key])} for key in sorted(provenance)],
                     'n': n, 'unit': unit, 'excluded_n': None if dropped.get(job['table_id']) else excluded}


def normalize_inference_fields(result, plan):
    """Unrequested uncertainty is not an algorithm-owned output choice."""
    if not isinstance(result, dict):
        return result
    normalized = dict(result)
    for job in plan['jobs']:
        numeric = result.get(job['id'])
        if not isinstance(numeric, dict) or set(numeric) != {'value', 'lower', 'upper', 'statistic', 'p_value'}:
            continue  # The verifier still reports malformed or missing results.
        numeric = dict(numeric)
        if job['interval_level'] is None:
            numeric.update(lower=None, upper=None)
        if (job['operation'] not in ('welch_t', 'paired_t')
                and not (job['operation'] in ('pearson', 'spearman') and job['correlation_test'])):
            numeric.update(statistic=None, p_value=None)
        normalized[job['id']] = numeric
    return normalized


def verify(result, plan, inputs, references=None):
    if not isinstance(result, dict) or set(result) != {job['id'] for job in plan['jobs']}:
        raise ValueError('生成代码的任务结果缺失或多出未知任务')
    models = {m['id']: m for m in plan['models']}
    views = materialize(inputs, plan['joins']) if references is None else None
    verified, errors = {}, []
    for job in plan['jobs']:
        expected, size = references[job['id']] if references is not None else reference(job, inputs, models, views)
        actual = result[job['id']]
        if not isinstance(actual, dict) or set(actual) != set(expected):
            errors.append(f'{job["id"]} 结果字段错误')
            continue
        for key, value in expected.items():
            number = actual[key]
            if value is None:
                if number is not None:
                    errors.append(f'{job["id"]}.{key} 不应存在')
            elif (type(number) not in (int, float) or not math.isfinite(number)
                  or not math.isclose(number, value, rel_tol=1e-7, abs_tol=1e-10)):
                errors.append(f'{job["id"]}.{key} 未通过独立数值复核')
        verified[job['id']] = (actual, size)
    if errors:
        # Give actionable semantics together, without revealing reference answers to copy into generated code.
        rules = ('区间契约：interval_level 非空时必须返回双侧区间，两端均为数值；'
                 '均值/差/OLS 半宽=t.ppf((1+interval_level)/2, df)*SE；Pearson 使用 Fisher-z 区间。'
                 '相关分析按两列共同非缺失观测计算，Spearman 对并列值取平均秩。'
                 'correlation_test=false 时 statistic/p_value 均为 null。alternative 仅控制检验 p 值，'
                 '不能改变区间方向。paired_difference 只估计匹配差及区间，statistic/p_value 必须为 null。'
                 '所有值仍须从原始观测计算，不能硬编码答案。')
        raise ValueError('；'.join(errors[:24]) + '。' + rules)
    return verified
