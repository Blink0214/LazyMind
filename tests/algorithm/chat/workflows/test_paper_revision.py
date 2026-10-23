"""Revision entries and current-request scope must not reuse historical review selections."""
import json
import pytest
from lazymind.document_tools.paper import api, contracts, workflow
from lazymind.document_tools.paper.writing import common, revise_document, write_document

BODY = '# Paper\n\n## Methods\nKeep methods.\n\n## Discussion\nShorten this paragraph.'


def test_separate_entries_and_no_implicit_draft_revision():
    assert api.ALGORITHMS['write_document'] is write_document
    assert api.ALGORITHMS['revise_document'] is revise_document
    assert 'revise_document' not in contracts.ALIASES
    inputs = contracts.validate_input('write_document', {'topic': 'Paper', 'manuscript': BODY})
    with pytest.raises(ValueError, match='revise_document'):
        write_document.validate_inputs(inputs)


def test_direct_revision_needs_request_and_has_no_review_context():
    inputs = contracts.validate_input('revise_document', {'topic': 'Paper', 'manuscript': BODY})
    with pytest.raises(contracts.MissingMaterials):
        revise_document.validate_inputs(inputs)
    inputs['request'] = 'Shorten Discussion only.'
    revise_document.validate_inputs(inputs)
    prepared = common.prepare_writer_context(inputs)
    assert prepared['writing_task']['task_type'] == 'revise'
    assert prepared['writing_task']['meta']['review_driven'] is False
    assert prepared['selected_issues'] == []
    assert not any(r['meta']['material_kind'] == 'review' for r in prepared['resources'])


def test_direct_business_input_omits_historical_review():
    pool = workflow.pool_from_business({'topic': 'Paper', 'manuscript': BODY})
    workflow.add_material(pool, 'review', 'Historical advice: replace every section.', [], [])
    inputs = workflow.business_input('revise_document', pool, 'Shorten Discussion only.')
    assert inputs['review'] is None and inputs['review_data'] is None
    assert inputs['selected_issue_ids'] == []
    assert inputs['manuscript'] == BODY


def test_initial_direct_revision_restores_launch_request_only_without_pool():
    pool = workflow.pool_from_business({'topic': 'Paper', 'manuscript': BODY})
    params = {'step_id': 'revise_document', 'user_input': 'Shorten Discussion only.'}
    assert workflow._step_request('revise_document', pool, params, {'manuscript_text': 'bound'}) == params['user_input']
    assert workflow._step_request('revise_document', pool, params, {'peer_review_pool': 'bound'}) == ''


def test_revision_keeps_existing_outline_material_version(monkeypatch):
    outline = '# Paper\n\n## Methods\n\n## Discussion'
    pool = workflow.pool_from_business({'topic': 'Paper', 'manuscript': BODY, 'outline': outline})
    revised = BODY.replace('## Discussion', '## Limitations')
    monkeypatch.setattr(revise_document, 'run', lambda inputs: {'manuscript': revised, 'is_demo': False})
    result = workflow.execute_step('revise_document', pool, 'Rename Discussion to Limitations.')
    assert result['pool']['current']['outline'] == pool['current']['outline']
    assert workflow.material(result['pool'], 'outline')['content'] == outline
    assert workflow.material(result['pool'], 'manuscript')['content'] == revised
    assert len(result['pool']['history'][-1]['outputs']) == 1


def test_direct_revision_without_review_does_not_call_model(monkeypatch):
    monkeypatch.setattr(revise_document, 'AutoModel', lambda **kw: pytest.fail('unnecessary scope model'))
    assert revise_document.resolve_selected_issues('只缩短讨论段落', None) == []


@pytest.mark.parametrize('intent,ids,expected', [
    ('direct', [], []),
    ('review', ['R2'], ['R2']),
    ('review', ['R1', 'R2'], ['R1', 'R2']),
    ('review', [], None),
    ('review', ['R9'], None),
    ('unclear', [], None),
    ('direct', ['R1'], None),
])
def test_review_scope_validation(monkeypatch, intent, ids, expected):
    monkeypatch.setattr(revise_document, 'AutoModel', lambda **kw: lambda prompt:
                        json.dumps({'intent': intent, 'issue_ids': ids}))
    review = {'issues': [{'id': 'R1'}, {'id': 'R2'}]}
    if expected is None:
        with pytest.raises(contracts.MissingMaterials):
            revise_document.resolve_selected_issues('本轮修订要求', review)
    else:
        assert revise_document.resolve_selected_issues('本轮修订要求', review) == expected


def test_missing_current_request_does_not_reuse_old_selection():
    with pytest.raises(contracts.MissingMaterials):
        revise_document.resolve_selected_issues('', {'issues': [{'id': 'R1'}]})
