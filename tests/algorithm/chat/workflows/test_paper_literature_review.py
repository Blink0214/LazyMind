"""Paper review scope, material preparation, generation and publication regressions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import lazyllm
import pytest
from httpx import HTTPStatusError, Request, Response

from lazymind.common.token_estimation import estimate_tokens
from lazymind.document_tools.paper import api, contracts, literature_review, review_materials, workflow
from lazymind.document_tools.paper.runtime_events import current_events


PAPER = {'id': 'P1', 'title': 'Test paper', 'text': 'Test source material.',
         'citation': 'Test paper[Z].', 'user_annotations': {'focus': 'composition'}}


@pytest.fixture
def paper():
    return copy.deepcopy(PAPER)


@pytest.fixture
def selection(monkeypatch):
    def use(*plans):
        values = iter(plans)
        calls = []

        def choose(prompt, schema):
            calls.append(json.loads(prompt.split('Input JSON:\n', 1)[1]))
            return schema.model_validate(next(values))

        monkeypatch.setattr(review_materials, 'AutoModel', lambda **_: None)
        monkeypatch.setattr(literature_review, '_input_budget', lambda: 8192)
        monkeypatch.setattr(review_materials, 'WriterToolBase', lambda **_: SimpleNamespace(_call_llm_structured=choose))
        return calls
    return use


def test_scope_selects_requested_papers_and_keeps_background(paper, selection):
    papers = [paper, {'id': 'NEW', 'title': 'New paper', 'text': 'Paper body'},
              {'id': 'BRIEF', 'title': 'System brief', 'text': 'Experiment CPU constraints'}]
    before = copy.deepcopy(papers)
    calls = selection({'material_roles': {'M1': 'exclude', 'M2': 'paper', 'M3': 'background'}, 'queries': {}})
    result = review_materials.prepare({'papers': papers, 'request': 'Only the new paper; use the brief as context'}, [])
    assert papers == before
    assert [p['id'] for p in result] == ['NEW', 'BRIEF']
    assert result[1]['review_role'] == 'background'
    assert calls[0]['papers'][1]['id'] == 'M2'


def test_fixed_scope_preserves_exact_selection_without_model_or_kb(paper, monkeypatch):
    monkeypatch.setattr(review_materials, '_select', lambda *_: pytest.fail('frozen papers were reselected'))
    result = review_materials.prepare({'papers': [paper]}, ['other-library'], fixed_scope=True)
    assert result == [paper]
    result[0]['user_annotations']['focus'] = 'changed'
    assert paper['user_annotations']['focus'] == 'composition'


def test_scope_cannot_add_unavailable_material(paper, selection):
    selection({'material_roles': {'M1': 'paper', 'M2': 'paper'}, 'queries': {}})
    with pytest.raises(ValueError):
        review_materials.prepare({'papers': [paper]}, [])


def test_background_alone_is_not_a_review(paper, selection):
    selection({'material_roles': {'M1': 'background'}, 'queries': {}})
    with pytest.raises(contracts.MissingMaterials):
        review_materials.prepare({'papers': [paper]}, [])


def test_kb_selection_reads_only_chosen_documents_and_merges_hits(monkeypatch, selection):
    from lazymind.chat.engine.tools.lazy_kb import KBToolkit
    calls = selection({'material_roles': {}, 'queries': {'timed learning': 4}},
                      {'material_roles': {'M1': 'paper', 'M2': 'exclude'}, 'queries': {}})
    search = Mock(return_value={'items': [
        {'kb_id': 'library', 'docid': doc, 'file_name': doc, 'text': text}
        for doc, text in [('one', 'first hit'), ('one', 'second hit'), ('two', 'unrelated hit')]
    ]})
    read = Mock(return_value={'content': {'text': 'Complete document body', 'truncated': False}})
    monkeypatch.setattr(KBToolkit, 'kb_search', search)
    monkeypatch.setattr(KBToolkit, 'read_document', read)
    result = review_materials.prepare({'papers': [], 'request': 'Only use the selected knowledge base'}, ['library'])
    search.assert_called_once_with('timed learning', k_max=4, kb_ids=['library'])
    read.assert_called_once_with('library', 'one')
    assert calls[1]['papers'][0]['snippet'] == 'first hit\n\nsecond hit'
    assert [p['id'] for p in result] == ['kb-library-one']
    assert result[0]['text'] == 'Complete document body'
    assert result[0]['reading']['content_type'] == 'knowledge_base_document'


def test_kb_candidates_are_selected_together_with_existing_papers(paper, monkeypatch, selection):
    from lazymind.chat.engine.tools.lazy_kb import KBToolkit
    calls = selection({'material_roles': {'M1': 'paper'}, 'queries': {'related methods': 3}},
                      {'material_roles': {'M1': 'paper', 'M2': 'exclude', 'M3': 'paper'}, 'queries': {}})
    monkeypatch.setattr(KBToolkit, 'kb_search', Mock(return_value={'items': [
        {'kb_id': 'library', 'docid': 'duplicate', 'file_name': paper['title'] + '.pdf', 'text': paper['text']},
        {'kb_id': 'library', 'docid': 'new', 'file_name': 'Related paper', 'text': 'Retrieved excerpt'},
    ]}))
    read = Mock(return_value={'content': {'text': 'Related paper body', 'truncated': False}})
    monkeypatch.setattr(KBToolkit, 'read_document', read)
    result = review_materials.prepare({'papers': [paper], 'request': 'Use attachments and the library'}, ['library'])
    assert [p['title'] for p in calls[1]['papers']] == [paper['title'], paper['title'] + '.pdf', 'Related paper']
    assert [p['id'] for p in result] == ['P1', 'kb-library-new']
    assert result[0] == paper
    read.assert_called_once_with('library', 'new')


@pytest.mark.parametrize('papers', [[], [{'id': 'P1', 'title': 'Missing text'}]])
def test_review_requires_readable_papers(papers):
    with pytest.raises(contracts.MissingMaterials):
        api.run_algorithm('literature_review', {'topic': 'Learning', 'papers': papers})


@pytest.fixture
def handoff(monkeypatch):
    from lazymind.chat.service import academic_handoff
    value = {'kind': 'review', 'input': {
        'user_instruction': 'Compare the selected papers', 'candidate_directions': ['learning', 'vision'],
        'selected_directions': ['learning'], 'papers': [{
            'paper': {'paper_id': 'global-paper', 'title': 'External paper', 'abstract': None, 'source_version': 'v2'},
            'is_seed': False, 'is_important': True, 'direction_relation': 'Supports learning',
        }],
    }}
    monkeypatch.setattr(academic_handoff, 'academic_handoff_context', lambda: copy.deepcopy(value))
    first = {'status': 'ready', 'content_level': 'fulltext', 'content_hash': 'hash-v2', 'source_version': 'v2',
             'paper': value['input']['papers'][0]['paper'], 'pages': [{'page': 1, 'text': 'First page'}],
             'warnings': [], 'next_cursor': 'cursor-v2'}
    last = {**first, 'pages': [{'page': 0, 'text': 'Unknown page'}],
            'warnings': ['Page unknown'], 'next_cursor': ''}
    read = Mock(side_effect=[first, last])
    monkeypatch.setattr(academic_handoff, 'academic_read_paper', read)
    return value, read


def test_handoff_preserves_identity_scope_and_versioned_pages(handoff):
    original, read = handoff
    result = review_materials.handoff_input(review_materials.current_handoff())
    assert result['topic'] == 'learning'
    assert original['input']['user_instruction'] in result['request']
    assert 'selected_directions' in result['request']
    paper = result['papers'][0]
    assert paper['id'] == paper['paper_id'] == 'global-paper'
    assert paper['is_important'] is True and paper['direction_relation'] == 'Supports learning'
    assert paper['text'] == '<!-- page:1 -->\nFirst page\n\nUnknown page'
    assert paper['source_version'] == 'v2'
    assert paper['reading'] == {'content_type': 'fulltext', 'content_hash': 'hash-v2', 'warnings': ['Page unknown']}
    assert [call.kwargs for call in read.call_args_list] == [
        {'paper_id': 'global-paper'}, {'cursor': 'cursor-v2'}]


@pytest.mark.parametrize('status', [404, 403])
def test_handoff_lookup_distinguishes_ordinary_chat_from_denied_access(monkeypatch, status):
    from lazymind.chat.service import academic_handoff

    request = Request('GET', 'http://core/academic/chat-context')
    response = Response(status, request=request)
    monkeypatch.setattr(academic_handoff, 'academic_handoff_context',
                        response.raise_for_status)
    if status == 404:
        assert review_materials.current_handoff() is None
    else:
        with pytest.raises(HTTPStatusError):
            review_materials.current_handoff()


@pytest.mark.parametrize('resume', [False, True], ids=['first-entry', 'saved-pool'])
def test_handoff_enters_f2_and_publishes_reusable_artifacts(handoff, tmp_path, monkeypatch, resume):
    from lazymind.chat.engine.subagent.context import SubAgentContext

    monkeypatch.setattr(workflow, '_conversation_papers', lambda *_: pytest.fail('handoff read other attachments'))
    monkeypatch.setattr(review_materials, '_select', lambda *_: pytest.fail('handoff selection changed'))
    received = []

    def review(inputs):
        received.append(copy.deepcopy(inputs))
        return {'evidence': '# Review result', 'is_demo': False,
                'papers': [{**paper, 'citation': 'External paper.'} for paper in inputs['papers']]}

    monkeypatch.setattr(literature_review, 'run', review)
    bound = {}
    if resume:
        previous = workflow.pool_from_business({
            'topic': 'Updated topic', 'question': 'Updated research question', 'evidence': '# Edited review',
            'papers': [{'id': 'global-paper', 'title': 'External paper', 'text': 'Saved full text',
                        'source_version': 'v2', 'is_important': True}],
        })
        path = tmp_path / 'saved-pool.json'
        path.write_text(json.dumps(previous))
        bound['recovery_literature_pool'] = str(path)
        handoff[1].side_effect = AssertionError('resumed review reread the original handoff')
    events = []
    slots = ['literature_review_pool', 'literature_review_document']
    lazyllm.globals['subagent_ctx'] = SubAgentContext(
        task_id='handoff-review', conversation_id='test', agent_type='workflow_step',
        objective='Review selected papers', workspace_path=str(tmp_path), input_slots=[],
        output_slots=slots, db=None, emit=events.append,
        params={'step_id': 'literature_review', 'user_input': 'Generate the review',
                'remote_inputs': bound, 'output_slot_types': dict.fromkeys(slots, 'file')},
    )
    assert workflow.run_paper_step()['status'] == 'completed'
    assert [paper['id'] for paper in received[0]['papers']] == ['global-paper']
    saved = next(e for e in events if e['type'] == 'artifact' and e['slot'] == slots[0])
    pool = workflow._read_bound(saved['value']['path'])
    if resume:
        assert received[0]['topic'] == 'Updated topic'
        assert received[0]['question'] == 'Updated research question'
        assert received[0]['evidence'] == '# Edited review'
        assert received[0]['papers'] == workflow.material(previous, 'papers')['content']
        assert pool['materials'][:len(previous['materials'])] == previous['materials']
        assert json.loads(path.read_text()) == previous
        handoff[1].assert_not_called()
    else:
        assert handoff[0]['input']['user_instruction'] in received[0]['request']
    downstream = workflow.business_input('refine_topic', pool)
    assert downstream['papers'][0]['source_version'] == 'v2'
    assert downstream['papers'][0]['is_important'] is True
    assert downstream['evidence'] == '# Review result'


def test_handoff_waits_for_pending_fulltext(handoff, monkeypatch):
    _, read = handoff
    pages = list(read.side_effect)
    read.side_effect = [{'status': 'processing'}, *pages]
    monkeypatch.setattr(review_materials.time, 'sleep', lambda _: None)
    result = review_materials.handoff_input(handoff[0])
    assert 'First page' in result['papers'][0]['text']
    assert [call.kwargs for call in read.call_args_list[:2]] == [{'paper_id': 'global-paper'}] * 2


@pytest.mark.parametrize('status', ['failed', 'unavailable'])
def test_unready_handoff_cannot_become_a_successful_review(handoff, status):
    _, read = handoff
    read.side_effect = None
    read.return_value = {'status': status, 'content_level': 'none', 'warnings': ['Not ready']}
    with pytest.raises(contracts.MissingMaterials, match='Not ready'):
        review_materials.handoff_input(handoff[0])


@pytest.mark.parametrize('instruction,group,field', [
    (review_materials.MATERIAL_SELECTION_PROMPT, 'papers', 'snippet'),
    (literature_review.SYNTHESIS_PROMPT, 'background', 'note'),
])
def test_prompts_share_text_budget_and_preserve_source_fields(paper, instruction, group, field):
    data = {group: [
        {'id': paper['id'], 'citation': paper['citation'], field: paper['text']},
        {'id': 'SRC-007', 'citation': 'Xu R, An J, Zhan B. arXiv:2208.00412, 2022.',
         field: "Active automata learning in the framework of Angluin's $L^{*}$ algorithm has been applied "
                'to learning many kinds of automata models.'},
    ]}
    before = copy.deepcopy(data)
    empty = copy.deepcopy(data)
    for item in empty[group]:
        item[field] = ''
    empty['truncated'] = True
    fixed = literature_review.INPUT_PROMPT.format(instruction=instruction, data=json.dumps(empty, ensure_ascii=False))
    limit = estimate_tokens(fixed) + min(estimate_tokens(item[field]) for item in data[group])
    settings = lazyllm.globals['config']
    previous = settings.get('dynamic_model_configs')
    settings['dynamic_model_configs'] = {'llm': {'chat': {'max_input_tokens': limit * 2}}}
    try:
        prompt = literature_review._prompt(instruction, data)
    finally:
        if previous is None:
            settings.pop('dynamic_model_configs', None)
        else:
            settings['dynamic_model_configs'] = previous
    bounded = json.loads(prompt.split('Input JSON:\n', 1)[1])
    assert data == before
    assert estimate_tokens(prompt) <= limit
    assert bounded['truncated'] is True
    for original, item in zip(data[group], bounded[group]):
        assert item['id'] == original['id']
        assert item['citation'] == original['citation']
        assert 0 < len(item[field]) < len(original[field])
        assert original[field].startswith(item[field])
    sizes = [estimate_tokens(item[field]) for item in bounded[group]]
    assert max(sizes) - min(sizes) <= 2


def test_supplied_papers_are_read_synthesized_and_published_for_downstream(tmp_path, monkeypatch):
    from lazymind.chat.engine.subagent.context import SubAgentContext

    events = []
    readings, syntheses = {}, []
    request = 'Use the supplied papers and system context; write about 300 words.'
    inputs = {'topic': 'Learning', 'request': request, 'papers': [
        copy.deepcopy(PAPER),
        {'id': 'A2', 'title': 'Abstract paper', 'abstract': 'Abstract-only evidence.'},
        {'id': 'B1', 'title': 'System context', 'text': 'Local experiment constraints.', 'review_role': 'background'},
    ]}

    def share(*, stream):
        def generate(prompt):
            data = json.loads(prompt.split('Input JSON:\n', 1)[1])
            if 'source_id' in data:
                readings[data['source_id']] = data
                return json.dumps({'note': 'Read: ' + data['text'],
                                   'citation': 'Model reference for ' + data['title'] + '[Z].'})
            syntheses.append(data)
            raw = '# Review\n\n' + '\n\n'.join(p['note'] + ' [' + p['id'] + ']' for p in data['sources'])
            raw += '\n\n## References\n\n' + '\n'.join(
                '[' + p['id'] + '] ' + p['citation'] for p in data['sources'])
            stream['_stream_sink']({'tag': 'think', 'delta': 'Internal reasoning'})
            for delta in raw:
                stream['_stream_sink']({'tag': 'text', 'delta': delta})
            return raw
        return generate

    monkeypatch.setattr(literature_review, 'AutoModel', lambda **_: SimpleNamespace(share=share))
    monkeypatch.setattr(literature_review, '_input_budget', lambda: 8192)
    entry = tmp_path / 'papers.json'
    entry.write_text(json.dumps(inputs))
    slots = ['literature_review_pool', 'literature_review_document']
    lazyllm.globals['subagent_ctx'] = SubAgentContext(
        task_id='literature-stream', conversation_id='test', agent_type='workflow_step',
        objective='Review papers', workspace_path=str(tmp_path), input_slots=['paper_input'],
        output_slots=slots, db=None, emit=events.append,
        params={'step_id': 'literature_review', 'remote_inputs': {'paper_input': str(entry)},
                'output_slot_types': dict.fromkeys(slots, 'file')},
    )
    assert workflow.run_paper_step()['status'] == 'completed'
    published = next(e for e in events if e['type'] == 'artifact' and e['slot'] == slots[1])
    body = Path(published['value']['path']).read_text()
    assert {key: value['text'] for key, value in readings.items()} == {
        'P1': PAPER['text'], 'A2': 'Abstract-only evidence.', 'B1': 'Local experiment constraints.',
    }
    assert all(value['research']['request'] == request for value in readings.values())
    assert len(syntheses) == 1
    synthesis = syntheses[0]
    assert synthesis['research']['request'] == request
    assert [p['note'] for p in synthesis['sources']] == [
        'Read: Test source material.', 'Read: Abstract-only evidence.']
    assert [p['title'] for p in synthesis['background']] == ['System context']
    assert PAPER['citation'] in body and 'Model reference for Abstract paper[Z].' in body
    assert '[P1]' in body and '[A2]' in body and '[M1]' not in body
    assert '[B1]' not in body
    assert ''.join(e['delta'] for e in events if e['type'] == 'artifact_stream') == body
    assert next(i for i, e in enumerate(events) if e['type'] == 'artifact_stream_end') < events.index(published)
    assert all(e['slot'] == slots[1] for e in events if e['type'].startswith('artifact_stream'))
    assert 'Internal reasoning' not in json.dumps(events)
    assert current_events() is None
    saved = next(e for e in events if e['type'] == 'artifact' and e['slot'] == slots[0])
    downstream = workflow.business_input('refine_topic', workflow._read_bound(saved['value']['path']))
    assert [p['id'] for p in downstream['papers']] == ['P1', 'A2']
    assert downstream['papers'][0]['user_annotations'] == PAPER['user_annotations']
    assert downstream['papers'][0]['citation'] == PAPER['citation']
    assert downstream['papers'][1]['reading']['content_type'] == 'abstract'
    assert downstream['evidence'] == body
    assert json.loads(entry.read_text()) == inputs
