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


def test_format_repair_contains_only_bad_issues_without_manuscript(example):
    candidate = copy.deepcopy(example['model_response'])
    candidate['issues'].append(dict(candidate['issues'][0], problem='只用于有效意见的标记'))
    del candidate['issues'][0]['suggestion']
    targets = peer_review.repair_targets(candidate, example['input']['manuscript'])
    prompt = peer_review.repair_prompt(example['input'], targets)
    assert '只用于有效意见的标记' not in prompt
    payload = json.loads(prompt.split('Input JSON:\n', 1)[1])
    assert 'manuscript' not in payload['inputs']
    assert 'manuscript_lines' not in payload['inputs']
    assert payload['quote_mode'] == 'preserve'


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


def test_multiple_invalid_issues_are_independent_targets(example):
    candidate = copy.deepcopy(example['model_response'])
    candidate['issues'] = [dict(candidate['issues'][0], severity='invalid') for _ in range(6)]
    assert len(peer_review.repair_targets(candidate, example['input']['manuscript'])) == 6


def test_repair_payload_limit_does_not_silently_truncate_source(example):
    inputs = {**example['input'], 'request': '审查要求' * peer_review.MAX_REPAIR_CHARS}
    with pytest.raises(ValueError, match='超过限制'):
        peer_review.repair_prompt(inputs, {'summary': {'value': '', 'errors': []}})


def test_quote_repair_does_not_resend_large_analysis_or_figures(example):
    inputs = copy.deepcopy(example['input'])
    inputs.update(analysis={'report': 'analysis-only-marker' * peer_review.MAX_REPAIR_CHARS},
                  charts=[{'svg': 'figure-only-marker' * peer_review.MAX_REPAIR_CHARS}])
    before = copy.deepcopy(inputs)
    candidate = copy.deepcopy(example['model_response'])
    candidate['issues'][0]['quote'] = 'An inexact quotation'
    targets = peer_review.repair_targets(candidate, inputs['manuscript'])
    prompt = peer_review.repair_prompt(inputs, targets)
    payload = json.loads(prompt.split('Input JSON:\n', 1)[1])
    assert set(payload['inputs']) == {'topic', 'request', 'manuscript_lines'}
    for line in payload['inputs']['manuscript_lines'].splitlines():
        number, text = line.split(': ', 1)
        assert inputs['manuscript'].splitlines()[int(number) - 1] == text
    assert payload['targets'] == targets
    assert 'analysis-only-marker' not in prompt and 'figure-only-marker' not in prompt
    assert inputs == before


def test_long_manuscript_quote_repair_only_sends_related_source(example):
    manuscript = '# 背景\n' + '\n\n'.join('无关背景材料。' * 25 for _ in range(300))
    manuscript += '\n\n## 方法\n比较两种证据组织方法。\n\n## 附录\n不应发送的附录。'
    inputs = {**example['input'], 'manuscript': manuscript}
    issue = dict(example['model_response']['issues'][0], quote='比较三种证据组织方法。')
    prompt, allowed = peer_review.repair_request(inputs, {'issues.0': {
        'value': issue, 'errors': ['quote not found']}})
    payload = json.loads(prompt.split('Input JSON:\n', 1)[1])
    assert '比较两种证据组织方法。' in payload['inputs']['manuscript_lines']
    assert '无关背景材料' not in prompt and '不应发送的附录' not in prompt
    assert len(manuscript) > peer_review.MAX_REPAIR_CHARS
    assert allowed
    assert len(prompt) < peer_review.MAX_REPAIR_CHARS


@pytest.mark.parametrize('quote', [None, {'start_line': 1, 'end_line': 3},
                                  {'start_line': 2, 'end_line': 4}, '编造的引用'])
def test_local_repair_cannot_evade_source_checks(example, quote):
    issue = example['model_response']['issues'][0]
    target = {'value': dict(issue, quote='未匹配的原文'), 'errors': ['quote not found']}
    with pytest.raises(ValueError):
        peer_review.apply_repair(example['model_response'], {'issues.0': target},
                                 peer_review.ReviewRepair(replacements={'issues.0': dict(issue, quote=quote)}),
                                 example['input']['manuscript'], allowed_lines={1, 3, 5})


def test_format_repair_preserves_original_quote(example):
    issue = example['model_response']['issues'][0]
    target = {'value': dict(issue, severity='invalid'),
              'errors': [{'field': ['issues', 0, 'severity'], 'message': 'invalid severity'}]}
    result = peer_review.apply_repair(example['model_response'], {'issues.0': target},
                                     peer_review.ReviewRepair(replacements={'issues.0': issue}),
                                     example['input']['manuscript'], allowed_lines=set())
    assert result.issues[0].model_dump() == issue


def test_repair_runs_separate_batches_and_preserves_valid_issues(example, monkeypatch):
    candidate = copy.deepcopy(example['model_response'])
    issue = candidate['issues'][0]
    candidate['issues'] = [copy.deepcopy(issue), dict(issue, severity='invalid'),
                           dict(issue, quote='比较三种证据组织方法。')]
    before = copy.deepcopy(candidate)
    calls = []

    def repair(_reviewer, prompt, schema, trace_id):
        payload = json.loads(prompt.split('Input JSON:\n')[1])
        calls.append(payload)
        key, = payload['targets']
        value = dict(issue)
        if payload['quote_mode'] == 'source_lines':
            assert 'manuscript_lines' in payload['inputs']
            value['quote'] = {'start_line': 3, 'end_line': 3}
        else:
            assert 'manuscript_lines' not in payload['inputs']
        return {'replacements': {key: value}}

    monkeypatch.setattr(peer_review, 'call_once', repair)
    targets = peer_review.repair_targets(candidate, example['input']['manuscript'])
    result = peer_review.repair_result(None, example['input'], candidate, targets, 'offline')
    assert len(calls) == 2 and all(len(p['targets']) == 1 for p in calls)
    assert [i.model_dump() for i in result.issues] == [issue] * 3
    assert candidate == before
    peer_review.build_output(result, example['input'])


def test_unverifiable_issue_is_disclosed_and_excluded_from_auto_revision(example, monkeypatch):
    candidate = copy.deepcopy(example['model_response'])
    candidate['issues'].append(dict(candidate['issues'][0], location='不存在的章节', quote='xyzzzz'))
    before = copy.deepcopy(candidate)
    monkeypatch.setattr(peer_review, 'call_once', lambda *a: pytest.fail('No source, no model call'))
    result = peer_review.repair_result(None, example['input'], candidate,
                                      peer_review.repair_targets(candidate, example['input']['manuscript']), 'offline')
    output = peer_review.build_output(result, example['input'])
    assert len(output['review_data']['issues']) == 1
    assert '待核验意见' in output['review'] and 'xyzzzz' in output['review']
    assert output['review_data']['issues'][0]['quote'] == candidate['issues'][0]['quote']
    assert candidate == before


def test_bad_quote_repair_keeps_valid_review_and_cannot_publish_fake_quote(example, monkeypatch):
    candidate = copy.deepcopy(example['model_response'])
    issue = candidate['issues'][0]
    candidate['issues'].append(dict(issue, quote='比较三种证据组织方法。'))
    monkeypatch.setattr(peer_review, 'call_once', lambda *a: {
        'replacements': {'issues.1': dict(issue, quote=None)}})
    result = peer_review.repair_result(None, example['input'], candidate,
                                      peer_review.repair_targets(candidate, example['input']['manuscript']), 'offline')
    assert len(result.issues) == 1
    assert any('不能将需要核验的原文引用改为 null' in text for text in result.limitations)


def test_quote_repair_cannot_change_supported_judgment(example):
    issue = example['model_response']['issues'][0]
    target = {'value': dict(issue, quote='比较三种证据组织方法。'), 'errors': ['quote not found']}
    replacement = dict(issue, problem='擅自替换原来的判断', quote={'start_line': 3, 'end_line': 3})
    with pytest.raises(ValueError, match='已经有效的字段'):
        peer_review.apply_repair(example['model_response'], {'issues.0': target},
                                 peer_review.ReviewRepair(replacements={'issues.0': replacement}),
                                 example['input']['manuscript'], {3})


def test_full_review_repairs_long_manuscript_in_bounded_batches(example, monkeypatch):
    issue = example['model_response']['issues'][0]
    manuscript = '# 背景\n' + ('无关内容。' * 100 + '\n\n') * 150
    manuscript += '\n## 方法\n' + issue['quote'] + '\n'
    line_number = manuscript.splitlines().index(issue['quote']) + 1
    inputs = {**example['input'], 'manuscript': manuscript}
    candidate = copy.deepcopy(example['model_response'])
    candidate['issues'] = [copy.deepcopy(issue)] + [dict(issue, quote='比较三种证据组织方法。') for _ in range(6)]
    before = copy.deepcopy(candidate)
    calls = []

    def respond(_reviewer, prompt, schema, trace_id):
        calls.append(schema)
        if schema is peer_review.ReviewResult:
            return copy.deepcopy(candidate)
        assert len(prompt) < peer_review.MAX_REPAIR_CHARS
        payload = json.loads(prompt.split('Input JSON:\n')[1])
        assert '无关内容' not in prompt
        key, = payload['targets']
        return {'replacements': {key: dict(issue, quote={'start_line': line_number, 'end_line': line_number})}}

    monkeypatch.setattr(peer_review, 'AutoModel', lambda **kw: object())
    monkeypatch.setattr(peer_review, 'WriterToolBase', lambda **kw: object())
    monkeypatch.setattr(peer_review, 'call_once', respond)
    output = peer_review.run(inputs)
    assert calls == [peer_review.ReviewResult] + [peer_review.ReviewRepair] * 6
    assert len(output['review_data']['issues']) == 7
    assert all(item['quote'] == issue['quote'] for item in output['review_data']['issues'])
    assert output['review_data']['manuscript_sha256'] == peer_review.text_digest(manuscript)
    assert candidate == before
    api.contracts.validate_output('peer_review', output)


def test_parse_failure_logs_metadata_without_response_content(caplog, monkeypatch):
    import logging
    monkeypatch.setattr(peer_review, 'LOG', logging.getLogger(__name__))
    from unittest.mock import Mock
    reviewer = Mock()
    reviewer._build_structured_llm.return_value = lambda prompt: '{"summary":"PRIVATE_MANUSCRIPT",'
    with pytest.raises(ValueError, match='无法解析的 JSON'):
        peer_review.call_once(reviewer, 'PRIVATE_PROMPT', peer_review.ReviewResult)
    assert 'paper_review parse_failure' in caplog.text
    assert 'position' in caplog.text and 'sha256' in caplog.text
    assert 'PRIVATE_MANUSCRIPT' not in caplog.text
    assert 'PRIVATE_PROMPT' not in caplog.text


def test_model_transport_failure_records_stage_without_payload(monkeypatch, caplog):
    import logging
    from unittest.mock import Mock
    monkeypatch.setattr(peer_review, 'LOG', logging.getLogger(__name__))
    caplog.set_level(logging.INFO)
    model = Mock(side_effect=TimeoutError('PRIVATE_PROVIDER_DETAIL'))
    reviewer = Mock()
    reviewer._build_structured_llm.return_value = model
    with pytest.raises(TimeoutError):
        peer_review.call_once(reviewer, 'PRIVATE_PROMPT', peer_review.ReviewResult, 'review-test')
    events = [json.loads(record.message.split(' ', 2)[2]) for record in caplog.records]
    assert any(event.get('stage') == 'model_response' and event.get('cause_types') == ['TimeoutError']
               and event['trace_id'] == 'review-test' for event in events)
    assert not any(event.get('stage') == 'json_parse' for event in events)
    assert 'PRIVATE_' not in caplog.text


def test_silent_review_stream_distinguishes_runtime_and_content(monkeypatch, caplog):
    import logging
    from unittest.mock import Mock
    monkeypatch.setattr(peer_review, 'LOG', logging.getLogger(__name__))
    caplog.set_level(logging.INFO)
    reviewer = Mock()

    def build(_prompt, *, stream_output, apply_formatter):
        assert apply_formatter is False
        sink = stream_output['_stream_sink']
        def model(_input):
            sink({'tag': 'runtime_event', 'runtime_event': {
                'type': 'model_call_started', 'data': {'model_call_id': 'provider-test', 'secret': 'PRIVATE'}}})
            sink({'tag': 'think', 'delta': 'PRIVATE_REASONING'})
            sink({'tag': 'text', 'delta': 'PRIVATE_CONTENT'})
            return '{"summary":"ok"}'
        return model

    reviewer._build_structured_llm.side_effect = build
    assert peer_review.call_once(reviewer, 'PRIVATE_PROMPT', peer_review.ReviewResult) == {'summary': 'ok'}
    assert 'model_runtime' in caplog.text and 'provider-test' in caplog.text
    assert caplog.text.count('model_first_delta') == 1
    assert '"delta_type": "think"' in caplog.text
    assert 'PRIVATE' not in caplog.text


@pytest.mark.parametrize('content', ['{"summary":"ok"}', '```json\n{"summary":"ok"}\n```'])
def test_parse_review_reuses_writer_thinking_cleanup(content):
    assert peer_review.parse_response('<think>not JSON { PRIVATE_REASONING</think>\n' + content) == {'summary': 'ok'}


def test_unclosed_thinking_or_duplicate_json_is_still_rejected():
    with pytest.raises(ValueError):
        peer_review.parse_response('<think>unfinished {"summary":"ok"}')
    with pytest.raises(ValueError, match='重复字段'):
        peer_review.parse_response('<think>reasoning</think>{"summary":"a","summary":"b"}')
