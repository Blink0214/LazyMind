"""Offline lifecycle contracts shared by every paper step, including multi-artifact outputs."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import lazyllm
import pytest

from lazymind.document_tools.paper import api, contracts, research_topic, workflow
from lazymind.document_tools.paper.material_bindings import MaterialBindings
from lazymind.document_tools.paper.runtime_events import (
    bind_runtime_events, current_events, generate_markdown, StructuredPreview,
)

ROOT = Path(__file__).resolve().parents[4]
STEPS = list(workflow.STEP_NODE)


@pytest.fixture(autouse=True)
def restore_context():
    previous = lazyllm.globals.get('subagent_ctx')
    yield
    if previous is None:
        lazyllm.globals.pop('subagent_ctx', None)
    else:
        lazyllm.globals['subagent_ctx'] = previous


def test_markdown_stream_filters_reasoning_and_restores_split_citations_before_return():
    emitted = []
    body = '# 研究选题\n\n## 研究问题\n证据[P1]。'
    bindings = MaterialBindings([{'id': 'P1'}])

    def share(*, stream):
        def generate(prompt):
            stream['_stream_sink']({'tag': 'think', 'delta': 'private reasoning'})
            for delta in ['<thi', 'nk>private ', 'reasoning</think>', '# 研究选题\n\n## 研究问题\n证据', '[', 'M', '1', ']', '。']:
                stream['_stream_sink']({'tag': 'text', 'delta': delta})
            assert ''.join(e['delta'] for e in emitted if e['type'] == 'artifact_stream') == body
            return '<think>private reasoning</think>' + body.replace('[P1]', '[M1]')
        return generate

    with bind_runtime_events(emitted.append, 'research_topic_document') as events:
        assert generate_markdown(SimpleNamespace(share=share), 'prompt', bindings=bindings) == body
        events.completed_preview(body)
        events.complete()
    assert 'private reasoning' not in json.dumps(emitted)
    assert sum(e['type'] == 'artifact_stream_start' for e in emitted) == 1
    assert current_events() is None


def test_full_result_replaces_preview_once_and_can_be_withdrawn_after_generation_ends():
    emitted = []
    with pytest.raises(ValueError, match='publication rejected'):
        with bind_runtime_events(emitted.append, 'experiment_document') as events:
            events.delta('partial')
            events.completed_preview('# 完整实验报告')
            events.completed_preview('# 完整实验报告')
            raise ValueError('publication rejected')
    latest = [e for e in emitted if e.get('stream_id') == next(
        e['stream_id'] for e in reversed(emitted) if e['type'] == 'artifact_stream_start')]
    assert [e['delta'] for e in latest if e['type'] == 'artifact_stream'] == ['# 完整实验报告']
    assert latest[-1]['type'] == 'artifact_stream_abort'
    assert not any(e['type'] == 'artifact' for e in emitted)


def test_stage_failure_and_retry_keep_independent_public_records():
    emitted = []
    with bind_runtime_events(emitted.append, 'data_analysis_document') as events:
        events.progress(5, '读取材料')
        events.begin_stage('first', '执行分析', 40)
        events.begin_stage('figure', '生成图表', 50)
        events.end_stage('first', ValueError('invalid result'))
        events.begin_stage('second', '修正分析', 45)
        events.end_stage('second')
        events.end_stage('figure')
        events.complete()
    snapshots = [e for e in emitted if e['type'] == 'progress']
    assert [e['progress'] for e in snapshots] == sorted(e['progress'] for e in snapshots)
    assert max(e['progress'] for e in snapshots) == 99  # Core owns authoritative success.
    stages = {s['step_id']: s for event in snapshots for s in event['public_display']['process_steps']}
    assert stages['paper-first']['status'] == 'failed'
    assert stages['paper-second']['status'] == stages['paper-figure']['status'] == 'succeeded'
    assert stages['paper-first']['finished_at'] and stages['paper-first']['elapsed_ms'] >= 0


def test_structured_preview_replaces_revised_fields_and_does_not_emit_raw_json():
    emitted = []
    with bind_runtime_events(emitted.append, 'peer_review_document') as events:
        preview = StructuredPreview(lambda value: '# 审稿\n' + value.get('summary', ''))
        preview({'tag': 'think', 'delta': 'private'})
        preview({'tag': 'text', 'delta': '{"summary":"意见'})
        preview.flush()
        assert any(e['type'] == 'artifact_stream' for e in emitted)
        preview({'tag': 'text', 'delta': '已形成"}'})
        preview.flush()
        events.complete()
    assert 'private' not in json.dumps(emitted)
    assert all(not e['delta'].startswith('{') for e in emitted if e['type'] == 'artifact_stream')


@pytest.mark.parametrize('step', STEPS)
def test_all_steps_use_shared_progress_preview_and_real_artifact_publication(step, monkeypatch, tmp_path):
    from lazymind.chat.engine.subagent.context import SubAgentContext
    if step == 'write_outline':
        example = {'input': {'topic': '方法比较', 'question': '如何比较两种方法？'},
                   'output': {'outline': '# 方法比较\n\n## 方法\n\n## 局限', 'is_demo': False}}
    else:
        example = json.loads((ROOT / 'tests/fixtures/paper-writing' / (step + '.json')).read_text())
    for paper in example['output'].get('papers', []):
        paper.setdefault('citation', paper['title'] + '[Z].')
    if step == 'revise_document':
        from lazymind.document_tools.paper.writing import revise_document
        monkeypatch.setattr(revise_document, 'resolve_review_issues', lambda request, review: [])
    value = {key: copy.deepcopy(item) for key, item in example['input'].items() if key != 'mode'}
    pool = workflow.pool_from_business(value)
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps(pool, ensure_ascii=False))
    module = contracts.ALIASES.get(step, step)
    monkeypatch.setattr(research_topic, 'prepare_papers', lambda papers, *args: [
        {**paper, 'abstract': paper.get('abstract') or paper.get('text') or 'Summary'} for paper in papers])

    def generate(inputs, **kwargs):
        assert current_events() is not None
        current_events().progress(35, '生成本步骤内容')
        current_events().delta('# 生成中的预览')
        return {key: copy.deepcopy(example['output'][key]) for key in ('is_demo', *contracts.OUTPUTS[module])}

    monkeypatch.setattr(api.ALGORITHMS[module], 'run', generate)
    slots = [f'{step}_pool', f'{step}_document']
    if step == 'experiment':
        slots += ['experiment_code', 'experiment_results']
    if step == 'data_analysis':
        slots += ['analysis_chart']
    emitted = []
    lazyllm.globals['subagent_ctx'] = SubAgentContext(
        task_id=step, conversation_id='test', agent_type='workflow_step', objective='Test lifecycle',
        workspace_path=str(tmp_path), input_slots=['seed_pool'], output_slots=slots, db=None, emit=emitted.append,
        params={'step_id': step, 'current_user_input': example['input'].get('request') or '按本轮要求完善内容',
                'remote_inputs': {'seed_pool': str(seed)},
                'output_slot_types': dict.fromkeys(slots, 'file')},
    )
    assert workflow.run_paper_step()['status'] == 'completed'
    document = next(e for e in emitted if e['type'] == 'artifact' and e['slot'] == f'{step}_document')
    saved = Path(document['value']['path']).read_text()
    latest_stream = next(e['stream_id'] for e in reversed(emitted)
                         if e['type'] == 'artifact_stream_start' and e['slot'] == f'{step}_document')
    preview = ''.join(e['delta'] for e in emitted if e['type'] == 'artifact_stream' and e['stream_id'] == latest_stream)
    assert preview == saved
    assert any(e['type'] == 'progress' and e.get('public_display') for e in emitted)
    assert current_events() is None


def test_finish_publishes_whole_document_without_a_model_call(tmp_path):
    from lazymind.chat.engine.subagent.context import SubAgentContext
    emitted = []
    lazyllm.globals['subagent_ctx'] = SubAgentContext(
        task_id='finish', conversation_id='test', agent_type='workflow_step', objective='Finish',
        workspace_path=str(tmp_path), input_slots=[], output_slots=['finish_document'], db=None, emit=emitted.append,
        params={'step_id': 'finish', 'output_slot_types': {'finish_document': 'file'}},
    )
    workflow.run_paper_step()
    assert len([e for e in emitted if e['type'] == 'artifact_stream']) == 1
    assert any(e['type'] == 'artifact' for e in emitted)


def test_review_projects_partial_results_while_preserving_strict_review_validation(tmp_path, monkeypatch):
    from lazymind.document_tools.paper import peer_review
    example = json.loads((ROOT / 'tests/fixtures/paper-writing/peer_review.json').read_text())
    emitted = []

    class Reviewer:
        def _structured_output_prompt(self, schema):
            return ''

        def _build_structured_llm(self, prompt, *, stream_output, apply_formatter):
            def generate(_):
                raw = json.dumps(example['model_response'], ensure_ascii=False)
                stream_output['_stream_sink']({'tag': 'text', 'delta': raw})
                assert any(e['type'] == 'artifact_stream' for e in emitted)
                return raw
            return generate

    with bind_runtime_events(emitted.append, 'peer_review_document') as events:
        peer_review.call_once(Reviewer(), 'prompt', peer_review.ReviewResult)
        events.complete()
    assert all(not e['delta'].startswith('{') for e in emitted if e['type'] == 'artifact_stream')
    assert any(s['status'] == 'succeeded' for e in emitted if e['type'] == 'progress'
               for s in e['public_display']['process_steps'])


def test_f4_keeps_diagnostics_and_projects_interpretation_to_shared_document_stream(tmp_path):
    from lazymind.document_tools.paper.analysis import trace
    emitted = []
    value = '结果仅适用于当前样本，仍需补充验证。'
    with bind_runtime_events(emitted.append, 'data_analysis_document') as events:
        with trace.capture(tmp_path, 'analysis', emitted.append):
            with trace.stage('interpretation'):
                preview = trace.response_preview(False, output_format='markdown')
                preview.sink({'tag': 'text', 'delta': value})
                assert any(e['type'] == 'artifact_stream' for e in emitted)
                preview.finish(value)
        events.complete()
    assert list(tmp_path.glob('f4-trace/*/public.json'))
    assert any(e.get('slot') == 'data_analysis_document' for e in emitted)
