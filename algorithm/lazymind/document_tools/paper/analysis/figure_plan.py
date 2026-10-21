"""Validate native figure prerequisites and preserve locked estimands during figure repair."""
import copy


class FigurePlanRepairError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__('；'.join(f'{issue["figure_id"]}: {issue["message"]}' for issue in issues))


def check_figure_plan(plan, references):
    jobs = {job['id']: job for job in plan['jobs']}
    issues = []
    for figure in plan['figures']:
        if figure['style'] != 'bar_paired_delta':
            continue
        selected = [jobs[identifier] for identifier in figure['finding_ids']]
        if any(job['operation'] != 'mean' for job in selected) or any(
            references[job['id']][0]['value'] < 0 for job in selected
        ):
            issues.append(dict(figure_id=figure['id'], message=(
                'bar_paired_delta 的原生函数要求非负的基线与方法绝对均值，不能接收差值作为两组原始值。'
                '保留全部 finding_ids 和图的研究目的，由模型改选可显示现有估计与区间的合适图型；'
                '不得伪造零基线观测、改数值、丢掉负结果或修改统计任务。')))
    if issues:
        raise FigurePlanRepairError(issues)


def apply_figure_updates(plan, response, issues):
    if not isinstance(response, dict) or set(response) != {'updates'} or not isinstance(response['updates'], list):
        raise ValueError('图表局部修复必须仅返回 updates')
    allowed = {issue['figure_id'] for issue in issues}
    previous = {figure['id']: figure for figure in plan['figures']}
    updates = {}
    for update in response['updates']:
        if not isinstance(update, dict) or set(update) != {'figure_id', 'figure'}:
            raise ValueError('图表修复项须包含 figure_id/figure')
        identifier, figure = update['figure_id'], update['figure']
        if identifier not in allowed or identifier in updates or not isinstance(figure, dict):
            raise ValueError('图表局部修复越出指定范围')
        if (figure.get('id') != identifier or figure.get('finding_ids') != previous[identifier]['finding_ids']
                or figure.get('columns') != previous[identifier]['columns']):
            raise ValueError('图表修复不得改变 ID、原始列引用或已选结果及其顺序')
        updates[identifier] = copy.deepcopy(figure)
    if updates.keys() != allowed:
        raise ValueError('图表修复缺少指定图表')
    result = copy.deepcopy(plan)
    result['figures'] = [updates.get(figure['id'], figure) for figure in result['figures']]
    return result
