"""F4 requirements stay independent of Writer's launch-context merging."""
import copy
import json
from pathlib import Path

import pytest

from lazymind.document_tools.paper import workflow
from lazymind.document_tools.paper.analysis import model
from lazymind.document_tools.paper.analysis.schema import normalize_input


def materials():
    return dict(topic='模拟数据分析', request='比较得分，只解释当前样本。',
                data=dict(columns=['score'], column_types=['number'], units=[None], rows=[[1], [2]]),
                papers=[dict(id='SRC-001', title='测试来源', text='虚构测试材料。')],
                evidence='[SRC-001] 虚构证据。')


@pytest.mark.parametrize('bindings', [{}, {'data_analysis_pool': 'saved.json'}])
@pytest.mark.parametrize('cold, current', [(True, 'launch'), (False, 'launch'), (False, ''),
                                         (True, '原始启动消息，尚未附加实验授权问答')])
def test_f4_keeps_material_request_out_of_launch_and_authorization_context(bindings, cold, current, monkeypatch):
    value = materials()
    pool = workflow.pool_from_business(value)
    before = copy.deepcopy(pool)
    launch = '启动论文写作。Clarification answers: 不允许，仅生成代码和模拟数据'
    params = dict(step_id='data_analysis', user_input=launch, launch_user_input=launch,
                  current_user_input=launch if current == 'launch' else current,
                  is_cold_start=cold, remote_inputs=bindings)
    request = workflow._step_request('data_analysis', pool, params)
    inputs = normalize_input(workflow.business_input('data_analysis', pool, request))
    assert inputs['request'] == value['request']
    prompts = []
    monkeypatch.setattr(model, 'call_model', lambda prompt: prompts.append(prompt))
    model.make_plan(inputs)
    rendered = json.dumps(prompts[0], ensure_ascii=False)
    assert 'Clarification answers:' not in rendered
    assert '本轮补充要求' not in rendered
    payload = json.loads(prompts[0]['input'][0]['content'].split('INPUT_JSON:\n', 1)[1])
    assert payload['facts']['request'] == value['request']
    assert pool == before
    writing = workflow.business_input('write_outline', pool, value['request'])
    assert writing['papers'] == value['papers']
    assert writing['evidence'] == value['evidence']


@pytest.mark.parametrize('params, expected', [
    ({'current_user_input': '只比较准确率'}, '比较得分，只解释当前样本。'),
    ({'retry_hint': 'Recovery request for this rerun only: 只比较耗时'}, '只比较耗时'),
    ({'current_user_input': '重试'}, '比较得分，只解释当前样本。'),
    ({'retry_hint': 'Recovery request for this rerun only: 重试'}, '上次 F4 要求'),
    ({}, '比较得分，只解释当前样本。'),
])
def test_f4_explicit_changes_and_retries_keep_baseline_precedence(params, expected):
    pool = workflow.pool_from_business(materials())
    pool['history'] = [{'node': 'F4', 'request': '上次 F4 要求'}, {'node': 'F5', 'request': '写作要求'}]
    params = dict(user_input='启动问答', launch_user_input='启动要求', **params)
    assert workflow._step_request('data_analysis', pool, params) == expected


def test_f4_without_material_requirements_falls_back_to_launch():
    pool = workflow.pool_from_business({**materials(), 'request': ''})
    assert workflow._step_request('data_analysis', pool, {'user_input': '分析得分'}) == '分析得分'
    assert workflow._step_request('data_analysis', pool, {
        'user_input': '重试', 'launch_user_input': '分析得分'}) == '分析得分'


def test_f5_keeps_its_independent_request_merging():
    pool = workflow.pool_from_business(materials())
    request = workflow._step_request('write_outline', pool, {'current_user_input': '正文用英文'})
    assert pool['request'] in request
    assert '本轮补充要求（优先于上述要求）：\n正文用英文' in request


def test_full_chain_attachment_keeps_request_and_writer_materials(tmp_path):
    path = tmp_path / 'paper-input.json'
    value = materials()
    path.write_text(json.dumps(value))
    launch = '请按照附件中的要求，从数据分析开始，继续完成论文写作和审稿。'
    params = dict(step_id='data_analysis', user_input=launch, launch_user_input=launch,
                  current_user_input=launch, is_cold_start=True)
    pool = workflow.initialize_pool({'paper_input': str(path)}, launch)
    request = workflow._step_request('data_analysis', pool, params)
    assert normalize_input(workflow.business_input('data_analysis', pool, request))['request'] == value['request']
    writer = workflow.business_input('write_outline', pool, '')
    assert writer['papers'] == value['papers']
    assert writer['evidence'] == value['evidence']


@pytest.mark.parametrize('current', ['请采用审稿意见R1、R3修订论文，不处理未选中的意见，保留无关内容。', ''])
def test_f5_revision_uses_current_turn_without_launch_fallback(current):
    pool = workflow.pool_from_business(materials())
    params = dict(step_id='revise_document', user_input='原始启动和实验授权问答',
                  launch_user_input='原始启动和实验授权问答', current_user_input=current)
    assert workflow._step_request('revise_document', pool, params, {'peer_review_pool': 'saved.json'}) == current


@pytest.mark.parametrize('raw', ['', {'role': 'assistant', 'content': '', 'reasoning_content': 'thinking only'}])
def test_response_budget_rejects_missing_body(raw):
    from lazymind.document_tools.paper.plotting.model import _bounded_response, PlotModelError
    with pytest.raises(PlotModelError):
        _bounded_response(raw)


def test_response_budget_separates_reasoning_from_final_body(monkeypatch):
    from lazymind.document_tools.paper.plotting import model as plotting_model
    monkeypatch.setattr(plotting_model, 'MAX_BODY_CHARS', 10)
    raw = {'role': 'assistant', 'content': 'result', 'reasoning_content': 'thinking ' * 100}
    result = plotting_model._bounded_response(raw)
    assert result.body == 'result' and result.reasoning == raw['reasoning_content']
    with pytest.raises(plotting_model.ModelBodyTooLong):
        plotting_model._bounded_response({'content': 'x' * 11})
    monkeypatch.setattr(plotting_model, 'MAX_RAW_RESPONSE_CHARS', 10)
    with pytest.raises(plotting_model.PlotModelError):
        plotting_model._bounded_response(raw)


def test_response_budget_preserves_structured_body():
    from lazymind.document_tools.paper.plotting.model import _bounded_response
    raw = {'code': 'print(1)'}
    assert _bounded_response(raw).body == raw
