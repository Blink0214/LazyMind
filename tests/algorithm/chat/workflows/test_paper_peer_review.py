"""Real-model F6 integration, plus direct schema and report-rendering checks."""
import copy
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from lazymind.document_tools.paper import api, peer_review, workflow

EXAMPLE = Path(__file__).resolve().parents[4] / 'tests/fixtures/paper-writing/peer_review.json'


@pytest.fixture
def example():
    return json.loads(EXAMPLE.read_text())


def test_review_instructions_are_english_with_explicit_output_language():
    for prompt in (peer_review.REVIEW_PROMPT, peer_review.REPAIR_PROMPT):
        assert not re.search(r'[\u4e00-\u9fff]', prompt)
        assert 'Simplified Chinese unless the request' in prompt
    for schema in (peer_review.ReviewResult, peer_review.RepairedIssue, peer_review.ReviewRepair):
        assert not re.search(r'[\u4e00-\u9fff]', json.dumps(schema.model_json_schema(), ensure_ascii=False))


def test_real_model_review_preserves_input_and_output_contract(example):
    before = copy.deepcopy(example['input'])
    output = api.run_algorithm('peer_review', example['input'])
    assert example['input'] == before
    assert set(output) == {'review', 'review_data', 'is_demo'}
    assert output['is_demo'] is False
    assert all(section in output['review'] for section in ('## 总体评价', '## 逐条意见', '## 检查范围与限制'))
    assert '未提供分析材料' in output['review']
    assert output['review_data']['manuscript_sha256'] == peer_review.text_digest(before['manuscript'])


def test_real_review_reaches_revision_contract(example):
    pool = workflow.pool_from_business(example['input'])
    result = workflow.execute_step('peer_review', pool, '只检查结论是否超出证据')
    assert result['status'] == 'completed'
    assert result['mock'] is False
    result['pool']['selected_issue_ids'] = [
        issue['id'] for issue in workflow.material(result['pool'], 'review_data')['content']['issues']
    ]
    revision = workflow.business_input('revise_document', result['pool'], '')
    assert revision['manuscript'] == example['input']['manuscript']
    assert revision['review'] == workflow.material(result['pool'], 'review')['content']
    assert '## 逐条意见' in revision['review']
    assert revision['review_data'] == workflow.material(result['pool'], 'review_data')['content']
    api.contracts.validate_input('revise_document', revision)


def test_render_example_without_mutating_input(example):
    before = copy.deepcopy(example)
    result = peer_review.ReviewResult.model_validate(example['model_response'])
    assert peer_review.render_review(result, example['input']) == example['output']['review']
    assert peer_review.build_output(result, example['input']) == example['output']
    assert example == before


def test_priority_numbering_and_all_action_types(example):
    response = example['model_response']
    issue = response['issues'][0]
    response['issues'] = [
        dict(issue, severity='suggestion', action_type='text_revision'),
        dict(issue, severity='major', action_type='needs_experiment'),
        dict(issue, severity='minor', action_type='needs_material'),
    ]
    before = copy.deepcopy(response)
    result = peer_review.ReviewResult.model_validate(response)
    text = peer_review.render_review(result, example['input'])
    assert 'R1｜重要问题' in text and 'R2｜一般问题' in text and 'R3｜建议' in text
    assert all(label in text for label in peer_review.ACTION_LABELS.values())
    assert response == before


def test_no_findings_is_not_publication_approval():
    result = peer_review.ReviewResult(summary='未识别出明确问题。', issues=[], limitations=[])
    text = peer_review.render_review(result, {'topic': 'T', 'manuscript': '# 稿件\n正文'})
    assert '不代表论文已达到发表标准' in text
    assert '未提供文献证据材料' in text
    assert '未提供分析材料' in text


def test_missing_content_and_multiline_quote_rendering(example):
    issue = example['model_response']['issues'][0]
    issue.update(location='全文（缺少数据说明）', quote=None)
    result = peer_review.ReviewResult.model_validate(example['model_response'])
    assert '全文或缺失内容建议' in peer_review.render_review(result, example['input'])
    issue.update(location='方法', quote='## 方法\n比较两种证据组织方法。')
    result = peer_review.ReviewResult.model_validate(example['model_response'])
    assert '> ## 方法\n> 比较两种证据组织方法。' in peer_review.render_review(result, example['input'])


@pytest.mark.parametrize('change', [
    {'severity': 'critical'}, {'category': 'invented'}, {'action_type': 'auto_experiment'},
    {'location': ''}, {'problem': '  '}, {'suggestion': 42}, {'extra': 'unknown'},
])
def test_invalid_issue_schema_is_rejected(example, change):
    example['model_response']['issues'][0].update(change)
    with pytest.raises(ValidationError):
        peer_review.ReviewResult.model_validate(example['model_response'])


def test_empty_manuscript_is_rejected():
    with pytest.raises(ValueError):
        peer_review.run({'topic': 'T', 'manuscript': ' '})


def test_whitespace_only_quote_difference_restores_exact_source(example):
    result = peer_review.ReviewResult.model_validate(example['model_response'])
    result.issues[0].quote = '本文方法 20 16 90% 3.2'
    inputs = {**example['input'], 'manuscript': '结果：\n本文方法   20\t16  90%\n3.2\n结束'}
    before = result.model_dump()
    output = peer_review.build_output(result, inputs)
    quote = output['review_data']['issues'][0]['quote']
    assert quote == '本文方法   20\t16  90%\n3.2'
    assert quote in inputs['manuscript']
    assert result.model_dump() == before


@pytest.mark.parametrize('quote', ['本文方法 20 18 90% 3.2', '本文方法 2016 90% 3.2', '本文方法……3.2'])
def test_changed_numbers_and_stitched_quotes_are_not_accepted(example, quote):
    result = peer_review.ReviewResult.model_validate(example['model_response'])
    result.issues[0].quote = quote
    with pytest.raises(peer_review.ReviewQuoteError, match='论文已收到') as error:
        peer_review.build_output(result, {**example['input'], 'manuscript': '本文方法 20 16 90% 3.2'})
    assert quote in str(error.value)


@pytest.mark.parametrize('field', ['manuscript', 'review'])
def test_revision_rejects_stale_structured_review(example, field):
    result = peer_review.ReviewResult.model_validate(example['model_response'])
    output = peer_review.build_output(result, example['input'])
    inputs = {**example['input'], 'review': output['review'], 'review_data': output['review_data']}
    api.contracts.validate_input('revise_document', inputs)
    inputs[field] += '\n用户编辑'
    with pytest.raises(ValueError, match='已过期'):
        api.contracts.validate_input('revise_document', inputs)


def test_structured_ids_match_markdown_and_reject_duplicate_ids(example):
    issue = example['model_response']['issues'][0]
    result = peer_review.ReviewResult.model_validate({**example['model_response'], 'issues': [
        dict(issue, severity='suggestion'), dict(issue, severity='major'),
    ]})
    output = peer_review.build_output(result, example['input'])
    issues = output['review_data']['issues']
    assert [(i['id'], i['severity']) for i in issues] == [('R1', 'major'), ('R2', 'suggestion')]
    assert 'R1｜重要问题' in output['review'] and 'R2｜建议' in output['review']
    issues[1]['id'] = 'R1'
    with pytest.raises(ValueError, match='编号不能重复'):
        api.contracts.validate_output('peer_review', output)


def test_edited_report_from_pool_cannot_use_old_structured_advice(example):
    result = peer_review.ReviewResult.model_validate(example['model_response'])
    output = peer_review.build_output(result, example['input'])
    pool = workflow.pool_from_business({**example['input'], 'review': output['review'],
                                       'review_data': output['review_data']})
    pool['history'].append({'node': 'F6', 'request': '', 'inputs': [], 'outputs': []})
    pool['sequence'] = 1
    edited = workflow.pool_from_inputs({'peer_review_pool': pool,
                                       'peer_review_document': output['review'] + '\n编辑后的意见'})
    edited['selected_issue_ids'] = [output['review_data']['issues'][0]['id']]
    with pytest.raises(ValueError, match='已过期'):
        api.contracts.validate_input('revise_document', workflow.business_input('revise_document', edited))


def test_legacy_markdown_only_revision_requires_structured_review(example):
    # F5 now consumes the paired report/review_data produced by F6.
    with pytest.raises(api.contracts.MissingMaterials, match='结构化意见'):
        api.contracts.validate_input('revise_document', {
            **example['input'], 'review': example['output']['review'],
        })


def test_partial_repair_keeps_valid_issues_and_input_unchanged(example):
    issue = example['model_response']['issues'][0]
    candidate = {**example['model_response'], 'issues': [copy.deepcopy(issue), copy.deepcopy(issue)]}
    del candidate['issues'][1]['suggestion']
    candidate['issues'][1]['正文被误当字段'] = '错误值'
    before = copy.deepcopy(candidate)
    targets = peer_review.repair_targets(candidate, example['input']['manuscript'])
    assert set(targets) == {'issues.1'}
    repair = peer_review.ReviewRepair(replacements={
        'issues.1': dict(issue, quote={'start_line': 3, 'end_line': 3})})
    result = peer_review.apply_repair(candidate, targets, repair, example['input']['manuscript'])
    assert result.issues[0].model_dump() == candidate['issues'][0]
    assert result.issues[1].model_dump() == issue
    assert candidate == before
    peer_review.build_output(result, example['input'])


def test_quote_and_format_failures_share_one_repair_batch(example):
    issue = example['model_response']['issues'][0]
    candidate = {**example['model_response'], 'issues': [copy.deepcopy(issue), copy.deepcopy(issue)]}
    del candidate['issues'][0]['action_type']
    candidate['issues'][1]['quote'] = '不在原稿中的一句话'
    targets = peer_review.repair_targets(candidate, example['input']['manuscript'])
    assert set(targets) == {'issues.0', 'issues.1'}


@pytest.mark.parametrize('replacements', [{}, {'summary': '擅自改动有效字段'}, {'issues.0': {}}])
def test_repair_cannot_omit_targets_or_change_valid_fields(example, replacements):
    candidate = example['model_response']
    del candidate['issues'][0]['suggestion']
    targets = peer_review.repair_targets(candidate, example['input']['manuscript'])
    with pytest.raises(ValueError):
        peer_review.apply_repair(candidate, targets, peer_review.ReviewRepair(replacements=replacements),
                                 example['input']['manuscript'])


def test_summary_and_limitations_can_be_repaired_without_regenerating_issues(example):
    candidate = {**example['model_response'], 'summary': '', 'limitations': '不是数组'}
    targets = peer_review.repair_targets(candidate, example['input']['manuscript'])
    assert set(targets) == {'summary', 'limitations'}
    result = peer_review.apply_repair(candidate, targets, peer_review.ReviewRepair(
        replacements={'summary': '已有审查结果', 'limitations': []}), example['input']['manuscript'])
    assert [issue.model_dump() for issue in result.issues] == candidate['issues']


@pytest.mark.parametrize('candidate', [None, [], {}, {'issues': '坏列表'},
                                       {'summary': 'S', 'issues': [], 'limitations': [], 'unknown': 1}])
def test_broken_envelope_stops_instead_of_regenerating_review(candidate):
    with pytest.raises(ValueError, match='未自动重新审稿'):
        peer_review.repair_targets(candidate, '稿件')


def test_valid_result_needs_no_repair(example):
    assert peer_review.repair_targets(example['model_response'], example['input']['manuscript']) == {}


@pytest.mark.parametrize('start,end', [(0, 1), (3, 2), (1, 999), (True, 2)])
def test_repair_rejects_invalid_quote_lines(example, start, end):
    issue = example['model_response']['issues'][0]
    repair = peer_review.ReviewRepair(replacements={
        'issues.0': dict(issue, quote={'start_line': start, 'end_line': end})})
    with pytest.raises(ValueError):
        peer_review.apply_repair(example['model_response'], {'issues.0': {}}, repair,
                                 example['input']['manuscript'])


def test_repair_copies_source_lines_without_rewriting_whitespace(example):
    manuscript = '方法\r\n数据  16\t20\r\n准确率 90%\r\n'
    issue = example['model_response']['issues'][0]
    repair = peer_review.ReviewRepair(replacements={
        'issues.0': dict(issue, quote={'start_line': 2, 'end_line': 3})})
    result = peer_review.apply_repair(example['model_response'], {'issues.0': {}}, repair, manuscript)
    assert result.issues[0].quote == '数据  16\t20\r\n准确率 90%'
    assert result.issues[0].quote in manuscript


def test_repair_prompt_contains_only_bad_issues_and_numbered_source(example):
    candidate = copy.deepcopy(example['model_response'])
    candidate['issues'].append(dict(candidate['issues'][0], problem='只用于有效意见的标记'))
    del candidate['issues'][0]['suggestion']
    targets = peer_review.repair_targets(candidate, example['input']['manuscript'])
    prompt = peer_review.repair_prompt(example['input'], targets)
    assert '只用于有效意见的标记' not in prompt
    payload = json.loads(prompt.split('Input JSON:\n', 1)[1])
    assert 'manuscript' not in payload['inputs']
    assert payload['inputs']['manuscript_lines'][2] == {'line': 3, 'text': '比较两种证据组织方法。'}


def test_repeated_field_tag_does_not_split_or_expand_issues(example):
    candidate = example['model_response']
    raw = json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))
    damaged = raw.replace('"location":', '"category":"location":', 1)
    assert damaged != raw
    parsed = peer_review.parse_response(damaged)
    assert parsed == candidate
    assert peer_review.repair_targets(parsed, example['input']['manuscript']) == {}


def test_json_parser_preserves_quoted_manuscript_syntax(example):
    candidate = example['model_response']
    candidate['issues'][0]['quote'] = '"category":"analysis","category":"location":"方法"'
    raw = json.dumps(candidate, ensure_ascii=False)
    assert peer_review.parse_response('```json\n' + raw + '\n```') == candidate


@pytest.mark.parametrize('raw', [
    '{"summary":"A","summary":"B","issues":[],"limitations":[]}',
    '{"summary":"A","issues":[{"problem":"unfinished"}',
    '{"summary":"A","issues":[{"problem":"a"} {"suggestion":"b"}],"limitations":[]}',
])
def test_ambiguous_json_is_not_guessed_or_expanded(raw):
    with pytest.raises(ValueError):
        peer_review.parse_response(raw)


def test_large_repair_is_rejected_without_expanding_fragments(example):
    candidate = {**example['model_response'], 'issues': [
        {'problem': 'fragment'} for _ in range(peer_review.MAX_REPAIR_ISSUES + 1)]}
    with pytest.raises(ValueError, match='损坏范围过大'):
        peer_review.repair_targets(candidate, example['input']['manuscript'])


def test_repair_payload_limit_does_not_silently_truncate_source(example):
    inputs = {**example['input'], 'manuscript': '原文' * peer_review.MAX_REPAIR_CHARS}
    with pytest.raises(ValueError, match='超过限制'):
        peer_review.repair_prompt(inputs, {'summary': {'value': '', 'errors': []}})
