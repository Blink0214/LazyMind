"""Plan -> generated preparation/statistics -> container execution -> checks -> figures."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import os

from . import model
from . import generated_executor as executor
from .plan import validate_plan
from .plan_repair import PlanRepairError, AnalysisFeasibilityError, UnverifiedMaterialClaim
from .figure_plan import check_figure_plan, FigurePlanRepairError
from .reference import verify, reference, select
from .schema import (
    ContractError, binding, digest, normalize_input, table_map, validate_analysis, validate_charts, VERSION,
)
from ..plotting.model import PlotModelError
from .views import materialize
from .methods import executed_methods
from .prompt_payload import runtime_plan
from .presentation import finding_label
from ..execution.threads import ThreadPoolExecutor
from . import trace as diagnostics

_trace_sink = ContextVar('paper_analysis_trace_sink', default=None)


@contextmanager
def capture_execution():
    records = []
    token = _trace_sink.set(records)
    try:
        yield records
    finally:
        _trace_sink.reset(token)


def build_analysis(inputs, plan, checked):
    bindings = binding(inputs)
    targets = {t['id']: t for t in plan['targets']}
    findings, diagnostics = [], []
    views = materialize(inputs, plan['joins'])
    for job in plan['jobs']:
        numeric, size = checked[job['id']]
        op = job['operation']
        posterior = op.startswith('beta_')
        correlation = op in ('pearson', 'spearman')
        probability = op == 'beta_probability'
        kind = ('probability' if probability else 'count' if op == 'count' else
                'effect' if op in ('difference', 'paired_difference', 'paired_t', 'welch_t', 'ols_coefficient')
                else 'estimate')
        interval = None
        if numeric['lower'] is not None:
            interval = dict(kind='credible' if posterior else 'confidence', level=job['interval_level'],
                            lower=numeric['lower'], upper=numeric['upper'],
                            method='Beta 后验等尾区间' if posterior else 'Pearson Fisher-z 双侧区间' if correlation
                            else '双侧 Student t 区间（独立组差使用 Welch 自由度）')
        test = None
        if numeric['p_value'] is not None:
            test = dict(name=op, statistic=numeric['statistic'], p_value=numeric['p_value'], adjusted_p_value=None,
                        adjustment=None, alternative=job['alternative'],
                        null_hypothesis=('总体 Pearson 相关系数为 0' if op == 'pearson' else
                                         '总体 Spearman 秩相关系数为 0' if op == 'spearman' else '总体均值差为 0'),
                        family_id=None)
        event = None
        if probability:
            sign = {'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<='}[job['operator']]
            event = dict(kind='posterior', event=f'P(θ {sign} {job["threshold"]} | 当前数据、先验与模型)',
                         operator=job['operator'], threshold=job['threshold'], threshold_unit=None, mcse=None)
        columns = targets[job['target_id']]['columns']
        statement = finding_label({'probability': event}, job) + f'：{numeric["value"]:.8g}'
        calculation = json.dumps(job, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        findings.append(dict(id=job['id'], target_id=job['target_id'], kind=kind,
                             statement=statement,
                             columns=columns, calculation=calculation,
                             value=numeric['value'], unit=None if posterior or op == 'count' else job['unit'],
                             sample_size=size, interval=interval, test=test, probability=event,
                             model_id=job['model_id']))
        diagnostics.append(dict(id='verified_' + job['id'], model_id=job['model_id'], name='独立数值与样本量复核',
                                outcome='passed', value=numeric['value'], threshold='rtol=1e-7; atol=1e-10',
                                details='从完整原始表按锁定范围重新选择观测，用独立参考实现核对点估计、区间及检验；'
                                '不以生成代码自行报告成功为准。'))
    tests = [f for f in findings if f['test']]
    if tests and plan['multiplicity'] == 'holm':
        ordered = sorted(tests, key=lambda f: f['test']['p_value'])
        maximum = 0.0
        for index, finding in enumerate(ordered):
            maximum = max(maximum, min(1.0, (len(ordered) - index) * finding['test']['p_value']))
            finding['test'].update(adjusted_p_value=maximum, adjustment='holm', family_id='planned_primary_tests')
    limitations = list(plan['limitations'])
    if any(job['operation'] in ('pearson', 'spearman') for job in plan['jobs']):
        limitations.append('相关系数描述锁定范围内两个变量的关联，不能证明因果；零相关也不等于独立。'
                           '不同变量对按共同非缺失观测分别计算，样本量可能不同；缺失值不填零。'
                           '按变量对删缺失值的矩阵不保证为半正定矩阵，不能直接当作协方差模型。')
    if any(job['operation'] == 'pearson' and (job['correlation_test'] or job['interval_level']) for job in plan['jobs']):
        limitations.append('Pearson 的零相关检验依赖独立、二元正态的观测假设，Fisher-z 区间为近似点态区间；'
                           '设计依据与单位键检查不能证明这些分布假设成立。')
    if any(job['operation'] == 'spearman' and job['correlation_test'] for job in plan['jobs']):
        limitations.append('Spearman 检验使用大样本渐近 p 值，只接受共同有效样本数大于五百的任务；未执行置换检验。')
    for identifier, count in views[2].items():
        if count:
            limitations.append(f'关联视图 {identifier} 使用 inner 连接，排除了 {count} 条左表未匹配记录；'
                               '筛选后实验单位排除数无法直接归因，sample_size.excluded_n 标为 null。')
    for m in plan['models']:
        if m['priors']:
            diagnostics.append(dict(id='analytic_' + m['id'], model_id=m['id'], name='MCMC 收敛诊断',
                                    outcome='not_applicable', value=None, threshold=None,
                                    details='当前模型采用解析后验，不产生 MCMC 链或 ESS。'))
            limitations.append(f'{m["id"]} 的后验结果以声明的似然和先验为条件，不表示模型为真的概率。')
            # Analytic checks are performed by trusted code, not assertions supplied by the generated program.
            from scipy import stats
            job = next(j for j in plan['jobs'] if j['model_id'] == m['id'] and j['operation'].startswith('beta_'))
            table = views[0][job['table_id']]
            column = table['columns'].index(job['column'])
            trial_index = table['columns'].index(job['trials_column']) if job['trials_column'] else None
            chosen = [i for i in select(table, job['filters']) if table['rows'][i][column] is not None
                      and (trial_index is None or table['rows'][i][trial_index] is not None)]
            successes = sum(table['rows'][i][column] for i in chosen)
            n = checked[job['id']][1]['n']
            parameters = m['priors'][0]['parameters']
            a, b = parameters['alpha'], parameters['beta']
            for prefix, alpha, beta in [('prior', a, b), ('posterior', a + successes, b + n - successes)]:
                mass = float(stats.betabinom.pmf(successes, n, alpha, beta))
                diagnostics.append(dict(id=prefix + '_predictive_' + m['id'], model_id=m['id'],
                                        name=prefix + ' predictive count check', outcome='passed', value=mass,
                                        threshold=None, details='在相同试验次数下，预测成功总数等于当前观测数的概率。'
                                        '这是解析预测分布检查，不是 p 值，也不证明模型正确。'))
            alternatives = {model['id']: copy.deepcopy(model) for model in plan['models']}
            alternatives[m['id']]['priors'][0]['parameters'] = dict(alpha=2 * a, beta=2 * b)
            sensitivity, _ = reference(job, inputs, alternatives, views)
            diagnostics.append(dict(id='sensitivity_' + m['id'], model_id=m['id'], name='先验集中程度敏感性',
                                    outcome='passed', value=sensitivity['value'], threshold=None,
                                    details=f'保持先验均值，将 alpha/beta 加倍至 {2 * a}/{2 * b}，'
                                    f'同一目标的对照值为 {sensitivity["value"]}；不据此选择更有利先验。'))
    summary = '；'.join(f['statement'] for f in findings)
    return validate_analysis(dict(**bindings, summary=summary, targets=plan['targets'], methods=executed_methods(plan),
                                  findings=findings, models=plan['models'], diagnostics=diagnostics,
                                  limitations=list(dict.fromkeys(limitations))), inputs)


def finish_verified(analysis, inputs, plan, attempts, prefetched=None):
    """Join independent interpretation/figure branches before any output is published."""
    charts, figure_trace = [], []
    if plan['figures']:
        from ..plotting.analysis import draw
        with ThreadPoolExecutor(max_workers=2) as pool:
            interpretation = pool.submit(model.interpret_results, inputs, copy.deepcopy(analysis),
                                         copy.deepcopy(plan), attempts)
            figure_options = dict(inputs=inputs, jobs=copy.deepcopy(plan['jobs']))
            if prefetched is not None:
                figure_options['prefetched'] = prefetched
            figures = pool.submit(draw, copy.deepcopy(analysis), copy.deepcopy(plan['figures']), **figure_options)
            try:
                summary, interpretation_trace = interpretation.result()
                charts, figure_trace = figures.result()
            except BaseException:
                interpretation.cancel()
                figures.cancel()
                raise
    else:
        summary, interpretation_trace = model.interpret_results(
            inputs, copy.deepcopy(analysis), copy.deepcopy(plan), attempts)
    return summary, interpretation_trace, charts, figure_trace


def execute_locked(inputs, plan, references, attempts):
    """Generated statistics remain independent of reference answers used by the figure branch."""
    code, error, history, rendered, checked = '', '', [], None, None
    for attempt in range(1, attempts + 1):
        try:
            with diagnostics.stage('analysis_code_generation', attempt=attempt):
                code = model.generate_program(inputs, copy.deepcopy(plan), code, error)
                diagnostics.save(f'analysis-code-{attempt}.py', code)
            with diagnostics.stage('analysis_code_validation', attempt=attempt):
                executor.validate_code(code)
            payload = dict(tables=table_map(inputs), plan=runtime_plan(plan), code=code, seed=42)
            with diagnostics.stage('analysis_execution', attempt=attempt):
                rendered = executor.execute(payload)
                diagnostics.save(f'computed-results-{attempt}.json', rendered['results'])
            with diagnostics.stage('numerical_verification', attempt=attempt):
                checked = verify(rendered['results'], plan, inputs, references)
            history.append(dict(attempt=attempt, outcome='passed',
                                code_sha256=hashlib.sha256(code.encode()).hexdigest()))
            break
        except (executor.AnalysisEnvironmentError, PlotModelError):
            raise
        except (ValueError, TypeError, KeyError, SyntaxError, RuntimeError) as exc:
            error = str(exc)[-6000:]
            history.append(dict(attempt=attempt, outcome='failed', error=error,
                                code_sha256=hashlib.sha256(code.encode()).hexdigest()))
    if checked is None:
        raise ContractError('EXECUTION_FAILED', 'analysis.code', '统计执行或独立复核失败，未发布结果: ' + error)
    return code, history, rendered, checked


def run(raw_inputs):
    with diagnostics.stage('input_validation'):
        inputs = normalize_input(raw_inputs)
        if diagnostics.enabled():
            diagnostics.save('input-summary.json', {
                **{key: value for key, value in binding(inputs).items() if key.endswith('sha256')},
                'tables': [{'id': key, 'rows': len(table['rows']), 'columns': len(table['columns'])}
                           for key, table in table_map(inputs).items()],
            })
    with diagnostics.stage('environment_validation'):
        executor.configuration()  # Deployment failures are not model/code repair requests.
    try:
        attempts = int(os.environ.get('LAZYMIND_PAPER_GENERATED_ANALYSIS_ATTEMPTS', '3'))
        if not 1 <= attempts <= 3:
            raise ValueError
    except ValueError as exc:
        raise ValueError('生成统计程序尝试次数必须为 1..3') from exc
    plan, error, planning, candidate, issues = None, '', [], None, []
    figure_issues = []
    for attempt in range(1, attempts + 1):
        try:
            if figure_issues and candidate is not None:
                with diagnostics.stage('figure_plan_repair', attempt=attempt):
                    candidate = model.repair_figures(inputs, candidate, figure_issues)
                    diagnostics.save(f'plan-attempt-{attempt}.json', candidate)
                figure_issues = []
            elif issues and candidate is not None:
                with diagnostics.stage('plan_repair', attempt=attempt):
                    candidate = model.repair_plan(inputs, candidate, issues)
                    diagnostics.save(f'plan-attempt-{attempt}.json', candidate)
            else:
                with diagnostics.stage('plan_generation', attempt=attempt):
                    candidate = model.make_plan(inputs, error)
                    diagnostics.save(f'plan-attempt-{attempt}.json', candidate)
            with diagnostics.stage('plan_validation', attempt=attempt):
                candidate = validate_plan(candidate, inputs)
            with diagnostics.stage('reference_validation', attempt=attempt):
                views = materialize(inputs, candidate['joins'])
                models = {m['id']: m for m in candidate['models']}
                references, failed_jobs = {}, []
                for job in candidate['jobs']:
                    try:
                        references[job['id']] = reference(job, inputs, models, views)
                    except AnalysisFeasibilityError as exc:
                        failed_jobs.append(dict(job_id=job['id'], field=None, message=str(exc),
                                                reason_code=exc.code, repair_scope='analytical_choice',
                                                diagnostics=exc.diagnostics))
                    except (ValueError, TypeError, KeyError) as exc:
                        failed_jobs.append(dict(job_id=job['id'], field=None, message=str(exc)))
                if failed_jobs:
                    raise PlanRepairError(failed_jobs)
                check_figure_plan(candidate, references)
                # Bind/validate figure inputs before spending any drawing-code model calls.
                from ..plotting.analysis import prepare_figures
                from ..plotting.figure_data import FigureDataError
                try:
                    prepare_figures(build_analysis(inputs, candidate, references), candidate['figures'],
                                    inputs, candidate['jobs'])
                except FigureDataError as exc:
                    raise FigurePlanRepairError([dict(figure_id=exc.figure_id, message=str(exc))]) from exc
            plan = candidate
            diagnostics.save('locked-plan.json', plan)
            planning.append(dict(attempt=attempt, outcome='passed'))
            break
        except FigurePlanRepairError as exc:
            figure_issues = exc.issues
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error, repair_figures=figure_issues))
        except PlanRepairError as exc:
            if exc.plan is not None:
                candidate = exc.plan
            issues = exc.issues
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error, repair_fields=issues))
        except UnverifiedMaterialClaim as exc:
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error,
                                 unverified_material_claims=exc.claims))
        except ContractError as exc:
            if exc.code in ('MISSING_MATERIAL', 'EXECUTION_FAILED'):
                raise
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error))
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error))
    if plan is None:
        raise ContractError('INVALID_OUTPUT', 'analysis.plan', '分析计划验证失败: ' + error)
    locked = digest(plan)
    from ..plotting.analysis import prefetch_codes
    preview = build_analysis(inputs, plan, references) if plan['figures'] else None
    with prefetch_codes(preview, plan['figures'], inputs, plan['jobs']) as prefetched:
        code, history, rendered, checked = execute_locked(inputs, plan, references, attempts)
        if digest(plan) != locked:
            raise ContractError('INVALID_OUTPUT', 'analysis.plan', '执行中计划发生变化')
        analysis = build_analysis(inputs, plan, checked)
        diagnostics.save('verified-results.json', analysis)
        summary, interpretation_trace, charts, figure_trace = finish_verified(
            analysis, inputs, plan, attempts, prefetched=prefetched)
    analysis['summary'] = summary
    with diagnostics.stage('output_validation'):
        analysis = validate_analysis(analysis, inputs)
        validate_charts(charts, analysis)
        diagnostics.save('analysis.json', analysis)
        diagnostics.save('figures.json', [dict(id=chart['id'], title=chart['title'],
                                              svg_bytes=len(chart['svg'].encode())) for chart in charts])
    trace = dict(contract=VERSION, input_sha256=analysis['input_sha256'], plan=plan, plan_sha256=locked,
                 runtime_plan_sha256=digest(runtime_plan(plan)),
                 plan_adaptations={'alternative_spelling': {'two_sided': 'two-sided'}},
                 code=code, code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                 seed=42, versions=rendered['versions'],
                 planning=planning, attempts=history, interpretation=interpretation_trace, figures=figure_trace)
    sink = _trace_sink.get()
    if sink is not None:
        sink.append(trace)
    return dict(is_demo=False, analysis=analysis, charts=charts)
