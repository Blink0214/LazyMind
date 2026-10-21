import copy
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / 'workflows/paper-writing'
from lazymind.document_tools.paper import api
from lazymind.document_tools.paper import workflow as tools


@pytest.fixture(autouse=True)
def mock_f1_model(monkeypatch):
    def model(prompt):
        if '"paper_clues"' in prompt:
            return '{"topic": "证据可追溯性"}'
        if 'Input materials (JSON):\n' not in prompt:
            return '论文讨论证据追溯的方法与局限。'
        suffix = '\n\n## 文献反馈与调整依据\n根据综述调整验证路线。' if '"mode": "refine"' in prompt else ''
        return '# 研究选题\n\n## 研究问题\n现有方法能否改善效果？' + suffix

    monkeypatch.setattr(api.ALGORITHMS['research_topic'], 'AutoModel', lambda **_kwargs: model)


@pytest.mark.parametrize('path', [
    ['F1', 'F2', 'F1', 'F2'],
    ['F2', 'F5', 'F6', 'F5', 'F6'],
    ['F4', 'F5'], ['F7'], ['F2', 'F7'],
    ['F2', 'F3', 'F4'], *[[f'F{i}'] for i in range(1, 8)],
])
def test_scenarios_preserve_sources_versions_and_inputs(path):
    pool = tools.seed_pool()
    original = copy.deepcopy(pool)
    for node in path:
        before = copy.deepcopy(pool)
        result = tools.execute_mock(node, pool, '给导师汇报')
        assert pool == before
        pool = result['pool']
        assert result['document'].startswith('# ')
        assert '演示数据' in result['document']
        assert 'MOCK' not in result['document']
        assert pool['materials'][:len(before['materials'])] == before['materials']
        assert result['next'] == tools.ROUTES[node] + ['END']
    assert pool['sequence'] == len(path)
    assert original['materials'] == pool['materials'][:len(original['materials'])]


@pytest.mark.parametrize('start,feedback', [('F2', 'F1'), ('F6', 'F5')])
def test_ten_feedback_rounds_from_direct_entry(start, feedback):
    pool = tools.execute_mock(start, tools.seed_pool())['pool']
    for _ in range(10):
        pool = tools.execute_mock(feedback, pool)['pool']
        before = copy.deepcopy(pool)
        pool = tools.execute_mock(start, pool)['pool']
        assert pool['materials'][:len(before['materials'])] == before['materials']
        kind = 'question' if start == 'F2' else 'manuscript'
        assert before['current'][kind] in pool['history'][-1]['inputs']
        assert tools.material(pool, 'evidence' if start == 'F2' else 'review')['content']


@pytest.mark.parametrize('count', [1, 2])
def test_single_multiple_paper_reflection(count):
    pool = tools.seed_pool()
    tools.material(pool, 'papers')['content'] = tools.material(pool, 'papers')['content'][:count]
    result = tools.execute_mock('F7', pool)
    assert f'{count} 篇' in result['document']


def test_edited_artifact_is_imported_as_new_version_and_used_for_review():
    pool = tools.execute_mock('F5', tools.seed_pool())['pool']
    before = copy.deepcopy(pool)
    edited = '# 用户确认的修订稿\n保留该句'
    selected = tools.pool_from_inputs({'write_document_pool': pool, 'write_document_document': edited})
    assert pool == before
    assert tools.material(selected, 'manuscript')['content'] == edited
    assert selected['current']['manuscript']['version'] == pool['current']['manuscript']['version'] + 1
    result = tools.execute_mock('F6', selected)
    assert selected['current']['manuscript'] in result['pool']['history'][-1]['inputs']


def test_feedback_snapshot_wins_over_old_upstream_pool():
    seed = tools.seed_pool()
    revised = tools.execute_mock('F5', tools.execute_mock('F6', seed)['pool'])['pool']
    selected = tools.pool_from_inputs({'seed_pool': seed, 'recovery_review_pool': revised,
                                      'recovery_review_document': '# 修改后的正文'})
    assert selected['sequence'] == 2
    assert tools.material(selected, 'manuscript')['content'] == '# 修改后的正文'
    assert tools.execute_mock('F6', selected)['pool']['sequence'] == 3


@pytest.mark.parametrize('node,kind', [('F4', 'data'), ('F6', 'manuscript'), ('F7', 'papers')])
def test_missing_input_fails_without_mutation_then_can_resume(node, kind):
    pool = tools.seed_pool()
    ref = pool['current'].pop(kind)
    before = copy.deepcopy(pool)
    with pytest.raises(tools.MissingMaterials, match='缺少材料'):
        tools.execute_mock(node, pool)
    assert pool == before
    pool['current'][kind] = ref
    assert tools.execute_mock(node, pool)['status'] == 'completed'


@pytest.mark.parametrize('corrupt', [
    lambda p: p.update(schema='invalid'),
    lambda p: p.update(sequence=True),
    lambda p: p.update(sequence=5),
    lambda p: p['current']['data'].update(version=999),
    lambda p: p['materials'].append(copy.deepcopy(p['materials'][0])),
    lambda p: p['materials'][0].update(mock='false'),
    lambda p: p['materials'][0].update(content='not papers'),
    lambda p: p['history'].append({'node': 'F2', 'inputs': 'bad', 'outputs': []}),
])
def test_invalid_input_rejected(corrupt):
    pool = tools.seed_pool()
    corrupt(pool)
    with pytest.raises(ValueError):
        tools.execute_mock('F2', pool)


@pytest.mark.parametrize('a,b', [('F1','F3'), ('F1','F5'), ('F3','F5'), ('F4','F3'), ('F6','F2')])
def test_forbidden_execution_edges(a, b):
    pool = tools.execute_mock(a, tools.seed_pool())['pool']
    with pytest.raises(ValueError, match='不允许'):
        tools.execute_mock(b, pool)


def test_failed_or_invalid_algorithm_does_not_publish_or_change_input(monkeypatch):
    pool = tools.seed_pool()
    before = copy.deepcopy(pool)
    with pytest.raises(RuntimeError, match='模拟执行失败'):
        tools.execute_mock('F5', pool, fail=True)
    assert pool == before
    assert tools.execute_mock('F5', pool)['status'] == 'completed'
    monkeypatch.setattr(api.ALGORITHMS['write_document'], 'run', lambda inputs: {'manuscript': 'x'})
    with pytest.raises(ValueError, match='输出'):
        tools.execute_mock('F5', pool)
    assert pool == before


def test_algorithm_cannot_publish_wrong_material_kind(monkeypatch):
    monkeypatch.setattr(api.ALGORITHMS['write_document'], 'run', lambda inputs: {'review': 'wrong output', 'is_demo': False})
    with pytest.raises(ValueError, match='输出'):
        tools.execute_mock('F5', tools.seed_pool())


def test_edited_experiment_report_keeps_simulated_result_and_history():
    pool = tools.execute_mock('F3', tools.seed_pool())['pool']
    selected = tools.pool_from_inputs({'experiment_pool': pool, 'experiment_document': '# 用户修改的实验方案'})
    experiment = tools.material(selected, 'experiment')
    assert experiment['content']['report'] == '# 用户修改的实验方案'
    assert experiment['content']['executed'] is False
    assert experiment['version'] == 2
    assert tools.execute_mock('F4', selected)['status'] == 'completed'


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    ctx = SimpleNamespace(params={}, workspace_path=str(tmp_path))
    batches = []
    context_module = ModuleType('lazymind.chat.engine.subagent.context')
    context_module.require_context = lambda: ctx
    artifact_module = ModuleType('lazymind.chat.engine.subagent.tools')
    artifact_module.save_artifacts = lambda items: batches.append(copy.deepcopy(items)) or {'status': 'ok'}
    monkeypatch.setitem(sys.modules, context_module.__name__, context_module)
    monkeypatch.setitem(sys.modules, artifact_module.__name__, artifact_module)
    return ctx, batches


def test_runtime_wrapper_uses_host_inputs_and_saves_all_declared_outputs(runtime, tmp_path):
    ctx, batches = runtime
    workflow = yaml.safe_load((PACKAGE / 'workflow.yaml').read_text())
    state = yaml.safe_load((PACKAGE / 'scenario/state.yml').read_text())
    assert workflow['runtime']['transactional_outputs']
    ctx.params = {'step_id': 'prepare', 'user_input': '[paper:demo]'}
    tools.run_paper_step()
    seed = tools._read_bound(batches[-1][0]['value'])
    path = tmp_path / 'seed.json'
    path.write_text(json.dumps({'data': seed}))
    for step in ['research_topic', 'literature_review', 'experiment', 'data_analysis', 'write_document', 'peer_review', 'reading_reflection']:
        ctx.params = {'step_id': step, 'remote_inputs': {'seed_pool': str(path)}}
        tools.run_paper_step()
        assert {item['key'] for item in batches[-1]} == {
            item['material'] for item in state['steps'][step]['outputs']}
        document = next(item for item in batches[-1] if item['key'].endswith('_document'))
        assert document['content_type'] == 'file'
        assert Path(document['value']).name == tools.STEP_LABELS[step] + '.md'
        assert '演示数据' in tools._read_bound(document['value'])
    ctx.params = {'step_id': 'finish'}
    tools.run_paper_step()
    assert batches[-1][0]['key'] == 'finish_document'


def test_runtime_failure_retry_and_bad_external_input_never_fallback(runtime, tmp_path):
    ctx, batches = runtime
    path = tmp_path / 'pool.json'
    path.write_text(json.dumps(tools.seed_pool()))
    ctx.params = {'step_id': 'write_document', 'remote_inputs': {'seed_pool': str(path)}, 'user_input': '[mock:fail]'}
    with pytest.raises(RuntimeError):
        tools.run_paper_step()
    assert not batches
    ctx.params['runtime_instruction'] = '[mock:retry]'
    tools.run_paper_step()
    assert len(batches) == 1
    path.write_text('{}')
    ctx.params = {'step_id': 'prepare', 'remote_inputs': {'paper_input': str(path)}}
    with pytest.raises(ValueError):
        tools.run_paper_step()
    assert len(batches) == 1


def test_f1_direct_entry_uses_conversation_pdfs_and_publishes_pool(runtime, monkeypatch):
    ctx, batches = runtime
    papers = [{'id': 'P1', 'title': '论文一', 'text': '<!-- page:1 -->\n证据一'},
              {'id': 'P2', 'title': '论文二', 'text': '<!-- page:1 -->\n证据二'}]
    monkeypatch.setattr(tools, '_conversation_papers', lambda history: papers if history else [])
    ctx.params = {'step_id': 'research_topic', 'user_input': '根据两篇论文研究证据追溯',
                  'history_files_per_turn': {'1': ['/uploads/论文一.pdf'], '2': ['/uploads/论文二.pdf']}}
    tools.run_paper_step()
    outputs = {item['key']: item['value'] for item in batches[-1]}
    pool = tools._read_bound(outputs['research_topic_pool'])
    assert pool['topic'] == '证据可追溯性'
    assert tools.material(pool, 'papers')['content'] == [
        {**paper, 'abstract': '论文讨论证据追溯的方法与局限。'} for paper in papers
    ]
    assert pool['history'][0]['node'] == 'F1'
    assert '## 研究问题' in tools._read_bound(outputs['research_topic_document'])


def test_f1_direct_entry_without_pdfs_creates_unverified_question(runtime):
    ctx, batches = runtime
    ctx.params = {'step_id': 'research_topic', 'user_input': '研究生成答案的证据可追溯性'}
    tools.run_paper_step()
    outputs = {item['key']: item['value'] for item in batches[-1]}
    pool = tools._read_bound(outputs['research_topic_pool'])
    assert pool['topic'] == '证据可追溯性'
    assert tools.material(pool, 'papers') is None
    assert '## 研究问题' in tools._read_bound(outputs['research_topic_document'])


def test_f1_direct_demo_without_pdfs_keeps_upstream_demo(runtime, monkeypatch):
    ctx, batches = runtime
    monkeypatch.setattr(tools.research_topic, 'infer_topic', lambda *_args: pytest.fail('demo topic was inferred'))
    ctx.params = {'step_id': 'research_topic', 'user_input': '[paper:demo]'}
    tools.run_paper_step()
    outputs = {item['key']: item['value'] for item in batches[-1]}
    pool = tools._read_bound(outputs['research_topic_pool'])
    assert tools.material(pool, 'papers')['mock'] is True
    assert '演示数据' in tools._read_bound(outputs['research_topic_document'])


def test_f1_real_pdf_overrides_demo_marker(runtime, monkeypatch):
    ctx, batches = runtime
    monkeypatch.setattr(tools, '_conversation_papers', lambda _history: [
        {'id': 'P1', 'title': '论文一', 'text': '真实论文正文'},
    ])
    ctx.params = {'step_id': 'research_topic', 'user_input': '[paper:demo] 根据上传论文选题',
                  'history_files_per_turn': {'1': ['/uploads/论文一.pdf']}}
    tools.run_paper_step()
    outputs = {item['key']: item['value'] for item in batches[-1]}
    pool = tools._read_bound(outputs['research_topic_pool'])
    assert tools.material(pool, 'papers')['mock'] is False
    assert tools.material(pool, 'papers')['content'][0]['id'] == 'P1'
    assert '演示数据' not in tools._read_bound(outputs['research_topic_document'])


def test_f1_pdf_resources_are_scoped_deduplicated_and_parsed(tmp_path, monkeypatch):
    from lazymind.chat.engine.tools.file_resources import ingest, resolver, store

    monkeypatch.setattr(store, 'workspace_for_request', lambda: str(tmp_path))
    resolved = {}
    for file_id, filename, body in [('P1', '论文一.pdf', '证据一'), ('P2', '论文二.pdf', '证据二')]:
        path = tmp_path / (file_id + '.md')
        path.write_text(body)
        resolved[file_id] = SimpleNamespace(kind='file_resource', file_id=file_id,
                                            display_name=filename, path=str(path))
    calls = []

    def cached_pdf(path, *, turn_seq, store):
        calls.append((Path(path).name, turn_seq))
        return {'file_id': Path(path).stem.replace('论文一', 'P1').replace('论文二', 'P2'),
                'parse_status': 'ready'}

    monkeypatch.setattr(ingest, 'ingest_pdf_file', cached_pdf)
    monkeypatch.setattr(resolver, 'resolve_text_target', lambda file_id, **_kwargs: resolved[file_id])
    papers = tools._conversation_papers({
        '2': ['/uploads/论文二.pdf', '/uploads/说明.txt'],
        '1': ['/uploads/论文一.pdf'],
        '3': ['/uploads/论文一.pdf'],
    })
    assert [(paper['id'], paper['title'], paper['text']) for paper in papers] == [
        ('P1', '论文一', '证据一'), ('P2', '论文二', '证据二'),
    ]
    assert calls == [('论文一.pdf', 1), ('论文二.pdf', 2), ('论文一.pdf', 3)]
    assert '/uploads/' not in json.dumps(papers, ensure_ascii=False)


def test_f1_explicit_and_saved_inputs_do_not_rescan_conversation(runtime, tmp_path, monkeypatch):
    ctx, batches = runtime
    monkeypatch.setattr(tools, '_conversation_papers', lambda _history: pytest.fail('PDF history was rescanned'))
    paper_input = tmp_path / 'paper-input.json'
    paper_input.write_text(json.dumps({'topic': '显式主题', 'papers': [
        {'id': 'P1', 'title': '指定论文', 'text': '指定正文'},
    ]}, ensure_ascii=False))
    ctx.params = {'step_id': 'research_topic', 'user_input': '研究选题',
                  'remote_inputs': {'paper_input': str(paper_input)},
                  'history_files_per_turn': {'1': ['/uploads/其他论文.pdf']}}
    tools.run_paper_step()
    first = {item['key']: item['value'] for item in batches[-1]}
    assert tools.material(tools._read_bound(first['research_topic_pool']), 'papers')['content'][0]['id'] == 'P1'

    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps(tools.pool_from_business({'topic': '已确认主题'}), ensure_ascii=False))
    ctx.params['remote_inputs'] = {'seed_pool': str(seed)}
    tools.run_paper_step()
    second = {item['key']: item['value'] for item in batches[-1]}
    assert tools._read_bound(second['research_topic_pool'])['topic'] == '已确认主题'


def test_f1_explicit_topic_wins_while_using_conversation_pdfs(runtime, tmp_path, monkeypatch):
    ctx, batches = runtime
    topic = tmp_path / 'topic.txt'
    topic.write_text('用户指定的研究主题')
    monkeypatch.setattr(tools, '_conversation_papers', lambda _history: [
        {'id': 'P1', 'title': '论文一', 'text': '论文正文'},
    ])
    monkeypatch.setattr(tools.research_topic, 'infer_topic', lambda *_args: pytest.fail('主题不应重新推断'))
    ctx.params = {'step_id': 'research_topic', 'user_input': '根据附件选题',
                  'remote_inputs': {'paper_topic': str(topic)},
                  'history_files_per_turn': {'1': ['/uploads/论文一.pdf']}}
    tools.run_paper_step()
    outputs = {item['key']: item['value'] for item in batches[-1]}
    pool = tools._read_bound(outputs['research_topic_pool'])
    assert pool['topic'] == '用户指定的研究主题'
    assert tools.material(pool, 'papers')['content'][0]['id'] == 'P1'


def test_f1_pdf_scan_does_not_change_direct_f2_entry(runtime, tmp_path, monkeypatch):
    ctx, batches = runtime
    monkeypatch.setattr(tools, '_conversation_papers', lambda _history: pytest.fail('F2 scanned PDFs'))
    paper_input = tmp_path / 'paper-input.json'
    paper_input.write_text(json.dumps({'topic': '文献主题'}, ensure_ascii=False))
    ctx.params = {'step_id': 'literature_review', 'user_input': '先做综述',
                  'remote_inputs': {'paper_input': str(paper_input)},
                  'history_files_per_turn': {'1': ['/uploads/研究论文.pdf']}}
    tools.run_paper_step()
    assert {item['key'] for item in batches[-1]} == {'literature_review_pool', 'literature_review_document'}


def test_f1_unreadable_pdf_stops_before_publication(runtime, monkeypatch):
    ctx, batches = runtime
    def unreadable(_history):
        raise tools.MissingMaterials('论文附件无法解析')
    monkeypatch.setattr(tools, '_conversation_papers', unreadable)
    ctx.params = {'step_id': 'research_topic', 'user_input': '研究证据追溯',
                  'history_files_per_turn': {'1': ['/uploads/坏论文.pdf']}}
    with pytest.raises(tools.MissingMaterials, match='论文附件无法解析'):
        tools.run_paper_step()
    assert not batches


def test_prepare_without_input_does_not_invent_materials(runtime):
    ctx, batches = runtime
    ctx.params = {'step_id': 'prepare', 'user_input': '帮我审稿'}
    with pytest.raises(tools.MissingMaterials, match='请提供'):
        tools.run_paper_step()
    assert not batches


def test_business_manuscript_entry_publishes_visible_preparation_then_review(runtime, tmp_path):
    ctx, batches = runtime
    path = tmp_path / 'business.json'
    path.write_text(json.dumps({'topic': '我的研究', 'manuscript': '# 已有稿件\n检查方法'}))
    ctx.params = {'step_id': 'prepare', 'remote_inputs': {'paper_input': str(path)}}
    tools.run_paper_step()
    outputs = {item['key']: item['value'] for item in batches[-1]}
    summary = Path(outputs['prepare_document']).read_text()
    assert '我的研究' in summary and '稿件' in summary
    assert '演示数据' not in summary and 'pool' not in summary
    pool = tools._read_bound(outputs['seed_pool'])
    assert set(pool['current']) == {'manuscript'}
    ctx.params = {'step_id': 'peer_review', 'remote_inputs': {'seed_pool': outputs['seed_pool']}}
    tools.run_paper_step()
    assert {item['key'] for item in batches[-1]} == {'peer_review_pool', 'peer_review_document'}


def test_config_logical_edges_and_no_real_external_calls():
    state = yaml.safe_load((PACKAGE / 'scenario/state.yml').read_text())
    for step in state['steps'].values():
        # The wrapper owns runtime parameters. Do not let a focused UI tab or
        # old launch request redefine a deterministic Mock tool invocation.
        assert '{{runtime_instruction}}' not in step['prompt']
        assert '{{user_input}}' not in step['prompt']
        assert 'Do not substitute chat text for the tool call.' in step['prompt']
        assert 'The currently viewed tab is not the execution node.' in step['prompt']
        assert step['prompt'].isascii()
        assert step['acceptance_criteria'].isascii()
    assert all(edge['when'].isascii() for edges in state['transitions'].values() for edge in edges)
    config = yaml.safe_load((PACKAGE / 'workflow.yaml').read_text())
    assert config['when_to_use'].isascii()
    assert config['name'] == '论文写作'
    for step, node in tools.STEP_NODE.items():
        actual = {tools.STEP_NODE[e['to']] for e in state['transitions'][step] if e['to'] != 'finish'}
        if step == 'refine_topic':
            actual.add('F2')  # Native rewind, not a cyclic control edge.
        if step == 'revise_document':
            actual.add('F6')
        assert actual == set(tools.ROUTES[node])
        assert state['steps'][step]['mode'] == 'human'
        assert state['steps'][step]['tools'] == ['run_paper_step']


@pytest.mark.parametrize('content', [
    'x' * 490 + '<a id="block-user-long-id"></a>\\n## Edited',
    '\u0060\u0060\u0060text\\nE2E-EDIT\\n\u0060\u0060\u0060\\n<a id="anchor"></a>' * 20,
])
def test_feedback_text_reaches_algorithm_unchanged(content, monkeypatch):
    pool = tools.execute_mock('F2', tools.seed_pool())['pool']
    pool = tools.pool_from_inputs({'literature_review_pool': pool, 'literature_review_document': content})
    seen = []
    module = api.ALGORITHMS['research_topic']
    original = module.run
    def capture(inputs):
        seen.append(copy.deepcopy(inputs))
        return original(inputs)
    monkeypatch.setattr(module, 'run', capture)
    tools.execute_step('refine_topic', pool)
    assert seen[0]['evidence'] == content
    assert seen[0]['mode'] == 'refine'


@pytest.mark.parametrize('update', [
    {'current_user_input': '只检查引用和局限'},
    {'current_user_input': '', 'retry_hint': "User is currently viewing Workflow tab 'data_analysis'. "
     'Recovery request for this rerun only: 只检查引用和局限'},
])
def test_real_core_request_fields_override_launch_intent(runtime, tmp_path, update):
    ctx, batches = runtime
    pool = tools.execute_mock('F5', tools.seed_pool())['pool']
    path = tmp_path / 'pool.json'
    path.write_text(json.dumps(pool))
    ctx.params = {'step_id': 'peer_review', 'remote_inputs': {'write_document_pool': str(path)},
                  'user_input': '旧要求：检查全文', 'launch_user_input': '旧要求：检查全文', **update}
    tools.run_paper_step()
    assert tools._read_bound(batches[-1][0]['value'])['history'][-1]['request'] == '只检查引用和局限'
    assert '旧要求' not in tools._read_bound(batches[-1][1]['value'])


def test_public_workflow_presentation_and_named_outputs():
    workflow = yaml.safe_load((PACKAGE / 'workflow.yaml').read_text())
    assert workflow['id'] == 'paper-writing'
    assert workflow['name'] == '论文写作'
    assert {step['id'] for step in workflow['steps']} == {
        'research_topic', 'literature_review', 'refine_topic', 'experiment',
        'data_analysis', 'write_document', 'peer_review', 'revise_document',
        'reading_reflection', 'finish',
    }
    assert not any(re.match(r'f\d(?:_|$)', slot['id']) for slot in workflow['slots'])
    ui = workflow['ui']
    assert not ui['task_presentation'].get('grouped')
    assert set(ui['task_presentation']['hidden_steps']) == {'finish'}
    assert len(ui['tabs']) == 9
    assert ui['tabs'][0]['step_id'] == 'research_topic'
    assert 'prepare_document' not in ui['slots']
    assert 'prepare' not in yaml.safe_load((PACKAGE / 'scenario/state.yml').read_text())['steps']
    for tab in ui['tabs']:
        assert tab['id'] not in ui['task_presentation']['hidden_steps']
        assert len(tab['label']) <= 6
        assert not re.search(r'F\d|mock', tab['label'], re.I)
        assert tab['allow_download'] is True
    for slot in workflow['slots']:
        if slot['id'].endswith('_pool'):
            assert slot['exposed'] is False
            assert slot['type'] == 'file'
        if slot['id'].endswith('_document') and not slot.get('external'):
            assert slot['type'] == 'file'
    for step in tools.STEP_NODE:
        assert ui['slots'][step + '_document'] == {'widgetType': 'text-markdown', 'readOnly': False}


def test_no_ppt_node_artifact_or_prompt_in_paper_package():
    for relative in ('workflow.yaml', 'scenario/state.yml', 'scenario/scenario.md', 'scripts/tools.py'):
        assert 'ppt' not in (PACKAGE / relative).read_text().lower(), relative
    assert 'presentation' not in tools.KINDS
    pool = tools.seed_pool()
    before = copy.deepcopy(pool)
    with pytest.raises(ValueError, match='未知节点'):
        tools.execute_mock('PPT', pool)
    assert pool == before


def test_removed_ppt_step_cannot_publish_artifacts(runtime):
    ctx, batches = runtime
    ctx.params = {'step_id': 'ppt'}
    with pytest.raises(ValueError, match='未知 Workflow 步骤'):
        tools.run_paper_step()
    assert not batches


def test_recovery_failure_and_retry_use_core_retry_hint(runtime, tmp_path):
    ctx, batches = runtime
    path = tmp_path / 'pool.json'
    path.write_text(json.dumps(tools.seed_pool()))
    ctx.params = {'step_id': 'experiment', 'remote_inputs': {'seed_pool': str(path)},
                  'user_input': '旧的启动请求', 'current_user_input': '',
                  'retry_hint': 'Recovery request for this rerun only: 重跑 experiment [mock:fail]'}
    with pytest.raises(RuntimeError, match='模拟执行失败'):
        tools.run_paper_step()
    assert not batches
    ctx.params['user_input'] = '[mock:fail]'
    ctx.params['retry_hint'] = 'Recovery request for this rerun only: 重试 experiment [mock:retry]'
    tools.run_paper_step()
    assert len(batches) == 1
    assert tools._read_bound(batches[0][0]['value'])['history'][-1]['request'] == '重试 experiment [mock:retry]'
