"""Compact offline F4 integration checks; run inside the Chat container."""
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import lazyllm
import pytest

from lazymind.chat.engine.subagent.context import SubAgentContext
from lazymind.document_tools.paper import workflow
from lazymind.document_tools.paper.analysis import model
from lazymind.document_tools.paper.analysis.job_identity import bind_jobs
from lazymind.document_tools.paper.analysis.schema import ContractError
from lazymind.document_tools.paper.plotting import analysis as plots


ANALYSIS_CODE = """
import pandas as pd

def prepare(tables, plan):
    return {key: pd.DataFrame(table['rows'], columns=table['columns'])
            for key, table in tables.items()}

def analyze(prepared, plan, rng):
    results = {}
    for job in plan['jobs']:
        frame = prepared[job['table_id']]
        for condition in job['filters']:
            frame = frame[frame[condition['column']] == condition['value']]
        results[job['id']] = dict(value=float(frame[job['column']].mean()),
                                  lower=None, upper=None, statistic=None, p_value=None)
    return results
"""

PLOT_CODE = """
import matplotlib.pyplot as plt

def draw(data, statistics, spec):
    layout = spec['style_contract']['layout']
    fig, ax = plt.subplots(figsize=tuple(layout['single_inches']))
    fig.subplots_adjust(**layout['single_panel'])
    draw_reference_panel(ax, **data['figure']['panels'][0]['arguments'])
    return fig
"""


@pytest.fixture(autouse=True)
def runtime_state():
    previous = {key: lazyllm.globals.get(key) for key in ('agentic_config', 'subagent_ctx')}
    yield
    for key, value in previous.items():
        if value is None:
            lazyllm.globals.pop(key, None)
        else:
            lazyllm.globals[key] = value


@pytest.fixture
def replay(monkeypatch, tmp_path):
    groups = {'baseline': [1., 2., 3.], 'candidate': [4., 5., 6.]}
    columns = [dict(table_id='data', column=name) for name in ('method', 'score')]
    inputs = dict(
        topic='模拟：方法得分比较', request='比较各组平均得分，输出分析、结论和图表。',
        data=dict(columns=['method', 'score'], column_types=['string', 'number'],
                  units=[None, 'point'],
                  rows=[[name, score] for name, scores in groups.items() for score in scores]),
    )
    plan = dict(
        targets=[dict(id='comparison', question='各方法的平均得分如何？',
                      estimand='组内算术均值', scope='全部输入观测', columns=columns)],
        models=[], methods=['按方法计算平均得分，仅作描述性比较。'],
        limitations=['模拟数据不能用于实际研究结论。'],
        jobs=[dict(id=name, target_id='comparison', operation='mean', table_id='data',
                   column='score', filters=[dict(column='method', operator='eq', value=name)])
              for name in groups],
        figures=[dict(id='means', title='Mean scores', caption='Synthetic data; group means.',
                      kind='bar', style='bar_grouped_hatch', finding_ids=list(groups),
                      columns=columns, instructions='Compare observed group means.',
                      data_mapping=dict(category_column='data.method'))],
    )
    # Interpretation consumes the host-assigned IDs, not the planner's aliases.
    plan = bind_jobs(plan)
    finding_ids = [job['id'] for job in plan['jobs']]
    conclusion = dict(
        summary='候选方法在这组模拟观测中的平均得分较高。',
        answers=[dict(target_id='comparison', conclusion='结果仅反映当前样本的描述性差异。',
                      finding_ids=finding_ids, key_finding_ids=finding_ids)],
        mechanism_explanation='当前数据不足以解释差异机制。',
        limitations=['模拟观测不能证明因果关系或泛化能力。'],
    )
    structured = Mock(side_effect=[plan, conclusion])
    analysis = Mock(return_value={'code': ANALYSIS_CODE})
    plot = Mock(return_value={'code': PLOT_CODE})
    monkeypatch.setattr(model, 'call_model', structured)
    monkeypatch.setattr(model, 'call_code_model', analysis)
    monkeypatch.setattr(plots, 'call_code_model', plot)
    monkeypatch.setattr(lazyllm, 'AutoModel', Mock(side_effect=AssertionError('Unexpected live model call')))
    monkeypatch.setenv('LAZYMIND_PAPER_GENERATED_ANALYSIS_ATTEMPTS', '2')
    monkeypatch.setenv('LAZYMIND_PAPER_PLOT_ATTEMPTS', '2')
    monkeypatch.delenv('LAZYMIND_PAPER_PLOT_SKILLS', raising=False)
    source = tmp_path / 'input.json'
    request = inputs['request'] = '只执行 F4，分析上传的 input.json，输出分析、结论和图表。'
    source.write_text(json.dumps(inputs), encoding='utf-8')
    lazyllm.globals['agentic_config'] = {
        'user_id': 'f4-test', 'conversation_id': 'f4-test', 'query': request,
        'files': [str(source)], 'history_files_per_turn': {'1': [str(source)]},
    }
    from lazymind.document_tools.paper.analysis.attachments import workflow_input_bindings
    slots = ['data_analysis_pool', 'data_analysis_document', 'analysis_chart']
    events = []
    context = SubAgentContext(
        task_id='f4-test', conversation_id='f4-test', agent_type='workflow_step', objective=request,
        params={'step_id': 'data_analysis', 'user_input': request,
                'remote_inputs': workflow_input_bindings({}, request),
                'output_slot_types': dict.fromkeys(slots, 'file')},
        workspace_path=str(tmp_path), input_slots=['paper_input'], output_slots=slots,
        db=None, emit=events.append,
    )
    lazyllm.globals['subagent_ctx'] = context
    return SimpleNamespace(source=source, plan=plan, conclusion=conclusion, structured=structured,
                           analysis=analysis, plot=plot, events=events, context=context)


@pytest.mark.parametrize('fault', ['none', 'plan', 'mapping', 'interpretation', 'analysis', 'plot'])
def test_f4_workflow_publishes_verified_files(replay, fault):
    if fault == 'plan':
        replay.structured.side_effect = [{}, replay.plan, replay.conclusion]
    elif fault == 'mapping':
        incomplete = copy.deepcopy(replay.plan)
        incomplete['figures'][0].pop('data_mapping')
        repair = dict(updates=[], figure_updates=[dict(figure_id='means', figure=replay.plan['figures'][0])])
        replay.structured.side_effect = [incomplete, repair, replay.conclusion]
    elif fault == 'interpretation':
        invalid = {**replay.conclusion, 'summary': replay.conclusion['summary'] + '[结果:baseline]'}
        replay.structured.side_effect = [replay.plan, invalid, replay.conclusion]
    elif fault == 'analysis':
        replay.analysis.side_effect = [{'code': ANALYSIS_CODE.replace('.mean()', '.sum()')},
                                       {'code': ANALYSIS_CODE}]
    elif fault == 'plot':
        replay.plot.side_effect = [{'code': 'def draw(data, statistics, spec):\n    return missing_name'},
                                   {'code': PLOT_CODE}]

    assert workflow.run_paper_step()['status'] == 'completed'

    artifacts = [event for event in replay.events if event['type'] == 'artifact']
    assert {event['slot'] for event in artifacts} == set(replay.context.output_slots)
    paths = [Path(event['value']['path']) for event in artifacts]
    assert all(path.is_file() and path.stat().st_size for path in paths)
    pool = workflow._read_bound(str(next(p for p in paths if p.suffix == '.json')))
    analysis = workflow.material(pool, 'analysis')['content']
    assert [item['value'] for item in analysis['findings']] == pytest.approx([2., 5.])
    assert replay.conclusion['summary'] in next(p for p in paths if p.suffix == '.md').read_text()
    charts = workflow.material(pool, 'charts')['content']
    assert len(charts) == 1 and charts[0]['title'] == 'Mean scores' and charts[0]['caption']
    assert '<svg' in next(p for p in paths if p.suffix == '.svg').read_text()
    assert next(p for p in paths if p.suffix == '.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert next(p for p in paths if p.suffix == '.pdf').read_bytes().startswith(b'%PDF-')
    assert [item['node'] for item in pool['history']] == ['F4']
    assert (replay.structured.call_count, replay.analysis.call_count, replay.plot.call_count) == (
        2 + (fault in ('plan', 'mapping', 'interpretation')), 1 + (fault == 'analysis'), 1 + (fault == 'plot'))


@pytest.mark.parametrize('fault', ['input', 'calculation'])
def test_f4_failure_is_not_published_and_can_resume(replay, fault):
    original = replay.source.read_bytes()
    if fault == 'input':
        value = json.loads(replay.source.read_text())
        value['data']['rows'][0].pop()
        replay.source.write_text(json.dumps(value), encoding='utf-8')
    else:
        replay.analysis.return_value = {'code': ANALYSIS_CODE.replace('.mean()', '.sum()')}
    with pytest.raises(ContractError):
        workflow.run_paper_step()
    assert not any(event['type'] == 'artifact' for event in replay.events)
    latest = json.loads((replay.source.parent / 'f4-trace/latest.json').read_text())
    trace = json.loads((replay.source.parent / 'f4-trace' / latest['summary']).read_text())
    assert trace['status'] == 'failed' and trace['error']
    assert replay.structured.call_count == (0 if fault == 'input' else 1)
    assert replay.analysis.call_count == (0 if fault == 'input' else 2)
    replay.source.write_bytes(original)
    replay.analysis.return_value = {'code': ANALYSIS_CODE}
    replay.structured.side_effect = [replay.plan, replay.conclusion]
    replay.context.params['retry_hint'] = 'Recovery request for this rerun only: 重试'
    assert workflow.run_paper_step()['status'] == 'completed'
    saved_path = next(event['value']['path'] for event in reversed(replay.events)
                      if event['type'] == 'artifact' and event['slot'] == 'data_analysis_pool')
    saved = workflow._read_bound(saved_path)
    assert saved['history'][-1]['request'] == replay.context.params['user_input']
    assert workflow.material(saved, 'analysis')['content']['findings'][0]['value'] == 2.


def test_chat_hands_f4_to_workflow_and_stops(monkeypatch):
    from lazyllm.tools.agent.functionCall import _tool_result_stop_text
    from lazymind.chat.workflow import workflow_manager

    client = Mock()
    client.get_ready_steps.return_value = {'state_version': 1, 'ready_steps': ['data_analysis']}
    client.advance.return_value = SimpleNamespace(result={'accepted': True, 'task_id': 'f4-task'})
    monkeypatch.setattr(workflow_manager, '_client', lambda: client)
    lazyllm.globals['agentic_config'] = {'query': '只执行 F4'}
    result = workflow_manager._handoff_tool('session')('data_analysis')
    request = client.advance.call_args.args[0]
    assert request.handoff and [step.step_id for step in request.steps] == ['data_analysis']
    assert result['task_id'] == 'f4-task' and _tool_result_stop_text(result)
    client.advance.assert_called_once()


def test_design_evidence_requires_source_references(replay):
    from lazymind.document_tools.paper.analysis.plan import validate_plan
    from lazymind.document_tools.paper.analysis.plan_bindings import evidence_catalog
    from lazymind.document_tools.paper.analysis.plan_repair import PlanRepairError, apply_updates
    from lazymind.document_tools.paper.analysis.schema import normalize_input

    value = json.loads(replay.source.read_text())
    quote = '每条观测是独立测量，按方法分别计算均值区间。'
    value['study_design'] = dict(description=quote)
    inputs = normalize_input(value)
    for job in replay.plan['jobs']:
        job.update(interval_level=.95, design_evidence=quote)
    with pytest.raises(PlanRepairError) as error:
        validate_plan(replay.plan, inputs)
    issues = error.value.issues
    assert {issue['field'] for issue in issues} == {'design_evidence_refs'}
    reference = next(item['id'] for item in evidence_catalog(inputs) if item['text'] == quote)
    repaired = apply_updates(error.value.plan, dict(updates=[
        dict(job_id=issue['job_id'], field=issue['field'], value=[reference]) for issue in issues]), issues)
    locked = validate_plan(repaired, inputs)
    assert all(job['design_evidence'] == quote for job in locked['jobs'])
    locked['jobs'][0]['design_evidence'] = '模型改写的原文不能成为依据。'
    assert validate_plan(locked, inputs)['jobs'][0]['design_evidence'] == quote


def test_skill_override_requires_the_actual_skill_root(tmp_path):
    from lazymind.document_tools.paper.plotting.skills import PlotSkills

    root = tmp_path / 'plot-from-data'
    shutil.copytree(PlotSkills().root, root)
    with pytest.raises(RuntimeError, match='缺少绘图 skill'):
        PlotSkills(str(tmp_path))
    assert 'bar_grouped_hatch' in PlotSkills(str(root)).catalog()


def test_json_attachment_binding_stays_in_analysis(tmp_path, monkeypatch):
    from lazymind.document_tools.paper.analysis import attachments, trace
    from lazymind.chat.engine.tools.file_resources import resolver
    from lazymind.chat.engine.tools import host_file_resolution

    source = tmp_path / 'data.json'
    value = {'topic': 'Analysis', 'data': {'columns': ['score'], 'column_types': ['number'],
             'units': [None], 'rows': [[1], [2], [3]]}}
    value.update(request='只分析附件中的得分',
                 papers=[{'id': 'SRC-001', 'title': '测试来源', 'text': '虚构测试材料'}],
                 evidence='[SRC-001] 测试证据')
    source.write_text(json.dumps(value))
    lazyllm.globals['agentic_config'] = {'history_files_per_turn': {'1': [str(source)]}}
    assert attachments.workflow_input_bindings({}, '根据上传附件写论文') == {}
    assert attachments.workflow_input_bindings({}, '只做数据分析，使用上传附件') == {
        'paper_input': str(source)}
    monkeypatch.setattr(resolver, 'resolve_text_target',
                        lambda *a, **kw: SimpleNamespace(kind='attachment_text', path=str(source)))
    monkeypatch.setattr(host_file_resolution, 'open_input_file', lambda path: open(path, 'rb'))
    monkeypatch.setattr(trace, 'save', Mock())
    reference = tmp_path / 'reference.txt'
    reference.write_text('data.json')
    bound = attachments.bind_attachment({'paper_input': str(reference)}, str(tmp_path), '分析数据')
    assert json.loads(Path(bound['paper_input']).read_text()) == value
    pool = workflow.initialize_pool(bound, '分析数据')
    assert workflow.material(pool, 'data')['content']['rows'] == [[1], [2], [3]]
    request = workflow._step_request('data_analysis', pool, {'user_input': '启动请求和实验授权问答'})
    assert request == value['request']
    writing = workflow.business_input('write_outline', pool, value['request'])
    assert writing['papers'] == value['papers']
    assert writing['evidence'] == value['evidence']
