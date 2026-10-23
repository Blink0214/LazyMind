"""Paper Writer material adaptation, drafting and scoped revision regressions."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lazyllm.tools.writer.data_models.task import WritingTask
from lazymind.document_tools.paper import contracts, peer_review
from lazymind.document_tools.paper.writing import common as document, revise_document as revision_entry
from lazymind.document_tools.paper.review_schema import text_digest
from lazymind.document_tools.toolkits import WriterRevisionToolkit


OUTLINE = '# 方法比较\n\n## 方法\n\n## 结果\n\n## 局限'
BODY = '仅比较单次得分。新方法得分高 15 分；不能推断统计显著性。'


def test_review_location_numbering_and_quote_disambiguation():
    from lazymind.document_tools.paper.writing.common import resolve_review_target
    body = '# 论文\n\n## 2 方法\n\n### 2.1 参数\n第一种。\n\n## 3 补充\n\n### 3.1 参数\n第二种。'
    issue = {'location': '3.1 参数', 'quote': '第二种。'}
    assert resolve_review_target(issue, body)['heading_path'] == ['论文', '3 补充', '3.1 参数']
    issue.update(location='参数', quote=None)
    with pytest.raises(ValueError, match='ambiguous'):
        resolve_review_target(issue, body)
    issue.update(location='不存在', quote='第一种。')
    with pytest.raises(ValueError, match='missing'):
        resolve_review_target(issue, body)
    issue.update(location='2 方法', quote='第二种。')
    with pytest.raises(ValueError):
        resolve_review_target(issue, body)


@pytest.mark.parametrize('language,target', [('zh-CN', None), ('zh-CN', 1000), ('en', 1000)])
def test_outline_relative_weights_do_not_leak_as_character_instructions(language, target):
    descriptions = ['讨论60个问题与0.7权重（约3字）。', 'Discuss latency (about 2 characters).']
    outline = '# T\n\n' + '\n\n'.join(
        f'<a id="block-sec-{i}"></a>\n## {title}\n<!-- writer:outline '
        + json.dumps({'node_id': f'sec-{i}', 'target_chars': weight, 'outline_description': description})
        + ' -->'
        for i, (title, weight, description) in enumerate(zip(['方法', '结果'], [3, 2], descriptions), 1))
    plan = {'instructions': [
        {'instruction_id': str(i), 'content_ref': {'heading_path': ['T', title]},
         'section_title': title, 'section_goal': description,
         'required_points': [description, '原始记录：60个问题，延迟128ms。'],
         'expected_blocks': [description], 'fact_constraints': ['用户引语：“约3字”是界面提示。'],
         'meta': {'target_chars': weight, 'max_chars': weight + 1},
         'heading_structure': [{'node_id': f'child-{i}', 'level': 2, 'title': '细节', 'target_chars': 1}]}
        for i, (title, weight, description) in enumerate(zip(['方法', '结果'], [3, 2], descriptions), 1)]}
    before = copy.deepcopy(plan)
    result = document._prepare_sections(plan, outline, {'paper_language': language, 'word_target': target})
    assert plan == before
    for i, item in enumerate(result['instructions']):
        assert item['section_goal'] == ['讨论60个问题与0.7权重。', 'Discuss latency.'][i]
        assert item['expected_blocks'] == [item['section_goal']]
        assert item['required_points'][:2] == [item['section_goal'], '原始记录：60个问题，延迟128ms。']
        assert item['fact_constraints'][0] == '用户引语：“约3字”是界面提示。'
        assert 'target_chars' not in item['heading_structure'][0]
        if target is None:
            assert 'target_chars' not in item['meta'] and 'word_target' not in item['meta']
            assert document.SECTION_NO_TARGET_INSTRUCTIONS in item['required_points']
        else:
            assert item['meta']['word_target'] == [600, 400][i]
            assert ('target_chars' in item['meta']) == (language == 'zh-CN')


@pytest.mark.parametrize('marker, error', [
    ('', 'every supplied'),
    ('<!-- paper-charts: ["chart-999"] -->', 'unknown'),
    ('<!-- paper-charts: ["chart-001", "chart-001"] -->', 'more than once'),
    ('<!-- paper-charts: ["chart-001"] -->', None),
])
def test_chart_ownership_is_unique_and_subsection_maps_to_parent(marker, error):
    outline = '# 论文\n## 方法\n## 结果\n### 命中率\n' + marker + '\n## 讨论\n'
    plan = {'instructions': [
        {'content_ref': {'heading_path': ['论文', title]}, 'meta': {}}
        for title in ['方法', '结果', '讨论']
    ]}
    prepared = {'charts': [{'id': 'chart-001', 'title': '命中率', 'columns': [], 'finding_ids': [], 'caption': '合成数据'}]}
    if error:
        with pytest.raises(ValueError, match=error):
            document._bind_chart_references(plan, prepared, outline)
        return
    document._bind_chart_references(plan, prepared, outline)
    assert [item['meta']['cross_references'][0]['must_create'] for item in plan['instructions']] == [False, True, False]
    assert '命中率' in plan['instructions'][1]['meta']['cross_references'][0]['guidance']


def test_chart_ownership_survives_public_writer_outline_normalization():
    from lazyllm.tools.writer.tools.planning_tools import WriterPlanningTools
    source = '# 论文\n## 结果\n<!-- paper-charts: ["chart-001"] -->\n'
    task = WritingTask(task_id='ownership', query='write', task_type='write')
    normalized = WriterPlanningTools._complete_markdown_outline_fields(
        WriterPlanningTools._normalize_markdown_outline(source), task,
    )
    assert document._outline_chart_owners(normalized, [{'id': 'chart-001'}]) == {
        'chart-001': (['论文', '结果'], 1),
    }


# Current upstream input contracts and preparation.
@pytest.fixture
def revision():
    manuscript = '# 论文\n\n## 方法\n\n来源[SRC-001]。'
    reviewed = peer_review.build_output(peer_review.ReviewResult.model_validate(dict(
        summary='修改表述', limitations=[], issues=[dict(severity='minor', category='language',
        location='方法', quote='来源[SRC-001]。', problem='表述不清', suggestion='澄清', action_type='text_revision')]
    )), dict(topic='论文', manuscript=manuscript, evidence=None, analysis=None, request=''))
    return dict(topic='论文', manuscript=manuscript, selected_issue_ids=['R1'],
                papers=[dict(id='SRC-001', title='来源', text='来源正文', citation='来源文献[Z].')],
                review=reviewed['review'], review_data=reviewed['review_data'])


def test_revision_preserves_reviewed_source_and_quote_before_hash_check(revision):
    before = copy.deepcopy(revision)
    normalized = contracts.validate_input('revise_document', revision)
    document.validate_inputs(normalized)
    assert normalized['manuscript'] == revision['manuscript']
    assert text_digest(normalized['manuscript']) == revision['review_data']['manuscript_sha256']
    assert normalized['review_data']['issues'][0]['quote'] in normalized['manuscript']
    assert revision == before


def test_direct_revision_normalizes_explicit_source_ids(revision):
    normalized = contracts.validate_input('revise_document', {
        key: revision[key] for key in ('topic', 'manuscript', 'papers')})
    assert '[文献:SRC-001]' in normalized['manuscript']


@pytest.mark.parametrize('change', ['object_review', 'missing_review', 'missing_review_data'])
def test_revision_requires_current_f6_pair(revision, change):
    if change == 'object_review':
        revision['review'] = revision.pop('review_data')
    else:
        revision.pop(change.removeprefix('missing_'))
    with pytest.raises(ValueError):
        contracts.validate_input('revise_document', revision)


@pytest.mark.parametrize('field,value', [
    ('question', {'candidates': []}), ('analysis', '旧分析文本'),
    ('analysis', {'summary': '不完整的分析'}), ('chart', '<svg/>'),
    ('charts', [dict(id='fig1', title='图', svg='<svg/>', columns=[], finding_ids=[], description='旧图注')]),
])
def test_current_f5_contract_rejects_other_shapes(field, value):
    with pytest.raises(ValueError):
        contracts.validate_input('write_document', dict(topic='论文', manuscript='# 论文\n\n## 方法', **{field: value}))


@pytest.mark.parametrize('mode', ['direct', 'review'])
def test_existing_manuscript_does_not_validate_input_outline_structure(revision, mode):
    source = revision if mode == 'review' else {key: revision[key] for key in ('topic', 'manuscript', 'papers')}
    source = dict(source, outline='过期大纲，仅供背景参考', request='修改方法')
    normalized = contracts.validate_input('revise_document', source)
    document.validate_inputs(normalized)


def test_new_draft_requires_valid_input_outline():
    inputs = contracts.validate_input('write_document', dict(topic='论文', outline='没有标题结构'))
    with pytest.raises(ValueError):
        document.validate_inputs(inputs)


def test_review_location_is_resolved_once_during_preparation(revision, monkeypatch):
    normalized = contracts.validate_input('revise_document', revision)
    resolve = document.resolve_review_target
    calls = []
    def record(issue, source):
        calls.append(issue['id'])
        return resolve(issue, source)
    monkeypatch.setattr(document, 'resolve_review_target', record)
    document.validate_inputs(normalized)
    prepared = document.prepare_writer_context(normalized)
    assert calls == ['R1']
    assert prepared['selected_issues'][0]['target']['heading_path'] == ['论文', '方法']


def test_invalid_review_location_still_fails_before_building_writer_context(revision):
    normalized = contracts.validate_input('revise_document', revision)
    normalized['review_data']['issues'][0]['location'] = '不存在'
    toolkit = Mock()
    with pytest.raises(ValueError, match='missing or ambiguous'):
        document.prepare_writer_context(normalized, toolkit=toolkit)
    toolkit.build_writing_task.assert_not_called()


def test_chart_directory_reuses_one_index_and_keeps_exact_associations(monkeypatch):
    source = contracts.validate_input('write_document', {'topic': '论文', 'question': '比较'})
    source['analysis'] = {'findings': [{'id': 'F1', 'statement': '结论一'}, {'id': 'F2', 'statement': '结论二'}]}
    source['charts'] = [dict(id=f'Fig{i}', title=f'图{i}', caption='说明', columns=[],
                             finding_ids=refs, svg='<svg/>')
                        for i, refs in enumerate([['F2'], ['F1', 'F2']], 1)]
    before = copy.deepcopy(source)
    indexes = []
    original = document._chart_directory
    def record(chart, findings):
        indexes.append(findings)
        return original(chart, findings)
    monkeypatch.setattr(document, '_chart_directory', record)
    prepared = document.prepare_writer_inputs(source)
    assert indexes[0] is indexes[1]
    assert [[f['id'] for f in c['findings']] for c in prepared['chart_directory']] == [['F2'], ['F1', 'F2']]
    assert source == before


# Scoped revision through the public Writer toolkit.
SOURCE = '# 论文\n\n## 方法\n\n原方法。\n\n## 结果\n\n原结果。\n\n## 局限\n\n保留原局限。'


def instruction(section='方法', ids=None, **overrides):
    return dict(content_ref={'heading_path': ['论文', section]}, modify_type='update',
                target_scope='section', instruction='按意见修改',
                meta={'issue_ids': ['R1'] if ids is None else ids}, **overrides)


def replacement(old='原方法。', new='修订方法。', section='方法'):
    return dict(old_string=old, new_string=new, content_ref={'heading_path': ['论文', section]})


def prepared(issues=None):
    issues = issues if issues is not None else [('R1', '方法', 'text_revision')]
    return dict(manuscript=SOURCE, outline='# 论文\n\n## 方法\n\n## 结果\n\n## 局限',
                writing_task={'query': '只修改选中意见', 'meta': {'mode': 'revise', 'review_driven': True}},
                writing_context={'context_id': 'scope-test'}, charts=[], reference_candidates=[],
                selected_issues=[dict(id=key, action_type=action,
                    target={'heading_path': ['论文', section], 'occurrence': 1, 'document_root': False})
                    for key, section, action in issues])


def editor(operations, changes=()):
    return SimpleNamespace(
        generate_modify_plan=Mock(return_value=json.dumps({'scope': 'document', 'instructions': operations})),
        generate_string_replace_set=Mock(return_value=json.dumps({'replacements': changes})),
        apply_string_replace=Mock(wraps=WriterRevisionToolkit().apply_string_replace),
    )


def test_replacements_reject_ambiguous_original_and_accept_explicit_section():
    source = SOURCE.replace('原结果。', '原方法。')
    item = replacement()
    item['content_ref'] = {'document_root': True}
    with pytest.raises(ValueError, match='exactly once'):
        revision_entry._bounded_replacements(source, [(0, len(source))], {'replacements': [item]})
    item['content_ref'] = {'heading_path': ['论文', '方法']}
    _, located = revision_entry._bounded_replacements(source, [(0, len(source))], {'replacements': [item]})
    assert source[located[0][0]:located[0][1]] == '原方法。'


def test_replacements_cannot_escape_planned_scope():
    allowed = [document.markdown_section_range(SOURCE, ['论文', '方法'], 1)]
    with pytest.raises(ValueError, match='exceeds the planned'):
        revision_entry._bounded_replacements(SOURCE, allowed, {
            'replacements': [replacement('原结果。', '篡改结果。', '结果')]})


def test_replacements_reject_overlap():
    with pytest.raises(ValueError, match='overlapping'):
        revision_entry._bounded_replacements(SOURCE, [(0, len(SOURCE))], {'replacements': [
            replacement(), replacement('方法。', '新方法。')]})


def test_reverse_application_preserves_child_references_when_title_changes():
    changes, _ = revision_entry._bounded_replacements(SOURCE, [(0, len(SOURCE))], {'replacements': [
        dict(old_string='# 论文', new_string='# 新论文', content_ref={'heading_path': ['论文']}),
        replacement(), replacement('原结果。', '修订结果。', '结果')]})
    result = json.loads(WriterRevisionToolkit().apply_string_replace(
        markdown_document=SOURCE, string_replace_set_json=json.dumps(changes),
        writing_context_json='{"context_id":"scope-test"}'))
    assert result['string_replace_result']['success']
    assert result['revised_document'] == SOURCE.replace('原方法。', '修订方法。').replace(
        '原结果。', '修订结果。').replace('# 论文', '# 新论文')


@pytest.mark.parametrize('ids', [[], ['R2'], [1]])
def test_review_plan_requires_selected_issue_ids(ids):
    data = prepared()
    with pytest.raises(ValueError, match='selected issue_ids'):
        revision_entry._review_plan(SOURCE, copy.deepcopy(data['writing_task']), data,
                              editor([instruction(ids=ids)]), '{}')


def test_review_plan_rejects_unrelated_issue_association():
    data = prepared([('R1', '方法', 'text_revision'), ('R2', '结果', 'text_revision')])
    with pytest.raises(ValueError, match='unrelated operation'):
        revision_entry._review_plan(SOURCE, copy.deepcopy(data['writing_task']), data,
                              editor([instruction(ids=['R1', 'R2'])]), '{}')


def test_review_plan_requires_every_selected_text_revision():
    data = prepared([('R1', '方法', 'text_revision'), ('R2', '结果', 'text_revision')])
    with pytest.raises(ValueError, match='does not cover all'):
        revision_entry._review_plan(SOURCE, copy.deepcopy(data['writing_task']), data,
                              editor([instruction()]), '{}')


def test_move_destination_must_also_be_authorized():
    data = prepared()
    operation = instruction()
    operation.update(modify_type='move', destination_ref={'heading_path': ['论文', '结果']},
                     destination_scope='section', position='after')
    with pytest.raises(ValueError, match='exceeds selected issue target'):
        revision_entry._review_plan(SOURCE, copy.deepcopy(data['writing_task']), data, editor([operation]), '{}')
    data['selected_issues'].append(prepared([('R2', '结果', 'text_revision')])['selected_issues'][0])
    operation['meta']['issue_ids'].append('R2')
    _, ranges, _ = revision_entry._review_plan(SOURCE, copy.deepcopy(data['writing_task']), data, editor([operation]), '{}')
    assert [SOURCE[start:end].strip() for start, end in ranges[0]] == ['## 方法\n\n原方法。', '## 结果\n\n原结果。']


def test_each_text_issue_requires_an_effective_replacement_before_application():
    data = prepared([('R1', '方法', 'text_revision'), ('R2', '结果', 'text_revision')])
    tools = editor([instruction(), instruction('结果', ['R2'])], [replacement()])
    with pytest.raises(ValueError, match='no effective edit.*R2'):
        revision_entry._modify_manuscript(data, {'word_target': None}, Mock(), editor=tools)
    tools.apply_string_replace.assert_not_called()


def test_invalid_generated_edit_is_rejected_before_application():
    tools = editor([instruction()], [replacement('原结果。', '篡改结果。', '结果')])
    with pytest.raises(ValueError, match='exceeds the planned'):
        revision_entry._modify_manuscript(prepared(), {'word_target': None}, Mock(), editor=tools)
    tools.apply_string_replace.assert_not_called()


@pytest.mark.parametrize('word_target', [None, 10000])
def test_selected_revision_preserves_unselected_sections_and_inputs(word_target):
    data = prepared([('R1', '方法', 'text_revision'), ('R2', '结果', 'needs_experiment')])
    before = copy.deepcopy(data)
    tools = editor([instruction()], [replacement()])
    quality = Mock()
    result = revision_entry._modify_manuscript(data, {'word_target': word_target, 'paper_language': 'zh-CN'}, quality, editor=tools)
    assert result == dict(manuscript=SOURCE.replace('原方法。', '修订方法。'), is_demo=False)
    assert data == before
    tools.apply_string_replace.assert_called_once()
    quality.check_consistency.assert_not_called()


@pytest.mark.parametrize('mode', ['direct', 'review'])
@pytest.mark.parametrize('old,new,outline', [
    ('## 方法', '## 研究方法', None),
    ('原方法。', '原方法。\n\n### 参数\n\n补充说明。', '旧大纲说明，不再约束当前稿件'),
    ('## 方法\n\n原方法。\n\n', '', None),
    ('# 论文', '# 新论文', '旧大纲说明，不再约束当前稿件'),
])
def test_edits_only_update_manuscript_without_enforcing_old_outline(mode, outline, old, new):
    data = prepared()
    data['outline'] = outline
    data['writing_task']['meta']['mode'] = 'revise'
    data['writing_task']['meta']['review_driven'] = mode == 'review'
    operation = instruction()
    operation.update(target_scope='fragment', locator_text=old)
    change = replacement(old, new)
    if old == '# 论文':
        operation['content_ref'] = change['content_ref'] = {'heading_path': ['论文']}
        data['selected_issues'][0]['target'] = {'document_root': True}
    tools = editor([operation], [change])
    tools.locate_revision_target = Mock(return_value='{}')
    quality = Mock()
    quality.check_consistency.return_value = json.dumps({
        'review_report': {'result': {'is_passed': True, 'score': 100, 'summary': '通过'}}})
    result = revision_entry._modify_manuscript(data, {'word_target': None}, quality, editor=tools)
    expected = SOURCE.replace(old, new)
    assert result['manuscript'] == expected
    assert 'outline' not in result
    assert data['outline'] == outline
    document.validate_result(result, dict(mode='revise', manuscript=SOURCE, papers=[], charts=[], outline=outline))
    quality.check_consistency.assert_not_called()


def test_revision_result_does_not_require_output_outline():
    inputs = dict(mode='revise', manuscript=SOURCE, papers=[], charts=[], outline=None)
    document.validate_result(dict(manuscript=SOURCE), inputs)


def test_new_draft_still_checks_planned_headings():
    with pytest.raises(ValueError, match='headings do not match'):
        document._validate_manuscript_structure(
            SOURCE.replace('## 方法', '## 研究方法'), signature=document._heading_signature(SOURCE))



def test_inline_trace_failure_preserves_business_result_and_error(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('trace unavailable')
    for name in ('get_trace_context', 'start_span', 'set_span_attributes', 'set_span_error', 'finish_span'):
        monkeypatch.setattr(document.tracing, name, broken)
    assert document._trace_call('prepare', lambda: 'result') == 'result'
    with pytest.raises(ValueError, match='business failure'):
        document._trace_call('assemble', lambda: (_ for _ in ()).throw(ValueError('business failure')))


@pytest.mark.parametrize('mode', ['direct', 'review'])
@pytest.mark.parametrize('failure', [None, 'scope', 'validation'])
def test_completed_revision_streams_full_text_once_after_validation(monkeypatch, mode, failure):
    from lazymind.document_tools.paper.runtime_events import bind_runtime_events
    data = prepared()
    data['writing_task']['meta']['mode'] = 'revise'
    data['writing_task']['meta']['review_driven'] = mode == 'review'
    changes = [replacement('原结果。', '越界。', '结果')] if failure == 'scope' else [replacement()]
    tools = editor([instruction()], changes)
    tools.locate_revision_target = Mock(return_value='{}')
    emitted = []
    expected = SOURCE.replace('原方法。', '修订方法。')
    def validate(body, *args, **kwargs):
        assert body == expected
        assert not any(e['type'].startswith('artifact_stream') for e in emitted)
        if failure == 'validation':
            raise ValueError('invalid candidate')
    monkeypatch.setattr(revision_entry, '_validate_manuscript', validate)
    slot = 'revise_document_document'
    def run():
        with bind_runtime_events(emitted.append, slot) as events:
            result = revision_entry._modify_manuscript(data, {'word_target': None}, Mock(), editor=tools)
            assert ''.join(e['delta'] for e in emitted if 'delta' in e) == expected
            assert not any(e['type'] == 'artifact_stream_end' for e in emitted)
            events.complete()
            return result
    if failure:
        with pytest.raises(ValueError):
            run()
        assert not any(e['type'].startswith('artifact_stream') for e in emitted)
    else:
        assert run()['manuscript'] == expected
        assert len([e for e in emitted if e['type'] == 'artifact_stream_start']) == 1
        assert not any(e['type'] == 'artifact_stream_abort' for e in emitted)
        assert any(e['type'] == 'artifact_stream_end' for e in emitted)
    assert all(e['slot'] == slot for e in emitted if 'slot' in e)
    assert data['manuscript'] == SOURCE


def test_new_draft_still_enforces_full_manuscript_length():
    with pytest.raises(ValueError, match='outside target'):
        document._validate_manuscript(SOURCE, None, [],
            {'word_target': 10000, 'paper_language': 'zh-CN'}, Mock(), '{}', candidates=[])


@pytest.mark.parametrize('action', ['needs_material', 'needs_experiment'])
def test_review_without_executable_edits_stops_before_replacement(action):
    data = prepared([('R1', '方法', action)])
    tools = Mock()
    tools.generate_modify_plan.side_effect = ValueError(
        'modify_plan contains no operations for the located revision scope.')
    with pytest.raises(contracts.MissingMaterials, match='需要补充材料或实验'):
        revision_entry._modify_manuscript(data, {'word_target': None}, Mock(), editor=tools)
    tools.generate_string_replace_set.assert_not_called()
    tools.apply_string_replace.assert_not_called()
