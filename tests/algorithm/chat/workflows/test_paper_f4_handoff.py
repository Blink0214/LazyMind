"""Offline checks for passing F4 materials into writing, review and revision."""
import copy
import json
from unittest.mock import Mock

import pytest

from lazymind.document_tools.paper import api, contracts, peer_review, workflow, write_document
from lazymind.document_tools.paper.analysis.schema import binding, normalize_input


@pytest.fixture
def f4_result():
    inputs = normalize_input(dict(topic='方法得分', data=dict(
        columns=['score'], column_types=['number'], units=['point'], rows=[[2.]])))
    columns = [dict(table_id='data', column='score')]
    analysis = dict(
        **binding(inputs), summary='当前样本的平均得分为二分。', methods=['算术均值'],
        targets=[dict(id='mean', question='平均得分是多少？', estimand='均值', scope='全部观测', columns=columns)],
        findings=[dict(id='score', target_id='mean', kind='estimate', statement='平均得分', columns=columns,
                       calculation='mean(score)', value=2., unit='point',
                       sample_size=dict(source_rows=[dict(table_id='data', count=1)], n=1,
                                        unit='observation', excluded_n=0),
                       interval=None, test=None, probability=None, model_id=None)],
        models=[], diagnostics=[], limitations=['单次观测不能说明总体表现。'],
    )
    charts = [dict(id=f'mean_{i}', title=f'得分图 {i}', caption='样本平均得分。', columns=columns,
                   finding_ids=['score'], svg='<svg xmlns="http://www.w3.org/2000/svg"><text>2</text></svg>')
              for i in range(2)]
    return inputs, dict(analysis=analysis, charts=charts, is_demo=False)


@pytest.mark.parametrize('chart_count', [0, 2])
def test_f4_materials_reach_writing_review_and_revision(f4_result, monkeypatch, chart_count):
    inputs, output = f4_result
    output['charts'] = output['charts'][:chart_count]
    before = copy.deepcopy(output)
    monkeypatch.setattr(api.ALGORITHMS['data_analysis'], 'run', lambda _: copy.deepcopy(output))
    writing = Mock(wraps=write_document.run)
    monkeypatch.setattr(write_document, 'run', writing)
    monkeypatch.setattr(peer_review, 'AutoModel', lambda **_: object())
    monkeypatch.setattr(peer_review, 'WriterToolBase', lambda **_: object())
    review_call = Mock(return_value=dict(summary='结论需限定到当前样本。', issues=[], limitations=[]))
    monkeypatch.setattr(peer_review, 'call_once', review_call)

    f4_pool = workflow.execute_step('data_analysis', workflow.pool_from_business(inputs))['pool']
    f4_before = copy.deepcopy(f4_pool)
    written = workflow.execute_step('write_document', f4_pool)['pool']
    received = writing.call_args.args[0]
    assert received['analysis'] == output['analysis']
    assert received['charts'] == output['charts']
    for kind in ('analysis', 'charts'):
        assert f4_pool['current'][kind] in written['history'][-1]['inputs']

    reviewed = workflow.execute_step('peer_review', written)['pool']
    prompt = review_call.call_args.args[1]
    review_input = json.loads(prompt.split('Input JSON:\n', 1)[1])
    assert review_input['analysis'] == output['analysis']
    assert f4_pool['current']['analysis'] in reviewed['history'][-1]['inputs']

    workflow.execute_step('revise_document', reviewed)
    assert writing.call_args.args[0]['analysis'] == output['analysis']
    assert writing.call_args.args[0]['charts'] == output['charts']
    received['analysis']['findings'][0]['value'] = 999.
    assert f4_pool == f4_before
    assert output == before


@pytest.mark.parametrize('step', ['write_document', 'revise_document', 'peer_review'])
def test_downstream_rejects_malformed_analysis(f4_result, step):
    _, output = f4_result
    output['analysis']['findings'][0]['value'] = 'not a number'
    inputs = dict(topic='T', analysis=output['analysis'], manuscript='# 稿件')
    if step == 'revise_document':
        inputs['review'] = '# 审稿意见'
    with pytest.raises(ValueError, match='output.analysis'):
        contracts.validate_input(step, inputs)


def test_writing_rejects_chart_references_outside_analysis(f4_result):
    _, output = f4_result
    output['charts'][0]['finding_ids'] = ['unknown']
    with pytest.raises(ValueError, match='引用不存在'):
        contracts.validate_input('write_document', dict(topic='T', analysis=output['analysis'],
                                                        charts=output['charts']))


@pytest.mark.parametrize('step', ['write_document', 'revise_document', 'peer_review'])
def test_legacy_text_analysis_still_works(step):
    inputs = dict(topic='T', analysis='已有文字分析', manuscript='# 稿件')
    if step == 'revise_document':
        inputs['review'] = '# 审稿意见'
    if step != 'peer_review':
        inputs['chart'] = '<svg xmlns="http://www.w3.org/2000/svg"/>'
    normalized = contracts.validate_input(step, inputs)
    assert normalized['analysis'] == inputs['analysis']
    if step != 'peer_review':
        assert normalized['charts'] == []
        assert normalized['chart'] == inputs['chart']
