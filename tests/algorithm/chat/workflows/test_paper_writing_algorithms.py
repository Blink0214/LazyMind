"""Module-owner contracts; implemented algorithms use their configured real model."""
import base64
import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / 'workflows/paper-writing'
EXAMPLES = ROOT / 'tests/fixtures/paper-writing'
from lazymind.document_tools.paper import api
from lazymind.document_tools.paper import research_topic
from lazymind.document_tools.paper import workflow as tools

TOPIC_MARKDOWN = '# 研究选题\n\n## 研究问题\n现有方法能否改善效果？\n\n## 证据状态\n尚待核验。'
REFINED_TOPIC_MARKDOWN = TOPIC_MARKDOWN + '\n\n## 文献反馈与调整依据\n根据综述调整验证路线。'


@pytest.fixture
def mock_topic_model(monkeypatch):
    monkeypatch.setattr(research_topic, 'AutoModel', lambda **_kwargs: lambda prompt: (
        REFINED_TOPIC_MARKDOWN if '"mode": "refine"' in prompt else TOPIC_MARKDOWN
    ))


@pytest.mark.usefixtures('mock_topic_model')
@pytest.mark.parametrize('step', list(tools.STEP_NODE))
def test_independent_algorithm_contract(step):
    example = json.loads((EXAMPLES / (step + '.json')).read_text())
    before = copy.deepcopy(example['input'])
    output = tools.run_algorithm(step, example['input'])
    contracts = tools.contracts
    module = contracts.ALIASES.get(step, step)
    contracts.validate_output(module, example['output'])
    assert example['input'] == before
    assert set(output) == {'is_demo', *contracts.OUTPUTS[module]}
    assert not {'pool', 'current', 'history', 'schema', 'remote_inputs'} & example['input'].keys()


@pytest.mark.usefixtures('mock_topic_model')
@pytest.mark.parametrize('step,fields', [
    ('research_topic', {}), ('literature_review', {}),
    ('data_analysis', {'data': {'columns': ['score'], 'rows': [[1]]}}),
    ('write_document', {'outline': '# 大纲\n摘要、方法、结论'}),
    ('peer_review', {'manuscript': '# 用户提供的稿件'}),
    ('reading_reflection', {'papers': [{'id': 'P1', 'title': '论文', 'text': '论文内容'}]}),
])
def test_middle_entry_needs_only_business_data(step, fields):
    pool = tools.pool_from_business({'topic': '用户主题', **fields})
    assert pool['history'] == []
    assert not any(item['mock'] for item in pool['materials'])
    assert tools.execute_step(step, pool)['status'] == 'completed'


@pytest.mark.parametrize('step,inputs', [
    ('peer_review', {'topic': 'T'}), ('revise_document', {'topic': 'T', 'manuscript': '# 稿件'}),
    ('refine_topic', {'topic': 'T'}), ('reading_reflection', {'topic': 'T', 'papers': []}),
    ('data_analysis', {'topic': 'T', 'experiment': {
        'executed': False, 'plan': '方案', 'results': None, 'report': '仅方案'}}),
])
def test_missing_business_materials_stop_before_algorithm(step, inputs, monkeypatch):
    module = tools.contracts.ALIASES.get(step, step)
    def must_not_run(_):
        pytest.fail('missing materials reached algorithm')
    monkeypatch.setattr(api.ALGORITHMS[module], 'run', must_not_run)
    with pytest.raises(tools.MissingMaterials, match='缺少材料'):
        tools.run_algorithm(step, inputs)


def test_f1_without_papers_uses_unverified_evidence_prompt(monkeypatch):
    prompts = []
    monkeypatch.setattr(research_topic, 'AutoModel',
                        lambda **_kwargs: lambda prompt: prompts.append(prompt) or TOPIC_MARKDOWN)
    result = tools.run_algorithm('research_topic', {'topic': '研究主题'})
    assert result == {'question': TOPIC_MARKDOWN, 'is_demo': False}
    assert 'No parsed papers are currently available' in prompts[0]
    assert 'Do not claim to have read papers' in prompts[0]
    assert 'Return only one Markdown document written in Chinese' in prompts[0]


def test_f1_topic_inference_reads_paper_opening_not_filename(monkeypatch):
    prompts = []
    monkeypatch.setattr(research_topic, 'AutoModel',
                        lambda **_kwargs: lambda prompt: prompts.append(prompt) or '{"topic": "证据可追溯性"}')
    papers = [{'id': 'P1', 'title': '无关文件名', 'text': '<!-- page:1 -->\n摘要：讨论证据可追溯性。'}]
    assert research_topic.infer_topic('根据论文确定选题', papers) == '证据可追溯性'
    assert 'Use Chinese for a non-null topic value' in prompts[0]
    assert '讨论证据可追溯性' in prompts[0]
    assert '无关文件名' not in prompts[0]


def test_f1_long_paper_condenses_without_mutating_source_pool(monkeypatch):
    paper = {'id': 'P1', 'title': '长论文', 'text': ''.join(
        f'<!-- page:{page} -->\n第{page}页证据。' + '研究方法与结果。' * 190 + '\n'
        for page in range(1, 5)
    )}
    pool = tools.pool_from_business({'topic': '研究主题', 'papers': [paper]})
    before = copy.deepcopy(pool)
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return TOPIC_MARKDOWN if 'Input materials (JSON):\n' in prompt else '[P1] 方法与结果仍需核验。'

    monkeypatch.setattr(research_topic, 'AutoModel', lambda **_kwargs: model)
    result = tools.execute_step('research_topic', pool)
    final_input = json.loads(prompts[-1].split('Input materials (JSON):\n', 1)[1])
    assert 'Page-by-page notes' in final_input['papers'][0]['text']
    assert pool == before
    assert tools.material(result['pool'], 'papers')['content'] == [paper]


@pytest.mark.parametrize('invalid', ['', '## 其他章节\n内容',
                                     TOPIC_MARKDOWN + '\n\n## 研究问题\n另一个问题'])
def test_f1_rejects_invalid_model_output_without_publishing(invalid, monkeypatch):
    monkeypatch.setattr(research_topic, 'AutoModel', lambda **_kwargs: lambda _prompt: invalid)
    pool = tools.pool_from_business({'topic': 'T'})
    before = copy.deepcopy(pool)
    with pytest.raises(ValueError, match='研究选题输出'):
        tools.execute_step('research_topic', pool)
    assert pool == before


def test_f2_feedback_refines_f1_question_without_overwriting_evidence(monkeypatch):
    old_question = '# 旧选题\n\n## 研究问题\n原方法能否改善效果？'
    pool = tools.pool_from_business({'topic': 'T', 'question': old_question})
    monkeypatch.setattr(api.ALGORITHMS['literature_review'], 'run', lambda inputs: {
        'evidence': '# 初始综述\n尚待人工核对。', 'papers': inputs['papers'], 'is_demo': False,
    })
    reviewed = tools.execute_step('literature_review', pool)
    edited = '# 人工确认的综述\n需要缩小研究范围。'
    selected = tools.pool_from_inputs({
        'literature_review_pool': reviewed['pool'], 'literature_review_document': edited,
    })
    prompts = []
    monkeypatch.setattr(research_topic, 'AutoModel', lambda **_kwargs: lambda prompt: (
        prompts.append(prompt) or REFINED_TOPIC_MARKDOWN
    ))
    result = tools.execute_step('refine_topic', selected, '缩小研究范围')
    model_input = json.loads(prompts[0].split('Input materials (JSON):\n', 1)[1])
    assert model_input['question'] == old_question
    assert model_input['evidence'] == edited
    assert result['document'] == REFINED_TOPIC_MARKDOWN


def test_one_real_algorithm_replacement_preserves_storage_and_next_input(monkeypatch):
    pool = tools.pool_from_business({'topic': '实际主题', 'papers': [
        {'id': 'P1', 'title': '原文', 'text': '真实输入正文'}]})
    before = copy.deepcopy(pool)
    seen = []
    def research(inputs):
        seen.append(copy.deepcopy(inputs))
        inputs['papers'].clear()  # A module cannot mutate caller-owned state.
        return {'question': '# 新选题\n真实算法返回的候选问题', 'is_demo': False}
    monkeypatch.setattr(api.ALGORITHMS['research_topic'], 'run', research)
    selected = tools.execute_step('research_topic', pool)
    assert pool == before
    assert selected['mock'] is False
    assert '演示数据' not in selected['document']
    assert seen[0]['papers'][0]['id'] == 'P1'
    edited = '# 用户确认后的选题\nEDITED-UPSTREAM'
    updated = tools.pool_from_inputs({'research_topic_pool': selected['pool'], 'research_topic_document': edited})
    def literature(inputs):
        assert inputs['question'] == edited
        assert inputs['papers'][0]['text'] == '真实输入正文'
        return {'evidence': '# 新综述\n根据新选题完成', 'papers': inputs['papers'], 'is_demo': False}
    monkeypatch.setattr(api.ALGORITHMS['literature_review'], 'run', literature)
    result = tools.execute_step('literature_review', updated)
    assert result['mock'] is False
    assert tools.material(result['pool'], 'question')['content'] == edited
    assert tools.material(result['pool'], 'evidence')['content'] == '# 新综述\n根据新选题完成'


@pytest.mark.usefixtures('mock_topic_model')
@pytest.mark.parametrize('path', [
    ['research_topic', 'literature_review', 'refine_topic', 'literature_review'],
    ['literature_review', 'write_document', 'peer_review', 'revise_document', 'peer_review'],
    ['experiment', 'data_analysis', 'write_document'], ['literature_review', 'reading_reflection'],
])
def test_every_path_supplies_exact_current_business_values(path, monkeypatch):
    pool = tools.seed_pool()
    contracts = tools.contracts
    for step in path:
        module = api.ALGORITHMS[contracts.ALIASES.get(step, step)]
        expected = contracts.validate_input(step, tools.business_input(step, pool, '本轮要求'))
        original = module.run
        received = []
        def capture(value):
            received.append(copy.deepcopy(value))
            return original(value)
        with monkeypatch.context() as patch:
            patch.setattr(module, 'run', capture)
            pool = tools.execute_step(step, pool, '本轮要求')['pool']
        assert received == [expected]


@pytest.mark.parametrize('value', [
    {'topic': 'T', 'data': {'columns': ['a'], 'rows': [[1, 2]]}},
    {'topic': 'T', 'data': {'columns': ['a'], 'rows': [[float('nan')]]}},
    {'topic': 'T', 'papers': [{'id': 'P1', 'title': 'T'}]},
    {'topic': 'T', 'demo': True, 'manuscript': '真实稿件'},
])
def test_invalid_business_entry_rejected(value):
    with pytest.raises(ValueError):
        tools.pool_from_business(value)


def test_business_input_data_is_not_mistaken_for_runtime_envelope(tmp_path):
    value = {'topic': 'T', 'data': {'columns': ['a'], 'rows': [[1]]}}
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(value))
    assert tools._read_bound(str(path)) == value


def test_demo_provenance_cannot_be_erased_by_real_algorithm(monkeypatch):
    monkeypatch.setattr(api.ALGORITHMS['research_topic'], 'run',
                        lambda inputs: {'question': '# 结果', 'is_demo': False})
    result = tools.execute_step('research_topic', tools.seed_pool())
    assert result['mock'] is True
    assert '演示数据' in result['document']


def test_plugin_contains_only_thin_entry_and_calls_shared_runtime(tmp_path, monkeypatch):
    from lazymind.workflow_toolkit import load_workflow_package_tools
    files = {}
    for source in PACKAGE.rglob('*'):
        if not source.is_file() or '__pycache__' in source.parts:
            continue
        relative = source.relative_to(PACKAGE)
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        files[str(relative)] = base64.b64encode(source.read_bytes()).decode()
    assert sorted(str(p.relative_to(PACKAGE)) for p in (PACKAGE / 'scripts').rglob('*.py')) == ['scripts/tools.py']
    assert len((PACKAGE / 'scripts/tools.py').read_text().splitlines()) < 15
    loaded = load_workflow_package_tools({'files': files}, ['run_paper_step'],
                                        'paper-writing', 'isolated-revision', package_root=tmp_path)
    monkeypatch.setattr(tools, 'run_paper_step', lambda: {'status': 'shared-runtime'})
    assert loaded['run_paper_step']() == {'status': 'shared-runtime'}
    assert api.run_algorithm('research_topic', {'topic': 'T'})['question']
