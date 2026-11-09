"""Opt-in, billable tests of real Writer toolkits; skipped by default.

Run from the repo root only when model calls are explicitly authorized:
    PAPER_WRITING_LIVE=1 PYTHONPATH=algorithm/lazyllm:algorithm \
        .venv/bin/python -m pytest tests/algorithm/chat/workflows/test_paper_writing_live.py -v

Configure static roles via LAZYMIND_MODEL_CONFIG_PATH, or provide request JSON
with llm.source/model via PAPER_WRITING_LIVE_CONFIG (dynamic injection).
Alternatively PAPER_WRITING_LIVE_PROFILE=public-writer uses configured provider
credentials and PAPER_WRITING_LIVE_SOURCE/MODEL/BASE_URL. No response mocks or
automatic retries; failures remain failures. pytest does not load .env itself.
Synthetic input/output/manuscript evidence goes to tmp_path, never credentials.
These tests exercise algorithms, not workflow scheduling or frontend behavior.
"""
import copy
import json
import os
from pathlib import Path

import lazyllm
import pytest

from lazymind.config import config
from lazymind.document_tools.paper import api, peer_review
from lazymind.model_config import get_model_role_runtime_identity, inject_model_config
from lazyllm.tools.writer.utils.serialization import parse_markdown_sections

pytestmark = pytest.mark.skipif(
    os.environ.get('PAPER_WRITING_LIVE') != '1',
    reason='Real model calls are opt-in: set PAPER_WRITING_LIVE=1.',
)
FIXTURES = Path(__file__).resolve().parents[3] / 'fixtures' / 'paper-writing'


@pytest.fixture
def live_model(monkeypatch):
    """Use configured static roles, or inject an explicit request config file.

    Restore routing and credentials after the test, including failed calls.
    Never persist the model config alongside manuscript evidence.
    """
    profile = os.environ.get('PAPER_WRITING_LIVE_PROFILE', 'runtime')
    if profile == 'public-writer':
        # Same real constructor and key lookup as LazyLLM's charge Writer tests.
        # Replace only the Toolkit's model factory, never model/HTTP responses.
        from lazymind.document_tools import revision, writing
        source = os.environ.get('PAPER_WRITING_LIVE_SOURCE', 'qwen')
        model_name = os.environ.get('PAPER_WRITING_LIVE_MODEL') or ('qwen-turbo' if source == 'qwen' else None)
        base_url = os.environ.get('PAPER_WRITING_LIVE_BASE_URL')
        api_key = lazyllm.config[f'{source}_api_key']

        def public_writer_model(model):
            if model != 'llm':
                raise ValueError('The public-writer test profile supports only the llm role.')
            return lazyllm.OnlineChatModule(
                source=source, model=model_name, api_key=api_key, stream=False, url=base_url,
            )

        if not api_key:
            pytest.fail(f'Public Writer tests require configured {source}_api_key.', pytrace=False)
        monkeypatch.setattr(writing, 'AutoModel', public_writer_model)
        monkeypatch.setattr(revision, 'AutoModel', public_writer_model)
        yield
        return
    if profile != 'runtime':
        pytest.fail('Unknown PAPER_WRITING_LIVE_PROFILE; use runtime or public-writer.', pytrace=False)
    injected_path = os.environ.get('PAPER_WRITING_LIVE_CONFIG')
    previous = copy.deepcopy(lazyllm.globals['config'])
    old_capabilities = copy.deepcopy(lazyllm.globals.get('lazymind_model_capabilities'))
    old_model_path = config['model_config_path']
    old_auto_path = lazyllm.config['auto_model_config_map_path']
    try:
        if injected_path:
            # Only a request-scoped llm role is needed for paper writing.
            request_config = json.loads(Path(injected_path).read_text(encoding='utf-8'))
            role = request_config.get('llm')
            if not isinstance(role, dict) or not role.get('source') or not role.get('model'):
                pytest.fail('PAPER_WRITING_LIVE_CONFIG must contain llm.source and llm.model.', pytrace=False)
            config['model_config_path'] = 'dynamic'
            inject_model_config({'llm': role})
            yield
        else:
            identity = get_model_role_runtime_identity('llm')
            if not identity['source']:
                pytest.fail(
                    'No runtime llm source. Set LAZYMIND_MODEL_CONFIG_PATH to a configured static role file, '
                    'or provide PAPER_WRITING_LIVE_CONFIG for request injection.', pytrace=False,
                )
            yield
    finally:
        lazyllm.globals['config'] = previous
        lazyllm.globals['lazymind_model_capabilities'] = old_capabilities
        config['model_config_path'] = old_model_path
        lazyllm.config['auto_model_config_map_path'] = old_auto_path


def _input(name):
    name = {'write_document': 'writing_materials', 'revise_document': 'review_revision'}[name]
    return json.loads((FIXTURES / (name + '.json')).read_text(encoding='utf-8'))['input']


def _headings(markdown):
    return [(level, path[-1]) for level, path, _, _ in parse_markdown_sections(markdown)]


def _run(source, tmp_path, record_property):
    before = copy.deepcopy(source)
    # Synthetic research fixtures only; no model configuration or credentials.
    (tmp_path / 'input.json').write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding='utf-8')
    record_property('paper_writing_artifacts', str(tmp_path))
    try:
        prepared = copy.deepcopy(source)
        step = 'revise_document' if prepared.get('manuscript') else 'write_document'
        prepared['mode'] = 'revise' if step == 'revise_document' else 'draft'
        if step == 'write_document' and not prepared.get('outline'):
            outline = api.run_algorithm('write_outline', prepared)['outline']
            prepared['outline'] = outline
        result = api.run_algorithm(step, prepared)
    finally:
        assert source == before
    (tmp_path / 'output.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (tmp_path / 'manuscript.md').write_text(result['manuscript'], encoding='utf-8')
    assert set(result) == {'is_demo', 'manuscript'}
    assert result['is_demo'] is False
    headings = _headings(result['manuscript'])
    assert sum(level == 1 for level, _ in headings) == 1
    assert any(level == 2 for level, _ in headings)
    assert not result['manuscript'].lstrip().startswith('```')
    if step == 'write_document':
        result = {**result, 'outline': prepared['outline']}
    return result


@pytest.mark.parametrize('with_outline', [False, True], ids=['materials', 'outline'])
def test_live_draft(with_outline, live_model, tmp_path, record_property):
    source = _input('write_document')
    source['request'] = '写一份简短研究稿，各章节保持简洁。明确单次比较及材料不足，不补造实验或文献。'
    source['outline'] = '# 方法得分比较\n\n## 方法\n\n## 结果\n\n## 局限' if with_outline else None
    result = _run(source, tmp_path, record_property)
    assert _headings(result['outline']) == _headings(result['manuscript'])
    if with_outline:
        assert _headings(result['outline']) == _headings(source['outline'])
    assert '15' in result['manuscript']
    assert any(term in result['manuscript'] for term in ('单次', '一次'))
    assert any(term in result['manuscript'] for term in ('局限', '不足', '限制'))


def test_live_draft_edit(live_model, tmp_path, record_property):
    source = _input('revise_document')
    source.update(mode='revise', review=None, review_data=None, selected_issue_ids=[])
    old = '新方法得分高 15 分。'
    new = '新方法单次得分高 15 分，不能推断统计显著性。'
    source['request'] = f'仅将“{old}”替换为“{new}”，所有其他内容及格式保持原样。'
    result = _run(source, tmp_path, record_property)
    assert result['manuscript'] == source['manuscript'].replace(old, new)
    assert _headings(result['manuscript']) == _headings(source['manuscript'])


@pytest.mark.parametrize('select_all', [False, True], ids=['selected-only', 'multiple-selected'])
def test_live_review_revision(select_all, live_model, tmp_path, record_property):
    source = _input('revise_document')
    first = source['review_data']['issues'][0]
    new_method = '比较两个方法的单次得分；指标定义待补充。'
    first['suggestion'] = f'将引文原句精确替换为“{new_method}”，其他内容及格式保持原样。'
    second = copy.deepcopy(first)
    second.update(id='R2', category='analysis', quote='新方法得分高 15 分。',
                  problem='没有说明单次得分差异不能推断统计显著性。')
    second['location'] = '示例研究 > 结果'
    new_result = '新方法单次得分高 15 分，不能推断统计显著性。'
    second['suggestion'] = f'将引文原句精确替换为“{new_result}”，其他内容及格式保持原样。'
    source['review_data']['issues'].append(second)
    raw = {key: copy.deepcopy(source['review_data'][key]) for key in ('summary', 'issues', 'limitations')}
    for issue in raw['issues']:
        issue.pop('id', None)
    rendered = peer_review.build_output(peer_review.ReviewResult.model_validate(raw), source)
    source['review'], source['review_data'] = rendered['review'], rendered['review_data']
    source['request'] = '按选中意见落实精确替换，其他内容及格式保持原样，不增加解释或修订说明。'
    if select_all:
        source['selected_issue_ids'].append('R2')
    result = _run(source, tmp_path, record_property)
    expected = source['manuscript'].replace(first['quote'], new_method)
    if select_all:
        expected = expected.replace(second['quote'], new_result)
    assert result['manuscript'] == expected
    assert _headings(result['manuscript']) == _headings(source['manuscript'])
