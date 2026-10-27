"""Offline regressions for literature-review routing, handoff, and streaming."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import lazyllm
import pytest

from lazymind.common.token_estimation import estimate_tokens
from lazymind.document_tools.paper import api, contracts, literature_review, workflow
from lazymind.document_tools.paper.material_bindings import MaterialBindings
from lazymind.document_tools.paper.runtime_events import current_events


PAPER = {'id': 'P1', 'title': 'Test paper', 'text': 'Test source material.',
         'citation': 'Test paper[Z].', 'user_annotations': {'focus': 'composition'}}


@pytest.fixture
def paper():
    return copy.deepcopy(PAPER)


def test_review_scope_excludes_old_papers_and_separates_background(paper):
    sources = [paper, {'id': 'STORM', 'title': 'STORM', 'text': 'Uploaded paper'},
               {'id': 'BRIEF', 'title': 'System description', 'text': 'CPU limits apply to experiments.'}]
    before = copy.deepcopy(sources)
    bindings = MaterialBindings(sources)
    plan = literature_review._review_plan_schema(bindings)(
        queries={'unwanted search': 5}, material_roles={'M1': 'exclude', 'M2': 'paper', 'M3': 'background'},
        supplied_only=True, paper_limit=None,
    )
    selected, background = literature_review._select_materials(plan, bindings)
    assert [item['id'] for item in selected] == ['STORM']
    assert [item['id'] for item in background] == ['BRIEF']
    assert plan.queries == {}
    assert sources == before


@pytest.mark.parametrize('roles', [{'unknown': 'paper'}, {}, {'M1': 'invented'},
                                 {'M1': 'paper', 'M2': 'background'}])
def test_review_roles_are_fixed_by_host(paper, roles):
    with pytest.raises(ValueError):
        literature_review._review_plan_schema(MaterialBindings([paper]))(
            queries={}, material_roles=roles, supplied_only=True, paper_limit=None)


def test_background_is_not_a_supplied_paper(paper):
    bindings = MaterialBindings([paper])
    plan = literature_review._review_plan_schema(bindings)(
        queries={}, material_roles={'M1': 'background'}, supplied_only=True, paper_limit=None)
    with pytest.raises(contracts.MissingMaterials):
        literature_review._select_materials(plan, bindings)


def test_review_scope_allows_search_without_uploaded_papers():
    bindings = MaterialBindings([])
    plan = literature_review._review_plan_schema(bindings)(
        queries={'academic writing agents': 5}, material_roles={}, supplied_only=False, paper_limit=None,
    )
    assert literature_review._select_materials(plan, bindings) == ([], [])
    assert plan.queries == {'academic writing agents': 5}


def test_background_only_can_start_retrieval_without_recopying_identity():
    source = {'id': 'upload-17cae16226173b1c269c5a71cde4c14c6a34c189ed9e69380936615c861bc78a',
              'title': 'System brief', 'text': 'Actual implementation and resource constraints.'}
    bindings = MaterialBindings([source])
    plan = literature_review._review_plan_schema(bindings)(
        queries={'academic writing agents': 5}, material_roles={'M1': 'background'},
        supplied_only=False, paper_limit=None)
    selected, background = literature_review._select_materials(plan, bindings)
    assert selected == []
    assert background == [source]
    assert plan.queries == {'academic writing agents': 5}


def test_review_output_preserves_long_evidence_for_downstream(paper):
    evidence = ('# 文献综述\n\n' + '有依据的比较与局限。' * 100
                + '\n\n[SRC-001]\n\n## 参考文献\n\n' + paper['citation'])
    result = {'evidence': evidence, 'papers': [paper], 'is_demo': False}
    validated = contracts.validate_output('literature_review', result)
    assert validated['evidence'] == evidence
    assert validated['papers'] == [paper]


@pytest.mark.parametrize('instruction,group,field', [
    (literature_review.SEARCH_PLAN_PROMPT, 'papers', 'snippet'),
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


@pytest.fixture(autouse=True)
def restore_runtime_context():
    previous = {key: lazyllm.globals.get(key) for key in ('agentic_config', 'subagent_ctx')}
    yield
    for key, value in previous.items():
        if value is None:
            lazyllm.globals.pop(key, None)
        else:
            lazyllm.globals[key] = value


@pytest.mark.parametrize('kb,restricted,failed,with_paper,expected', [
    (True, False, False, True, ['knowledge_base', 'api']),
    (True, True, False, False, ['knowledge_base']),
    (False, False, True, True, ['api']),
    (False, False, True, False, ['api']),
])
def test_source_routing_preserves_materials(monkeypatch, kb, restricted, failed, with_paper, expected):
    calls = []
    first = literature_review._review_plan_schema(MaterialBindings([PAPER] if with_paper else []))(
        queries={'first': 1}, allow_external_search=not restricted, paper_limit=None,
        material_roles={'M1': 'paper'} if with_paper else {}, supplied_only=False,
    )
    plans = iter([first, literature_review._SearchPlan(
        queries={'second': 1}, allow_external_search=not restricted, paper_limit=None,
    )])
    lazyllm.globals['agentic_config'] = {'filters': {'kb_id': ['test-kb'] if kb else []}}
    monkeypatch.setattr(literature_review, 'AutoModel', lambda **_: None)
    monkeypatch.setattr(literature_review, 'WriterToolBase', lambda **_: SimpleNamespace(
        _call_llm_structured=lambda *args: next(plans)))
    monkeypatch.setattr(literature_review, '_input_budget', lambda: 8192)
    monkeypatch.setattr(literature_review, '_providers', lambda _: [SimpleNamespace(source_name='arxiv')])
    monkeypatch.setattr(literature_review, '_notes', lambda papers, _: list(papers))
    render = Mock(return_value='Evidence')
    monkeypatch.setattr(literature_review, '_render', render)

    def retrieve(providers, plan, sources, seen, used_ids, context, *, knowledge_base):
        calls.append(context['search_source'])
        assert knowledge_base == (context['search_source'] == 'knowledge_base')
        assert bool(providers) != knowledge_base
        if failed:
            providers.clear()
            return []
        return [{**PAPER, 'id': f'R{len(calls)}'}]

    monkeypatch.setattr(literature_review, '_retrieve', retrieve)
    result = api.run_algorithm('literature_review', {'topic': 'Test topic', 'papers': [PAPER] if with_paper else []})
    assert calls == expected
    expected_ids = ([PAPER['id']] if with_paper else []) + (
        [] if failed else [f'R{i + 1}' for i in range(len(expected))])
    assert [paper['id'] for paper in result['papers']] == expected_ids
    render.assert_called_once()
    assert render.call_args.args[1] == result['papers']
    assert render.call_args.args[2] == []
    pool = workflow.pool_from_business({'topic': 'Test topic', **{k: result[k] for k in ('papers', 'evidence')}})
    downstream = workflow.business_input('refine_topic', pool)
    assert downstream['papers'] == result['papers']
    assert downstream['evidence'] == result['evidence']
    if with_paper:
        assert downstream['papers'][0] == PAPER


def test_provider_failover_keeps_successful_results(monkeypatch):
    hit = {'title': 'Test paper', 'snippet': 'Abstract', 'extra': {'year': 2025}}
    sciverse = SimpleNamespace(source_name='sciverse', search=Mock(side_effect=[
        [{**hit, 'url': 'https://sciverse.test/paper'}], httpx.ConnectError('search unavailable'),
    ]))
    arxiv = SimpleNamespace(source_name='arxiv', search=Mock(return_value=[
        {**hit, 'url': 'https://arxiv.test/paper'},
    ]))
    monkeypatch.setattr(literature_review, '_arxiv_request', lambda call, *a, **kw: call(*a, **kw))
    providers = [sciverse, arxiv]
    plan = literature_review._SearchPlan(queries={'first': 1, 'second': 1, 'third': 1}, paper_limit=None)
    results = list(literature_review._search_results(providers, plan))
    assert [item['source'] for item in results] == ['sciverse', 'arxiv', 'arxiv']
    assert all(item['text'] == hit['snippet'] and item['metadata'] == hit['extra'] for item in results)
    assert results[0]['url'] == 'https://sciverse.test/paper'
    assert providers == [arxiv]
    assert sciverse.search.call_count == 2
    assert [call.args[0] for call in arxiv.search.call_args_list] == ['second', 'third']


@pytest.mark.parametrize('with_paper', [True, False])
def test_workflow_streams_evidence_before_publication(tmp_path, monkeypatch, with_paper):
    from lazymind.chat.engine.subagent.context import SubAgentContext

    events = []
    raw = '# Review\n\nEvidence [M1].' if with_paper else '# Review\n\nEvidence.'
    body = raw.replace('[M1]', f'[{PAPER["id"]}]')

    def share(*, stream):
        def generate(prompt):
            assert (literature_review.SYNTHESIS_PROMPT if with_paper else literature_review.OVERVIEW_PROMPT) in prompt
            if with_paper:
                assert json.loads(prompt.split('Input JSON:\n', 1)[1])['sources'][0]['id'] == 'M1'
            stream['_stream_sink']({'tag': 'think', 'delta': 'Internal reasoning'})
            for delta in raw:
                stream['_stream_sink']({'tag': 'text', 'delta': delta})
            assert ''.join(e['delta'] for e in events if e['type'] == 'artifact_stream') == body
            return raw
        return generate

    def review(inputs):
        return {'evidence': literature_review._render(inputs, inputs['papers'], []),
                'papers': inputs['papers'], 'is_demo': False}

    monkeypatch.setattr(literature_review, 'AutoModel', lambda **_: SimpleNamespace(share=share))
    monkeypatch.setattr(literature_review, '_input_budget', lambda: 8192)
    monkeypatch.setattr(literature_review, 'run', review)
    entry = tmp_path / 'papers.json'
    entry.write_text(json.dumps({'topic': 'Test topic', 'papers': [PAPER] if with_paper else []}))
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
    assert Path(published['value']['path']).read_text() == body
    assert next(i for i, e in enumerate(events) if e['type'] == 'artifact_stream_end') < events.index(published)
    assert all(e['slot'] == slots[1] for e in events if e['type'].startswith('artifact_stream'))
    assert any(e['type'] == 'progress' for e in events)
    assert any(e['type'] == 'text' and '正在生成' in e['text'] for e in events)
    assert 'Internal reasoning' not in json.dumps(events)
    assert current_events() is None
