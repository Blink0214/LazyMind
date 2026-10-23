"""Outline boundary and edited-artifact handoff regressions (not live acceptance)."""
import copy
from unittest.mock import Mock

import pytest

from lazymind.document_tools.paper import contracts, workflow
from lazymind.document_tools.paper.writing import write_outline
from lazymind.document_tools.paper.runtime_events import bind_runtime_events

OUTLINE = '# Test paper\n\n## Method\n\n## Limitations'


def test_outline_step_only_calls_public_outline_generation(monkeypatch):
    toolkit = Mock()
    def stream_outline(**kwargs):
        kwargs['on_delta']('# Test paper')
        kwargs['on_delta']('\n\n')
        kwargs['on_delta']('## Method\n\n## Limitations')
        return OUTLINE
    toolkit.stream_outline.side_effect = stream_outline
    prepared = {'writing_task': {'constraints': {}}, 'writing_context': {},
                'outline': None, 'charts': []}
    monkeypatch.setattr(write_outline, 'WriterCreateToolkit', lambda: toolkit)
    monkeypatch.setattr(write_outline, 'prepare_writer_context', lambda *a, **k: copy.deepcopy(prepared))
    inputs = contracts.validate_input('write_outline', {'topic': 'Test', 'question': 'Compare methods'})
    write_outline.validate_inputs(inputs)
    emitted = []
    with bind_runtime_events(emitted.append, 'write_outline_document') as events:
        result = write_outline.run(inputs)
        events.complete()
    assert any(event.get('current_phase') == '正在输出大纲内容' for event in emitted)
    assert emitted[-1]['current_phase'] == '大纲已保存，等待确认'
    deltas = [event for event in emitted if event.get('type') == 'artifact_stream']
    assert deltas
    assert ''.join(event['delta'] for event in deltas) == OUTLINE
    contracts.validate_output('write_outline', result, inputs)
    assert result == {'outline': OUTLINE, 'is_demo': False}
    assert [call[0] for call in toolkit.method_calls] == ['stream_outline']


def test_confirmed_outline_is_reused_without_regeneration():
    toolkit = Mock()
    prepared = {'writing_task': {}, 'writing_context': {}, 'outline': OUTLINE, 'charts': []}
    assert write_outline.prepare_outline(prepared, toolkit) == OUTLINE
    toolkit.generate_outline.assert_not_called()


def test_edited_outline_slot_overrides_pool_and_preserves_request(monkeypatch):
    pool = workflow.pool_from_business({'topic': 'Test', 'question': 'Compare methods', 'request': 'Use Chinese'})
    monkeypatch.setattr(workflow, 'run_algorithm', lambda *a, **k: {'outline': OUTLINE, 'is_demo': False})
    result = workflow.execute_step('write_outline', pool, 'Use Chinese; mark missing evidence')
    edited = OUTLINE.replace('Limitations', 'Limitations and future work')
    resumed = workflow.pool_from_inputs({'write_outline_pool': result['pool'], 'write_outline_document': edited})
    assert workflow.material(resumed, 'outline')['content'] == edited
    assert workflow.material(resumed, 'manuscript') is None
    assert workflow.material(result['pool'], 'outline')['content'] == OUTLINE
    request = workflow._step_request('write_document', resumed, {'current_user_input': ''}, {'write_outline_pool': 'bound'})
    assert request == 'Use Chinese; mark missing evidence'


def test_outline_step_rejects_existing_manuscript():
    inputs = contracts.validate_input('write_outline', {'topic': 'Test', 'manuscript': OUTLINE})
    with pytest.raises(ValueError, match='existing manuscript'):
        write_outline.validate_inputs(inputs)
