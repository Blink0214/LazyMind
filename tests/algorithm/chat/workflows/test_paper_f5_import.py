"""F5 upload boundaries and Writer consumption; no model/network execution."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lazymind.document_tools.toolkits import WriterCreateToolkit as ContextToolkit

from lazymind.document_tools.paper import contracts, workflow
from lazymind.document_tools.paper.publication import (
    publish_citations, validate_citations,
)
from lazymind.document_tools.paper.writing import attachments, common, write_outline
from lazymind.document_tools.paper.writing.attachments import (
    append_note, extract_references, initialize_upload, reference_catalog, source_catalog,
)

NOTE = '# 研究材料\n背景[SRC-a]。\n\n## 参考文献\n[SRC-a] Author. Title[R]. 2025.\n'
REPORT = ('# 模拟审稿 · Test\n\n## 总体评价\n需澄清。\n\n## 逐条意见\n\n### R1｜重要问题｜方法\n位置：方法\n\n原文：\n> 原方法。'
          '\n\n问题与依据：不明确。\n\n修改建议：澄清。\n\n处理方式：可直接修改文字'
          '\n\n## 检查范围与限制\n- 只检查文本。\n')
MANUSCRIPT = '# Test\n\n## 方法\n原方法。\n\n## 结果\n结果保持。'


def normalized(value, step='write_outline', request='write'):
    pool = initialize_upload(value, request)
    return contracts.validate_input(step, workflow.business_input(step, pool, request))


def toolkit():
    return SimpleNamespace(build_writing_task=lambda **kw: json.dumps({'query': kw['query'], 'constraints': {}}),
                           build_resources=lambda **kw: kw['input_resources_json'])


def test_f2_sources_and_published_manuscript_roundtrip():
    refs = extract_references(NOTE)
    assert refs == [{'id': 'SRC-a', 'citation': 'Author. Title[R]. 2025.'}]
    published = publish_citations('# T\n引用[文献:SRC-a]。', refs)
    assert extract_references(published) == refs
    assert extract_references(NOTE + '\n# 第二份\n' + NOTE) == refs
    validate_citations(published, refs, published=True)


def test_bibliography_ignores_code_and_links():
    text = ('```md\n[SRC-code] fake\n```\n![SRC-img](file.svg)\n'
            '[SRC-link](https://example.org)\n[SRC-def]: https://example.org\n')
    assert extract_references(text) == []
    assert extract_references('[SRC-x] 正文而非书目\n随后继续正文。') == []


def test_conflicting_reference_and_unresolved_source_fail():
    with pytest.raises(ValueError, match='冲突'):
        extract_references(NOTE + '\n## References\n[SRC-a] Different citation.\n')
    with pytest.raises(ValueError, match='冲突'):
        reference_catalog([{'id': 'SRC-a', 'citation': 'A'}], [{'id': 'SRC-a', 'citation': 'B'}])


def test_plain_markdown_reaches_writer_without_fake_papers():
    source = normalized({'topic': 'T', 'writing_materials': NOTE})
    prepared = common.prepare_writer_inputs(source, toolkit=toolkit())
    assert source['papers'] == [] and source['analysis'] is None
    assert prepared['reference_candidates'] == extract_references(NOTE)
    resources = {x['resource_id']: json.loads(x['inline_text']) for x in prepared['resources']}
    assert resources['write_document:writing_materials'] == NOTE
    assert resources['write_document:reference_candidates'] == extract_references(NOTE)


@pytest.mark.parametrize('step', ['write_outline', 'write_document', 'revise_document'])
def test_new_pool_preserves_all_current_values_without_old_routing(step):
    old = workflow.pool_from_business({'topic': 'T', 'writing_materials': NOTE, 'question': 'Q', 'evidence': NOTE,
                                      'outline': '# T\n## 方法', 'manuscript': MANUSCRIPT})
    workflow.add_material(old, 'question', 'Newest Q', [old['current']['question']], ['SRC-a'], is_demo=True)
    old['history'] = [{'node': 'F1', 'inputs': [], 'outputs': [old['current']['question']]}]
    old['sequence'] = 1
    old['selected_issue_ids'] = ['R9']
    before = copy.deepcopy(old)
    fresh = initialize_upload(old, 'current request')
    assert old == before and fresh['sequence'] == 0 and fresh['history'] == []
    assert fresh['selected_issue_ids'] == [] and fresh['request'] == 'current request'
    assert workflow.material(fresh, 'question')['content'] == 'Newest Q'
    assert workflow.material(fresh, 'question')['mock'] is True
    assert workflow.material(fresh, 'question')['source_refs'] == ['SRC-a']
    assert fresh['materials'] == old['materials']
    assert fresh['current'] == old['current']
    assert workflow.material(fresh, 'manuscript')['content'] == MANUSCRIPT
    # Project the requested step, not the old F1 history. Preconditions run afterwards.
    value = workflow.business_input(step, fresh, 'current request')
    assert value['writing_materials'] == NOTE


def test_pool_f3_material_is_preserved_but_not_passed_to_writer():
    experiment = {'executed': False, 'plan': '比较两个方法', 'report': '模拟数据，未运行。',
                  'results': {'columns': ['method', 'score'], 'rows': [['A', 80], ['B', 85]]}}
    pool = initialize_upload({'topic': 'T', 'question': 'Q', 'experiment': experiment}, 'write')
    assert workflow.material(pool, 'experiment')['content'] == experiment
    source = contracts.validate_input('write_outline', workflow.business_input('write_outline', pool, 'write'))
    assert source['writing_materials'] is None and 'experiment' not in source
    prepared = common.prepare_writer_inputs(source, toolkit=toolkit())
    assert '比较两个方法' not in json.dumps(prepared['resources'], ensure_ascii=False)


def test_json_runtime_binding_rebases_pool_only_for_f5(tmp_path):
    old = workflow.pool_from_business({'topic': 'T', 'question': 'Q'})
    path = tmp_path / 'pool.json'
    path.write_text(json.dumps(old))
    result = workflow.initialize_pool({'paper_input': str(path)}, 'write', writing_import=True)
    assert result['history'] == []
    with pytest.raises(ValueError, match='paper_input'):
        workflow.initialize_pool({'paper_input': str(path)}, 'write')


@pytest.mark.parametrize('step,user_request', [
    ('write_outline', '写大纲'), ('write_document', '按上传大纲写正文'),
    ('revise_document', '按上传审稿修改'), ('revise_document', '只改引言，不参考审稿'),
])
def test_raw_documents_reach_writer_without_classification(monkeypatch, step, user_request):
    import lazyllm
    monkeypatch.setattr(lazyllm, 'AutoModel', Mock(side_effect=AssertionError('No import classifier')))
    value = {'topic': 'T', 'manuscript': MANUSCRIPT}
    append_note(value, 'combined.md', NOTE + '\n' + REPORT)
    source = normalized(value, step, user_request)
    prepared = common.prepare_writer_inputs(source, toolkit=toolkit())
    resources = {x['resource_id']: json.loads(x['inline_text']) for x in prepared['resources']}
    assert NOTE + '\n' + REPORT in resources['write_document:writing_materials']
    assert source['review_data'] is None and prepared['selected_issues'] == []
    assert user_request in prepared['writing_task']['query']
    assert 'references' not in source and 'references' not in contracts.KINDS


@pytest.mark.parametrize('suffix', ['.pdf', '.docx', '.md', '.txt'])
def test_documents_use_common_text_resolver(tmp_path, monkeypatch, suffix):
    from lazymind.chat.engine.tools.file_resources import resolver
    text = tmp_path / 'parsed.txt'
    text.write_text(NOTE + '\n' + REPORT)
    resolve = Mock(return_value=SimpleNamespace(path=str(text), kind='attachment_document'))
    monkeypatch.setattr(resolver, 'resolve_text_target', resolve)
    source = attachments.import_files(['uploaded' + suffix], 'write')
    resolve.assert_called_once_with('uploaded' + suffix, resources_only=True)
    assert NOTE + '\n' + REPORT.strip() in source['writing_materials']
    assert set(source) == {'topic', 'request', 'writing_materials'}


def test_json_review_digests_are_preserved_and_not_repaired():
    from lazymind.document_tools.paper.peer_review import build_output
    from lazymind.document_tools.paper.review_schema import ReviewResult
    output = build_output(ReviewResult(summary='OK', issues=[], limitations=[]),
                          {'topic': 'T', 'manuscript': MANUSCRIPT})
    value = {'topic': 'T', 'manuscript': MANUSCRIPT, 'review': output['review'], 'review_data': output['review_data']}
    fresh = initialize_upload(value, 'edit')
    assert workflow.material(fresh, 'review_data')['content'] == output['review_data']
    source = {**value, 'manuscript': MANUSCRIPT + '\nchanged'}
    with pytest.raises(ValueError, match='STALE_MATERIAL'):
        contracts.validate_input('revise_document', source)


def test_actual_json_attachment_is_read_not_transcribed(tmp_path, monkeypatch):
    import lazyllm
    from lazymind.chat.engine.tools.file_resources import resolver
    from lazymind.chat.engine.tools import host_file_resolution
    uploaded = tmp_path / 'pool.json'
    uploaded.write_text(json.dumps(workflow.pool_from_business({'topic': 'T', 'question': 'Q'})))
    monkeypatch.setattr(lazyllm, 'globals', {'agentic_config': {'files': [str(uploaded)]}})
    monkeypatch.setattr(resolver, 'resolve_text_target',
                        lambda *a, **kw: SimpleNamespace(path=str(uploaded), kind='attachment_text'))
    monkeypatch.setattr(host_file_resolution, 'open_input_file', lambda p: open(p, 'rb'))
    bound = attachments.bind_attachment({}, str(tmp_path), '用上传的 pool 写论文')
    fresh = workflow.initialize_pool(bound, 'current', writing_import=True)
    assert workflow.material(fresh, 'question')['content'] == 'Q'
    assert fresh['request'] == 'current'
    pinned = {'write_outline_pool': 'current.json'}
    assert attachments.bind_attachment(pinned, str(tmp_path), 'anything') == pinned


def test_multiple_json_requires_selection(tmp_path, monkeypatch):
    import lazyllm
    monkeypatch.setattr(lazyllm, 'globals', {'agentic_config': {'files': ['a.json', 'b.json']}})
    with pytest.raises(contracts.MissingMaterials, match='多份'):
        attachments.bind_attachment({}, str(tmp_path), 'write')


def test_unknown_json_fields_and_pool_schema_rejected():
    with pytest.raises(ValueError, match='未知字段'):
        initialize_upload({'topic': 'T', 'mystery': 1}, 'write')
    with pytest.raises(ValueError, match='schema'):
        initialize_upload({'topic': 'T', 'schema': 'unknown'}, 'write')


def test_json_plus_raw_review_and_bound_manuscript(tmp_path, monkeypatch):
    import lazyllm
    from lazymind.chat.engine.tools.file_resources import resolver
    from lazymind.document_tools.paper.writing.attachments import merge_upload
    pool = initialize_upload({'topic': 'T', 'manuscript': MANUSCRIPT}, '按审稿修改')
    extra = {'topic': 'ignored', 'request': 'ignored'}
    append_note(extra, 'review.md', REPORT)
    merged = merge_upload(pool, extra)
    assert workflow.material(merged, 'manuscript')['content'] == MANUSCRIPT
    assert REPORT in workflow.material(merged, 'writing_materials')['content']
    assert merged['history'] == []
    # The usual Agent manuscript binding must not hide the co-uploaded report.
    report_path = tmp_path / 'review.md'
    report_path.write_text(REPORT)
    binding = tmp_path / 'manuscript_file.txt'
    binding.write_text('draft.md')
    monkeypatch.setattr(lazyllm, 'globals', {'agentic_config': {'files': ['draft.md', str(report_path)]}})
    monkeypatch.setattr(resolver, 'resolve_text_target', lambda p, **kw: SimpleNamespace(path=p, kind='attachment_text'))
    monkeypatch.setattr(workflow, 'read_manuscript', lambda p: MANUSCRIPT)
    bound = attachments.bind_attachment({'manuscript_file': str(binding)}, str(tmp_path), '按上传审稿修改')
    fresh = workflow.initialize_pool(bound, '按上传审稿修改', writing_import=True)
    assert workflow.material(fresh, 'manuscript')['content'] == MANUSCRIPT
    assert REPORT.strip() in workflow.material(fresh, 'writing_materials')['content']
    assert workflow.material(fresh, 'review_data') is None


def test_fixture_markdown_and_svg_are_consumable(tmp_path, monkeypatch):
    from lazymind.chat.engine.tools.file_resources import resolver
    from lazymind.chat.engine.tools import host_file_resolution
    from lazymind.document_tools.paper.publication import figure_catalog
    root = Path(__file__).resolve().parents[4] / 'tests/fixtures/paper-writing/f5-import'
    monkeypatch.setenv('LAZYMIND_UPLOAD_ROOT', str(tmp_path))
    monkeypatch.setattr(resolver, 'resolve_text_target', lambda p, **kw: SimpleNamespace(path=p, kind='attachment_text'))
    monkeypatch.setattr(host_file_resolution, 'open_input_file', lambda p: open(p, 'rb'))
    files = ['f5-research-materials.md', 'chart-rates.svg', 'chart-deltas.svg']
    value = attachments.import_files([str(root / x) for x in files], 'write')
    source = normalized(value)
    assert len(source_catalog(source)) == 3 and source['papers'] == []
    assert len(figure_catalog(source['charts'], source['media_assets'])) == 2
    prepared = common.prepare_writer_inputs(source, toolkit=toolkit())
    assert len(prepared['reference_candidates']) == 3 and len(prepared['charts']) == 2


def test_import_all_fixture_structured_materials_and_writer_context():
    root = Path(__file__).resolve().parents[4] / 'tests/fixtures/paper-writing/f5-import'
    value = json.loads((root / 'f5-paper-input.json').read_text())
    source = normalized(value)
    assert len(source['analysis']['findings']) == 15 and len(source['charts']) == 2
    prepared = common.prepare_writer_inputs(source, toolkit=toolkit())
    assert len(prepared['reference_candidates']) == 3
    assert len(prepared['charts']) == 2


def test_raw_reference_material_context_has_no_task_query_or_document_summary():
    from lazymind.document_tools.toolkits import WriterCreateToolkit

    value = {'topic': 'T'}
    append_note(value, 'reference.md', NOTE)
    source = normalized(value, 'write_outline', '按参考材料写大纲')
    prepared = common.prepare_writer_context(source, toolkit=WriterCreateToolkit())
    context = prepared['writing_context']
    assert 'query' not in context
    assert context.get('document_summary') is None
    facts = {fact['key']: fact['value'] for fact in context['facts']}
    assert NOTE in json.loads(facts['write_document:writing_materials'])
    assert '按参考材料写大纲' in prepared['writing_task']['query']


def test_revision_task_preserves_current_request_and_selected_issues():
    prepared = {'writing_task': {'query': 'Edit only the methods.', 'meta': {'review_driven': True}},
                'selected_issues': [{'id': 'R2', 'text': 'Clarify the existing method'}]}
    original = copy.deepcopy(prepared)
    task = common.stage_task(prepared, 'revision_content')
    assert 'Edit only the methods.' in task['query']
    assert 'Clarify the existing method' in task['query']
    assert prepared == original


def test_catalog_without_papers_survives_review_and_revision_entry():
    refs = [{'id': 'SRC-a', 'citation': 'Author. Title. 2025.'}]
    body = publish_citations(MANUSCRIPT + '\nEvidence[文献:SRC-a].', refs)
    pool = initialize_upload({'topic': 'T', 'manuscript': body}, 'review')
    for step in ('peer_review', 'revise_document'):
        inputs = workflow.business_input(step, pool, 'Clarify methods')
        contracts.validate_input(step, inputs)
        assert inputs['manuscript'] == body
    assert workflow.material(pool, 'papers') is None


def test_material_bundle_keeps_multiple_managed_bibliographies_and_full_text():
    value = {'topic': 'T'}
    documents = []
    for identifier in ('SRC-a', 'SRC-b'):
        text = publish_citations('# Source\nClaim[文献:' + identifier + '].',
                                 [{'id': identifier, 'citation': identifier + ' citation'}])
        documents.append(text)
        append_note(value, identifier + '.md', text)
    append_note(value, 'advice.md', '# Advice\nClarify methods.')
    assert [item['id'] for item in source_catalog(value)] == ['SRC-a', 'SRC-b']
    assert all(text in value['writing_materials'] for text in documents)
    # The relaxed bundle parser must not relax the manuscript publication contract.
    with pytest.raises(ValueError, match='参考文献区域'):
        extract_references(value['writing_materials'])


@pytest.mark.parametrize('text', [
    '[SRC-a] A paragraph at EOF',
    '# Notes\n[SRC-a] A paragraph\n\n## Next\nOther text',
    '# Why references matter\n[SRC-a] A paragraph',
])
def test_prose_is_not_promoted_to_a_citable_source(text):
    assert extract_references(text) == []


def test_truncated_document_is_rejected_at_import(tmp_path, monkeypatch):
    from lazymind.chat.engine.tools.file_resources import resolver
    parsed = tmp_path / 'parsed.txt'
    parsed.write_text('Partial source\n[Attachment truncated after 200000 characters.]')
    monkeypatch.setattr(resolver, 'resolve_text_target',
                        lambda *a, **kw: SimpleNamespace(path=str(parsed)))
    with pytest.raises(contracts.MissingMaterials, match='截断'):
        attachments.import_files(['long.docx'], 'write')


def test_pptx_is_rejected_before_the_text_reader(monkeypatch):
    from lazymind.chat.engine.tools.file_resources import resolver
    reader = Mock(side_effect=AssertionError('PPTX cannot be read as full text'))
    monkeypatch.setattr(resolver, 'resolve_text_target', reader)
    with pytest.raises(contracts.MissingMaterials, match='不支持'):
        attachments.import_files(['slides.pptx'], 'write')
    assert attachments.select_files({'files': ['slides.pptx']}) == []
    reader.assert_not_called()


def test_explicit_outline_binding_preserves_structure_and_avoids_duplicate_material(tmp_path, monkeypatch):
    import lazyllm
    from lazymind.document_tools.paper.writing import write_document
    outline = '# Confirmed title\n\n## Method\nExplain limitations.\n\n## Results'
    path = tmp_path / 'outline.md'
    path.write_text(outline)
    monkeypatch.setattr(lazyllm, 'globals', {'agentic_config': {'files': [str(path)]}})
    bound = attachments.bind_attachment({'outline_file': str(path)}, str(tmp_path), '按确认的大纲写正文')
    pool = workflow.initialize_pool(bound, '按确认的大纲写正文', writing_import=True)
    source = contracts.validate_input('write_document', workflow.business_input('write_document', pool, 'write'))
    write_document.validate_inputs(source)
    assert source['outline'] == outline and source['writing_materials'] is None
    prepared = common.prepare_writer_inputs(source)
    assert write_outline.prepare_outline(prepared, ContextToolkit()) == outline


def test_explicit_outline_conflict_requires_one_version(tmp_path):
    uploaded = tmp_path / 'outline.md'
    uploaded.write_text('# New\n\n## Method')
    business = tmp_path / 'input.json'
    business.write_text(json.dumps({'topic': 'T', 'outline': '# Old\n\n## Method'}))
    with pytest.raises(ValueError, match='冲突'):
        workflow.initialize_pool({'outline_file': str(uploaded), 'paper_input': str(business)},
                                 'write', writing_import=True)


def test_import_normalization_keeps_original_material_version():
    pool = workflow.pool_from_business({'topic': 'T', 'writing_materials': NOTE,
                                       'manuscript': MANUSCRIPT + '\nEvidence[SRC-a].'})
    original = copy.deepcopy(workflow.material(pool, 'manuscript'))
    imported = initialize_upload(pool, 'edit')
    current = workflow.material(imported, 'manuscript')
    assert original in imported['materials']
    assert current['derived_from'] == [pool['current']['manuscript']]
    assert current['version'] > original['version']
    assert imported['history'] == [] and imported['selected_issue_ids'] == []
    assert workflow.material(pool, 'manuscript') == original


def test_unbound_raw_review_count_requires_targets_without_model(monkeypatch):
    from lazymind.document_tools.paper.writing import revise_document
    model = Mock(side_effect=AssertionError('No unbound review selection'))
    monkeypatch.setattr(revise_document, 'AutoModel', model)
    with pytest.raises(revise_document.RevisionInputRequired, match='结构化审稿报告'):
        revise_document.resolve_review_issues('只处理最重要的三条审稿意见', None)
    assert revise_document.resolve_review_issues('只修改方法中的一句话', None) == []
    model.assert_not_called()


def test_managed_bibliography_inside_code_is_not_a_source():
    published = publish_citations('# Example\nClaim[文献:SRC-fake].',
                                  [{'id': 'SRC-fake', 'citation': 'Example only'}])
    assert source_catalog({'writing_materials': '```markdown\n' + published + '\n```'}) == []


def test_raw_bundle_rejects_incomplete_managed_bibliography():
    from lazymind.document_tools.paper.publication import START
    with pytest.raises(ValueError, match='边界'):
        source_catalog({'writing_materials': START + '\nMissing end marker'})


def test_bound_manuscript_file_is_not_imported_again_as_raw_material(tmp_path, monkeypatch):
    import lazyllm
    path = tmp_path / 'draft.md'
    path.write_text(MANUSCRIPT)
    monkeypatch.setattr(lazyllm, 'globals', {'agentic_config': {'files': [str(path)]}})
    bound = {'manuscript_file': str(path)}
    assert attachments.bind_attachment(bound, str(tmp_path), 'Clarify method') == bound
    pool = workflow.initialize_pool(bound, 'Clarify method', writing_import=True)
    assert workflow.material(pool, 'manuscript')['content'] == MANUSCRIPT
    assert workflow.material(pool, 'writing_materials') is None


class DraftGenerationReached(Exception):
    """Stop at the model boundary after real attachment and input validation."""


@pytest.fixture
def draft_entry(tmp_path, monkeypatch):
    import lazyllm
    import yaml
    from lazymind.chat.engine.tools.file_resources import resolver
    from lazymind.chat.engine.tools import host_file_resolution
    from lazymind.chat.engine.subagent import tools
    from lazymind.document_tools.paper.writing import write_document

    root = Path(__file__).resolve().parents[4]
    state = yaml.safe_load((root / 'workflows/paper-writing/scenario/state.yml').read_text())
    config = {'files': []}
    monkeypatch.setattr(lazyllm, 'globals', {'agentic_config': config})
    reader = Mock(side_effect=lambda path, **kw: SimpleNamespace(path=path, kind='attachment_text'))
    monkeypatch.setattr(resolver, 'resolve_text_target', reader)
    monkeypatch.setattr(host_file_resolution, 'open_input_file', lambda path: open(path, 'rb'))
    generation = Mock(side_effect=DraftGenerationReached)
    monkeypatch.setattr(write_document, 'run', generation)
    publication = Mock(side_effect=AssertionError('Must not publish before generation'))
    monkeypatch.setattr(tools, 'save_artifacts', publication)
    context = SimpleNamespace(
        params={'step_id': 'write_document', 'current_user_input': '按已确认的大纲写正文', 'remote_inputs': {}},
        workspace_path=str(tmp_path),
        # Use the real declared inputs, including absent optional bindings, as Core does.
        input_slots=[item['material'] for item in state['steps']['write_document']['inputs']],
    )

    def file(name, value, *, upload=False, slot=None):
        path = tmp_path / name
        path.write_text(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value)
        if upload:
            config['files'].append(str(path))
        if slot:
            context.params['remote_inputs'][slot] = str(path)
        return path

    return SimpleNamespace(context=context, file=file, reader=reader, generation=generation, publication=publication)


@pytest.mark.parametrize('case', ['missing', 'old_pool', 'unconfirmed_pool', 'invalid_outline'])
def test_draft_entry_rejects_missing_or_unconfirmed_inputs_before_generation(draft_entry, case):
    entry = draft_entry
    outline = '# Test\n\n## Method\n\n## Limitations'
    uploaded = None
    if case == 'missing':
        entry.file('materials.json', {'topic': 'Test', 'question': 'Compare methods'}, upload=True)
    elif case == 'old_pool':
        pool = workflow.pool_from_business({'topic': 'Test', 'outline': outline})
        pool['history'] = [{'node': 'F5', 'step': 'write_outline', 'inputs': [],
                            'outputs': [pool['current']['outline']]}]
        pool['sequence'] = 1
        uploaded = entry.file('exported_pool.json', pool, upload=True)
    elif case == 'unconfirmed_pool':
        entry.file('pool.json', workflow.pool_from_business({'topic': 'Test', 'outline': outline}),
                   slot='write_outline_pool')
    else:
        entry.file('outline.md', 'No Markdown title', slot='outline_file')
    with pytest.raises((contracts.MissingMaterials, ValueError)) as error:
        workflow._run_paper_step(entry.context)
    assert any(message in str(error.value) for message in ('大纲', '材料', '提取', 'H1'))
    if uploaded:
        entry.reader.assert_any_call(str(uploaded), resources_only=True)
    entry.generation.assert_not_called()
    entry.publication.assert_not_called()


@pytest.mark.parametrize('source', ['outline_file', 'json_with_bound_outline', 'confirmed_outline_artifact'])
def test_draft_entry_preserves_valid_attachment_and_confirmed_outline_paths(draft_entry, source):
    entry = draft_entry
    outline = '# Test\n\n## Method\nUse the selected evidence.\n\n## Limitations'
    if source == 'confirmed_outline_artifact':
        old = '# Test\n\n## Old section'
        entry.file('pool.json', workflow.pool_from_business({'topic': 'Test', 'outline': old}),
                   slot='write_outline_pool')
        entry.file('confirmed.md', outline, slot='write_outline_document')
    else:
        entry.file('confirmed.md', outline, upload=True, slot='outline_file')
        if source == 'json_with_bound_outline':
            uploaded = entry.file('input.json', {'topic': 'Test', 'outline': outline, 'question': 'Compare methods'},
                                  upload=True)
    with pytest.raises(DraftGenerationReached):
        workflow._run_paper_step(entry.context)
    entry.generation.assert_called_once()
    inputs = entry.generation.call_args.args[0]
    assert inputs['outline'] == outline and inputs['mode'] == 'draft'
    assert inputs['manuscript'] is None
    if source == 'json_with_bound_outline':
        entry.reader.assert_any_call(str(uploaded), resources_only=True)
        assert inputs['question'] == 'Compare methods'
    entry.publication.assert_not_called()
