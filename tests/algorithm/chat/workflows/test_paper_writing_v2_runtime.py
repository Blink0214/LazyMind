"""Historical entry scenarios expressed against the current paper contracts."""
import copy
import pytest
from lazymind.document_tools.paper import contracts, workflow
from lazymind.document_tools.paper.writing import common

BODY = '# Paper\n\n## Methods\nEvidence.'


def material(**overrides):
    return dict(topic='Paper', papers=[], question=None, evidence=None, analysis=None,
                charts=[], request='Clarify wording', **overrides)


@pytest.mark.parametrize('question', [{'candidates': []}, {'selected_id': 'Q1', 'candidates': [{'id': 'Q1'}]}])
def test_legacy_candidate_question_is_rejected_instead_of_silently_selected(question):
    value = material()
    value['question'] = question
    with pytest.raises(ValueError):
        contracts.validate_input('write_document', value)


def test_direct_revision_preserves_materials_without_input_mutation():
    value = material(manuscript=BODY)
    before = copy.deepcopy(value)
    normalized = contracts.validate_input('revise_document', value)
    common.validate_inputs(normalized)
    assert normalized['mode'] == 'revise' and normalized['manuscript'] == BODY
    assert value == before


def test_existing_manuscript_is_not_silently_written_as_new_draft():
    normalized = contracts.validate_input('write_document', material(manuscript=BODY))
    with pytest.raises(ValueError, match='revise_document'):
        common.validate_inputs(normalized)


@pytest.mark.parametrize('outline', [None, '# Obsolete\n\n## Old structure'])
def test_revision_result_does_not_require_output_outline(outline):
    normalized = contracts.validate_input('revise_document', material(manuscript=BODY, outline=outline))
    common.validate_result({'manuscript': BODY.replace('Evidence.', 'Updated evidence.')}, normalized)


def test_new_draft_still_requires_confirmed_outline():
    normalized = contracts.validate_input('write_document', material(outline='# Paper\n\n## Different'))
    with pytest.raises(ValueError, match='大纲'):
        common.validate_result({'manuscript': BODY}, normalized)


def test_revision_request_does_not_replay_launch_requirements():
    pool = workflow.pool_from_business(material(manuscript=BODY))
    params = {'current_user_input': 'Only update Methods', 'user_input': 'Old launch', 'launch_user_input': 'Old launch'}
    request = workflow._step_request('revise_document', pool, params)
    assert request.endswith('Only update Methods')
    assert 'Clarify wording' in request
    assert 'Old launch' not in request
