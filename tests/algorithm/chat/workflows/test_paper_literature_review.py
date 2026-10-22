import copy
import json
import os

import lazyllm
import pytest

from lazymind.common.token_estimation import estimate_tokens
from lazymind.document_tools.paper import api, contracts, literature_review, workflow


TOPIC = '时间同步系统的组合学习（形式化方法与时间自动机的组合式主动学习）'


@pytest.fixture
def paper():
    return {
        'id': 'SRC-001',
        'title': 'A Detailed Account of Compositional Automata Learning Through Alphabet Refinement',
        'text': 'Active automata learning infers automaton models of systems from behavioral observations, '
                'a technique successfully applied to a wide range of domains. Compositional approaches '
                'have recently emerged to address scalability to concurrent systems.',
        'citation': 'Henry L, Mousavi M R, Neele T, et al. A detailed account of compositional automata '
                    'learning through alphabet refinement[EB/OL]. arXiv:2504.16624v3, 2025.',
        'metadata': {'arxiv_id': '2504.16624v3', 'year': 2025},
        'user_annotations': {'focus': '同步并行系统的组合学习'},
    }


@pytest.mark.parametrize('with_citation', [True, False])
def test_input_preserves_paper_fields_and_does_not_share_nested_values(paper, with_citation):
    if not with_citation:
        paper.pop('citation')
    inputs = {'topic': TOPIC, 'papers': [paper]}
    normalized = contracts.validate_input('literature_review', inputs)
    assert normalized['papers'] == [paper]
    normalized['papers'][0]['user_annotations']['focus'] = '关注可扩展性'
    assert paper['user_annotations']['focus'] == '同步并行系统的组合学习'


def test_accepts_abstract_without_full_text_and_preserves_fields(paper):
    paper['abstract'] = paper.pop('text')
    pool = workflow.pool_from_business({'topic': TOPIC, 'papers': [paper]})
    inputs = contracts.validate_input('literature_review', workflow.business_input('literature_review', pool))
    assert inputs['papers'] == [paper]
    assert 'text' not in inputs['papers'][0]


def test_materials_preserve_papers_and_citations_for_downstream_steps(paper):
    materials = {
        'topic': TOPIC, 'papers': [paper], 'evidence': paper['text'],
        'question': '组合式主动学习在时间同步系统中有哪些适用条件和限制？',
    }
    pool = workflow.pool_from_business(materials)
    for step in ('literature_review', 'refine_topic', 'experiment', 'write_document', 'reading_reflection'):
        inputs = contracts.validate_input(step, workflow.business_input(step, pool))
        assert inputs['evidence'] == materials['evidence']
        if 'papers' in inputs:
            assert inputs['papers'] == [paper]
            inputs['papers'][0]['user_annotations']['focus'] = '关注可扩展性'
            assert workflow.material(pool, 'papers')['content'] == [paper]


@pytest.mark.parametrize('instruction,group,field', [
    (literature_review.SEARCH_PLAN_PROMPT, 'papers', 'note'),
    (literature_review.COVERAGE_PROMPT, 'sources', 'note'),
    (literature_review.SYNTHESIS_PROMPT, 'sources', 'note'),
    (literature_review.CANDIDATE_SELECTION_PROMPT, 'candidates', 'text'),
])
def test_prompts_share_text_budget_and_preserve_source_fields(paper, instruction, group, field):
    data = {group: [
        {'id': paper['id'], 'citation': paper['citation'], field: paper['text']},
        {'id': 'SRC-007', 'citation': 'Xu R, An J, Zhan B. arXiv:2208.00412, 2022.',
         field: "Active automata learning in the framework of Angluin's $L^{*}$ algorithm has been applied "
                'to learning many kinds of automata models.'},
    ]}
    if group == 'candidates':
        for item in data[group]:
            item['snippet'] = item[field]
    before = copy.deepcopy(data)
    empty = copy.deepcopy(data)
    for item in empty[group]:
        item[field] = ''
        item.pop('snippet', None)
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
        assert 'snippet' not in item
        assert 0 < len(item[field]) < len(original[field])
        assert original[field].startswith(item[field])
    sizes = [estimate_tokens(item[field]) for item in bounded[group]]
    assert max(sizes) - min(sizes) <= 2


@pytest.mark.skipif(
    os.environ.get('PAPER_REVIEW_LIVE') != '1',
    reason='设置 PAPER_REVIEW_LIVE=1，并配置真实模型后执行。',
)
def test_real_abstract_only_review(paper):
    paper['abstract'] = paper.pop('text')
    inputs = {'topic': TOPIC, 'papers': [paper],
              'request': '只综述给定论文摘要，不开展检索，正文约200字。'}
    before = copy.deepcopy(inputs)
    result = api.run_algorithm('literature_review', inputs)
    assert inputs == before
    assert len(result['papers']) == 1
    assert {key: result['papers'][0][key] for key in paper} == paper
    assert result['papers'][0]['reading']['content_type'] == 'abstract'
    assert result['evidence'].strip()
    assert paper['citation'] in result['evidence']


@pytest.mark.skipif(
    os.environ.get('PAPER_REVIEW_LIVE') != '1',
    reason='设置 PAPER_REVIEW_LIVE=1，并配置真实模型后执行。',
)
def test_real_search_generates_citations(paper):
    inputs = {
        'topic': TOPIC,
        'request': f'使用 arXiv 检索论文“{paper["title"]}”并综述，正文约200字。',
    }
    before = copy.deepcopy(inputs)
    result = api.run_algorithm('literature_review', inputs)
    assert inputs == before
    assert result['is_demo'] is False
    assert result['papers']
    for paper in result['papers']:
        assert paper['reading']
        assert paper['text'].strip()
        assert not paper['citation'].startswith('[')
        assert f'[{paper["id"]}]' in result['evidence']
        assert paper['citation'] in result['evidence']
    without_citation = copy.deepcopy(result)
    without_citation['papers'][0].pop('citation')
    with pytest.raises(ValueError, match=r'paper\.citation'):
        contracts.validate_output('literature_review', without_citation)
