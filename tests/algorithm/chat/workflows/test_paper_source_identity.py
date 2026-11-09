from lazymind.document_tools.paper.writing import revise_document as paper_writing_revise_document
from lazymind.document_tools.paper.writing import write_document as paper_writing_write_document
import copy

import pytest

from lazymind.document_tools.paper import publication, contracts, workflow
from lazymind.document_tools.paper.contracts import normalize_papers


def paper(identifier=None):
    value = {'title': 'A study', 'text': 'Evidence from an abstract.', 'citation': 'A study[Z].',
             'metadata': {'year': 2025}, 'reading': [{'content_type': 'abstract', 'truncated': False}],
             'kind': 'paper', 'user_annotations': {'focus': 'method'}}
    if identifier is not None:
        value['id'] = identifier
    return value


def test_missing_ids_stable_across_order_retry_and_metadata_changes():
    items = [paper('SRC-001'), paper('KB-001'), paper(), {**paper(), 'title': 'Other'}]
    before = copy.deepcopy(items)
    normalized = normalize_papers(items)
    assert items == before
    assert normalized[:2] == items[:2]
    assert normalize_papers(normalized) == normalized
    assert normalize_papers(list(reversed(items))) == list(reversed(normalized))
    changed = {**items[2], 'citation': 'Another format'}
    assert normalize_papers([changed])[0]['id'] == normalized[2]['id']
    inputs = contracts.validate_input('write_document', {'topic': 'T', 'papers': items})
    assert inputs['papers'] == normalized
    inputs['papers'][0]['metadata']['year'] = 1900
    assert items[0]['metadata']['year'] == 2025


@pytest.mark.parametrize('items', [[paper('SRC-001'), paper('SRC-001')], [paper(), paper()],
                                    [paper('bad id')], [paper(123)]])
def test_reject_ambiguous_or_invalid_identity(items):
    with pytest.raises(ValueError):
        normalize_papers(items)


def test_pool_persists_identity_without_mutating_input():
    source = {'topic': 'T', 'papers': [paper()]}
    pool = workflow.pool_from_business(source)
    assigned = workflow.material(pool, 'papers')['content'][0]['id']
    assert assigned.startswith('ref-')
    assert 'id' not in source['papers'][0]
    inputs = contracts.validate_input('write_document', workflow.business_input('write_document', pool))
    assert inputs['papers'][0]['id'] == assigned
    assert workflow.material(pool, 'papers')['source_refs'] == [assigned]


def test_old_pool_missing_identity_creates_papers_version(monkeypatch):
    pool = workflow.pool_from_business({'topic': 'T', 'papers': [paper()]})
    workflow.material(pool, 'papers')['content'][0].pop('id')
    workflow.material(pool, 'papers')['content'][0]['abstract'] = 'Already prepared'
    before = copy.deepcopy(pool)
    def run(step, inputs):
        assert inputs['papers'][0]['id'].startswith('ref-')
        return {'question': 'A question', 'is_demo': False}
    monkeypatch.setattr(workflow, 'run_algorithm', run)
    result = workflow.execute_step('research_topic', pool)['pool']
    assert pool == before
    assert result['current']['papers']['version'] == 2
    assert workflow.material(result, 'papers')['derived_from'] == [before['current']['papers']]


def test_pr81_evidence_preserved_and_manuscript_imported():
    papers = [paper('SRC-001'), {**paper('KB-001'), 'citation': 'KB source[Z].'}]
    original = '# T\n\n## Findings\nResult[SRC-001], context[KB-001], again[SRC-001].\n\n## 参考文献\n[SRC-001] A study[Z].\n[KB-001] KB source[Z].'
    normalized = contracts.validate_input('revise_document', {
        'topic': 'T', 'papers': papers, 'evidence': original, 'manuscript': original})
    assert normalized['evidence'] == original
    body = normalized['manuscript']
    assert '## 参考文献' not in body
    assert body.count('[文献:SRC-001]') == 2
    saved = publication.publish_citations(body, papers)
    assert saved.count('[\\[1\\]](#paper-ref-SRC-001)') == 2
    assert '[\\[2\\]](#paper-ref-KB-001)' in saved
    assert publication.normalize_source_citations(saved, papers) == saved
    assert publication.publish_citations(saved, papers) == saved
    publication.validate_citations(saved, papers, published=True)


def test_source_import_skips_code_links_images_and_numeric_ids():
    source = '`[SRC-001]`\n```md\n[SRC-001]\n```\n![SRC-001](image.svg) [SRC-001](https://example.com) [1] [SRC-001]'
    normalized = publication.normalize_source_citations(source, [paper('SRC-001'), paper('1')])
    assert normalized.endswith('[1] [文献:SRC-001]')
    assert normalized.count('[文献:') == 1
    with pytest.raises(ValueError, match='混编'):
        publication.publish_citations(normalized, [paper('SRC-001')])


@pytest.mark.parametrize('body', ['Unknown[SRC-999]',
    'Known[SRC-001]\n\n## 参考文献\n[SRC-001] Different entry'])
def test_source_import_rejects_unknown_or_conflicting_entries(body):
    with pytest.raises(ValueError):
        publication.normalize_source_citations(body, [paper('SRC-001')])


@pytest.mark.parametrize('entry', [None, '', 123])
def test_pr_citation_is_optional_but_nonempty_when_present(entry):
    with pytest.raises(ValueError, match='paper.citation'):
        contracts.validate_input('write_document', {'topic': 'T', 'papers': [{**paper(), 'citation': entry}]})


@pytest.mark.parametrize('heading', ['## References', '**参考文献**', '### Bibliography'])
def test_pr_source_list_heading_and_bullets(heading):
    body = f'Result[SRC-001].\n\n{heading}\n- [SRC-001] A study[Z].'
    assert publication.normalize_source_citations(body, [paper('SRC-001')]) == 'Result[文献:SRC-001].'


def test_generated_body_cannot_leak_pr_source_markers():
    from lazymind.document_tools.paper.writing.common import _validate_reference_markers
    with pytest.raises(ValueError, match='成稿来源引用'):
        _validate_reference_markers('Result[SRC-001]', [paper('SRC-001')])
    _validate_reference_markers('Result[文献:SRC-001]', [paper('SRC-001')])


def test_adjacent_source_citations_and_reference_style_links():
    papers = [paper('SRC-001'), paper('KB-001')]
    body = '[SRC-001][KB-001] and [SRC-001][label]'
    assert publication.normalize_source_citations(body, papers) == (
        '[文献:SRC-001][文献:KB-001] and [SRC-001][label]')
