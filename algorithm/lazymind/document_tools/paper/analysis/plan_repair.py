"""Apply model-authored repairs only to the invalid plan fields or jobs identified by the host."""
from __future__ import annotations

import copy


class AnalysisFeasibilityError(ValueError):
    def __init__(self, code, message, diagnostics):
        self.code, self.diagnostics = code, diagnostics
        super().__init__(code, message, diagnostics)

    def __str__(self):
        return self.args[1]


class UnverifiedMaterialClaim(ValueError):
    def __init__(self, claims):
        self.claims = claims
        super().__init__('模型提出的缺材料声明尚未核实，不能据此终止：' + '；'.join(claims)
                         + '。先核对现有数据，修正模型自行选择的不可计算方案；不得编造数据或省略用户要求。')


def material_claim(claims):
    if not isinstance(claims, list) or not claims or any(not isinstance(v, str) or not v.strip() for v in claims):
        raise ValueError('缺少材料时须说明具体缺失事实')
    return UnverifiedMaterialClaim(copy.deepcopy(claims))


class PlanRepairError(ValueError):
    def __init__(self, issues, *, plan=None):
        self.plan = plan
        merged = {}
        for issue in issues:
            key = (issue.get('job_id'), issue.get('figure_id'), issue['field'])
            if key not in merged:
                merged[key] = copy.deepcopy(issue)
            elif issue['message'] != merged[key]['message']:
                merged[key]['message'] += '；' + issue['message']
        self.issues = list(merged.values())
        super().__init__('；'.join(f'{i.get("job_id", i.get("figure_id"))}.{i["field"] or "job"}: '
                                  f'{i["message"]}' for i in self.issues))


def preserve_scope(job, changed, issue):
    """Only an unidentifiable regression may revise its numeric predictor range."""
    defaults = dict(other_column=None, baseline_filters=[], pair_by=[], alternative='two_sided')
    for field in ('table_id', 'column', 'other_column', 'baseline_filters', 'pair_by', 'alternative'):
        if field in job and changed.get(field, defaults.get(field)) != job[field]:
            raise ValueError('任务修复不能改变原始响应、比较基线或检验方向')
    for rule in job.get('filters', []):
        adjustable = (issue.get('reason_code') == 'OLS_NOT_IDENTIFIABLE'
                      and isinstance(rule, dict) and rule.get('column') in job.get('predictors', [])
                      and rule.get('operator') in ('gt', 'ge', 'lt', 'le'))
        if not adjustable and rule not in changed.get('filters', []):
            raise ValueError('任务修复不能删除原有条件或方法分组筛选')


def apply_updates(plan, response, issues):
    """Patch rejected jobs and their dependencies, keeping unrelated plan content immutable."""
    if (not isinstance(response, dict) or 'updates' not in response
            or set(response) - {'updates', 'model_updates', 'figure_updates'}
            or not isinstance(response['updates'], list)):
        raise ValueError('局部修复必须只返回 updates 列表')
    job_issues = [item for item in issues if 'job_id' in item]
    local_figures = {item['figure_id'] for item in issues if 'figure_id' in item}
    language_figures = {item['figure_id'] for item in issues if item.get('field') == 'display_language'}
    allowed = {(item['job_id'], item['field']) for item in job_issues}
    by_issue = {(item['job_id'], item['field']): item for item in job_issues}
    whole_jobs = {job_id for job_id, field in allowed if field is None}
    if not whole_jobs and (response.get('model_updates') or (response.get('figure_updates') and not local_figures)):
        raise ValueError('字段补全不能修改模型或图表')
    updates = {}
    for item in response['updates']:
        if not isinstance(item, dict) or set(item) != {'job_id', 'field', 'value'}:
            raise ValueError('每个修复项须包含 job_id/field/value')
        key = (item['job_id'], item['field'])
        if key not in allowed or key in updates:
            raise ValueError('局部修复越出指定字段或重复修改同一字段')
        updates[key] = copy.deepcopy(item['value'])
    if set(updates) != allowed:
        raise ValueError('局部修复缺少指定字段')
    result = copy.deepcopy(plan)
    replacements, jobs = {}, []
    for job in result['jobs']:
        identifier = job['id']
        for (job_id, field), value in updates.items():
            if job_id == identifier and field is not None:
                if field not in {'design_evidence_refs', 'pair_by', 'unit'}:
                    raise ValueError('当前字段补全仅支持设计依据引用、配对键和单位')
                job[field] = value
        if (identifier, None) in updates:
            changed = updates[(identifier, None)]
            if not isinstance(changed, list) or not changed or any(
                not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or row.get('target_id') != job['target_id'] for row in changed
            ):
                raise ValueError('任务修复须返回同一分析目标的非空任务列表')
            for row in changed:
                preserve_scope(job, row, by_issue[(identifier, None)])
            replacements[identifier] = [row['id'] for row in changed]
            jobs.extend(changed)
        else:
            jobs.append(job)
    ids = [job['id'] for job in jobs]
    if len(set(ids)) != len(ids):
        raise ValueError('修复后的任务 ID 重复')
    result['jobs'] = jobs
    affected_models = {job.get('model_id') for job in plan['jobs'] if job['id'] in whole_jobs} - {None}
    shared_models = {job.get('model_id') for job in plan['jobs'] if job['id'] not in whole_jobs}
    mutable_models = affected_models - shared_models
    original_models = {m['id']: m for m in plan['models']}
    replacement_ids = {new for replacements_for_job in replacements.values() for new in replacements_for_job}
    replacement_jobs = [j for j in jobs if j['id'] in replacement_ids]
    model_updates = response.get('model_updates', [])
    if not isinstance(model_updates, list):
        raise ValueError('model_updates 必须为模型列表')
    changed_models = {}
    for item in model_updates:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in changed_models:
            raise ValueError('修复模型缺少 ID 或重复')
        identifier = item['id']
        if identifier in original_models and identifier not in mutable_models:
            raise ValueError('不能修改无关任务或共享的模型；应为修复任务创建新模型 ID')
        users = [j for j in replacement_jobs if j.get('model_id') == identifier]
        if not users or set(item.get('target_ids', [])) != {j['target_id'] for j in users}:
            raise ValueError('修复模型只能引用受影响任务的原有目标，且必须实际使用')
        changed_models[identifier] = copy.deepcopy(item)
    used_models = {j.get('model_id') for j in jobs}
    result['models'] = [changed_models.get(m['id'], m) for m in result['models']
                        if m['id'] not in mutable_models or m['id'] in used_models]
    result['models'].extend(m for identifier, m in changed_models.items() if identifier not in original_models)
    affected_figures = {f['id'] for f in plan.get('figures', []) if set(f['finding_ids']) & whole_jobs}
    for figure in result.get('figures', []):
        if figure['id'] not in affected_figures:
            continue
        figure['finding_ids'] = [new for old in figure['finding_ids'] for new in replacements.get(old, [old])]
        figure['finding_ids'] = list(dict.fromkeys(figure['finding_ids']))
    figure_updates = response.get('figure_updates', [])
    if not isinstance(figure_updates, list):
        raise ValueError('figure_updates 必须为图表列表')
    figures = {f['id']: f for f in result.get('figures', [])}
    seen = set()
    for update in figure_updates:
        if not isinstance(update, dict) or set(update) != {'figure_id', 'figure'}:
            raise ValueError('图表修复须包含 figure_id/figure')
        identifier, figure = update['figure_id'], update['figure']
        if identifier not in affected_figures | local_figures or identifier in seen or not isinstance(figure, dict):
            raise ValueError('不能修复无关图表或重复修复图表')
        if any(figure.get(k) != figures[identifier].get(k) for k in ('id', 'columns', 'finding_ids')):
            raise ValueError('图表修复不能删除结果或改变原始来源')
        if identifier in local_figures and identifier not in affected_figures and any(
            figure.get(key) != figures[identifier].get(key)
            for key in figure.keys() | figures[identifier].keys()
            if key not in ({'data_mapping', 'title', 'caption'} if identifier in language_figures else {'data_mapping'})
        ):
            raise ValueError('映射字段修复不能改变图型、研究目的或其他图表字段')
        seen.add(identifier)
        figures[identifier] = copy.deepcopy(figure)
    if not local_figures <= seen:
        raise ValueError('局部修复缺少指定图表映射')
    result['figures'] = [figures[f['id']] for f in result.get('figures', [])]
    return result
