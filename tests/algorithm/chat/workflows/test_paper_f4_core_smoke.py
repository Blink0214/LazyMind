"""Compact offline F4 checks using the real generated-code executors."""
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
from lazymind.document_tools.paper.analysis.schema import ContractError
from lazymind.document_tools.paper.plotting import analysis as plots


ANALYSIS_CODE = """def analyze(tables, plan, rng):
    rows = tables['data']['rows']
    results = []
    for group in ('baseline', 'candidate'):
        values = [row[1] for row in rows if row[0] == group]
        results.append(dict(question_id='q1', label=group + ' mean', metric='score',
            method='Arithmetic mean', value=sum(values) / len(values), unit='point',
            columns=[dict(table_id='data', column='score')], scope='Observed group',
            coordinates={'method': group}, sample_size=dict(
                source_rows=[dict(table_id='data', count=len(values))],
                n=len(values), unit='observation', excluded_n=0)))
    return dict(results=results)
"""
# Annotation-only names must not require an additional runtime import.
PLOT_CODE = """import matplotlib.pyplot as plt

def draw(data, statistics, spec) -> matplotlib.figure.Figure:
    rows = data['analysis_results']
    fig, ax = plt.subplots()
    ax.bar([row['coordinates']['method'] for row in rows], [row['value'] for row in rows])
    ax.set_ylabel('Mean score (point)')
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
    request = '只执行 F4，分析上传的 input.json，输出分析、结论和图表。'
    inputs = dict(topic='模拟：方法得分比较', request=request,
                  data=dict(columns=['method', 'score'], column_types=['string', 'number'],
                            units=[None, 'point'], rows=[['baseline', 1.], ['baseline', 3.],
                                                       ['candidate', 4.], ['candidate', 6.]]))
    plan = dict(questions=['各组平均得分如何？'], outline='按方法计算得分均值，仅作模拟描述。')
    arrangement = dict(figures=[dict(title='Mean scores', purpose='Compare group means.',
                                    style='bar_grouped_hatch', result_indices=[0, 1])])
    conclusion = '候选组均值为 5 分，基线为 2 分；仅反映当前模拟样本。'
    structured = Mock(side_effect=[plan, arrangement])
    analysis = Mock(return_value={'code': ANALYSIS_CODE})
    plot = Mock(return_value={'code': PLOT_CODE, 'caption': '模拟样本的组均值。'})
    repair = Mock()
    monkeypatch.setattr(model, 'call_model', structured)
    monkeypatch.setattr(model, 'call_code_model', analysis)
    monkeypatch.setattr(model, 'call_markdown_model', Mock(return_value=conclusion))
    monkeypatch.setattr(plots, 'call_code_model', plot)
    monkeypatch.setattr(plots, 'call_model', repair)
    monkeypatch.setattr(lazyllm, 'AutoModel', Mock(side_effect=AssertionError('Unexpected live model call')))
    monkeypatch.setenv('LAZYMIND_PAPER_GENERATED_ANALYSIS_ATTEMPTS', '2')
    monkeypatch.setenv('LAZYMIND_PAPER_PLOT_ATTEMPTS', '2')
    monkeypatch.delenv('LAZYMIND_PAPER_PLOT_SKILLS', raising=False)
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(inputs), encoding='utf-8')
    lazyllm.globals['agentic_config'] = dict(user_id='f4-test', conversation_id='f4-test',
                                          query=request, files=[str(source)],
                                          history_files_per_turn={'1': [str(source)]})
    from lazymind.document_tools.paper.analysis.attachments import workflow_input_bindings
    events = []
    slots = ['data_analysis_pool', 'data_analysis_document', 'analysis_chart']
    context = SubAgentContext(
        task_id='f4-test', conversation_id='f4-test', agent_type='workflow_step', objective=request,
        params=dict(step_id='data_analysis', user_input=request,
                    remote_inputs=workflow_input_bindings({}, request),
                    output_slot_types=dict.fromkeys(slots, 'file')),
        workspace_path=str(tmp_path), input_slots=['paper_input'], output_slots=slots,
        db=None, emit=events.append)
    lazyllm.globals['subagent_ctx'] = context
    return SimpleNamespace(source=source, plan=plan, arrangement=arrangement, conclusion=conclusion,
                           structured=structured, analysis=analysis, plot=plot, repair=repair,
                           events=events, context=context)


@pytest.mark.parametrize('fault', ['none', 'plan', 'analysis', 'plot'])
def test_f4_workflow_publishes_executed_files(replay, fault):
    if fault == 'plan':
        replay.structured.side_effect = [{}, replay.plan, replay.arrangement]
    elif fault in ('analysis', 'plot'):
        source = ANALYSIS_CODE if fault == 'analysis' else PLOT_CODE
        statement = '    return dict(results=results)' if fault == 'analysis' else '    return fig'
        line = source.splitlines().index(statement) + 1
        replacement = dict(replacements=[dict(start_line=line, end_line=line, code=statement)])
        code = source.replace(statement, '    return missing_name')
        if fault == 'analysis':
            replay.analysis.return_value = {'code': code}
            replay.structured.side_effect = [replay.plan, replacement, replay.arrangement]
        else:
            replay.plot.return_value = {'code': code}
            replay.repair.return_value = replacement

    assert workflow.run_paper_step()['status'] == 'completed'
    artifacts = [event for event in replay.events if event['type'] == 'artifact']
    assert {event['slot'] for event in artifacts} == set(replay.context.output_slots)
    paths = {Path(event['value']['path']).suffix: Path(event['value']['path']) for event in artifacts}
    assert all(path.is_file() and path.stat().st_size for path in paths.values())
    pool = workflow._read_bound(str(paths['.json']))
    analysis = workflow.material(pool, 'analysis')['content']
    assert [row['value'] for row in analysis['findings']] == pytest.approx([2., 5.])
    assert analysis['summary'] == replay.conclusion
    assert replay.conclusion in paths['.md'].read_text()
    charts = workflow.material(pool, 'charts')['content']
    assert len(charts) == 1 and charts[0]['title'] == 'Mean scores'
    assert charts[0]['finding_ids'] == [row['id'] for row in analysis['findings']]
    execution = pool['history'][-1]['analysis_execution'][0]
    assert not execution['figures'][0].get('fallback')
    assert '<svg' in paths['.svg'].read_text()
    assert paths['.png'].read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert paths['.pdf'].read_bytes().startswith(b'%PDF-')
    assert replay.structured.call_count == 2 + (fault in ('plan', 'analysis'))
    assert replay.analysis.call_count == replay.plot.call_count == 1
    assert replay.repair.call_count == (fault == 'plot')


def test_invalid_input_is_not_published(replay):
    inputs = json.loads(replay.source.read_text())
    inputs['data']['rows'][0].pop()
    replay.source.write_text(json.dumps(inputs))
    with pytest.raises(ContractError):
        workflow.run_paper_step()
    assert not any(event['type'] == 'artifact' for event in replay.events)
    replay.structured.assert_not_called()


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
