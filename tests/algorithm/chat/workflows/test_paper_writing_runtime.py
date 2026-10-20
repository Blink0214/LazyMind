"""Contract check against the real Runtime publisher and configured algorithms."""
import json
from pathlib import Path

import lazyllm
import pytest
import yaml

from lazymind.chat.engine.subagent.context import SubAgentContext, set_context

PACKAGE = Path(__file__).resolve().parents[4] / 'workflows/paper-writing'
from lazymind.document_tools.paper import api
from lazymind.document_tools.paper import workflow as tools


@pytest.mark.parametrize('start,feedback,recovery', [
    ('literature_review', 'refine_topic', 'recovery_research'),
    ('peer_review', 'revise_document', 'recovery_review'),
])
def test_real_artifact_publisher_across_ten_feedback_rounds(tmp_path, start, feedback, recovery, monkeypatch):
    monkeypatch.setattr(api.ALGORITHMS['research_topic'], 'AutoModel', lambda **_kwargs: lambda _prompt:
                        '# 研究选题\n\n## 研究问题\n如何改善效果？\n\n'
                        '## 文献反馈与调整依据\n根据综述调整路线。')
    state = yaml.safe_load((PACKAGE / 'scenario/state.yml').read_text())
    workflow = yaml.safe_load((PACKAGE / 'workflow.yaml').read_text())
    types = {slot['id']: slot['type'] for slot in workflow['slots']}
    previous = lazyllm.globals.get('subagent_ctx')
    events = []
    latest = {}
    offloaded = []

    def run(step, inputs):
        workspace = tmp_path / str(len(events))
        workspace.mkdir()
        bound = {}
        for key, value in inputs.items():
            path = workspace / (key + '.json')
            # Core materializes managed output bytes before the next execution.
            if isinstance(value, dict) and value.get('path'):
                assert Path(value['path']).is_relative_to(tmp_path)
                path.write_bytes(Path(value['path']).read_bytes())
            else:
                path.write_text(json.dumps(value, ensure_ascii=False))
            bound[key] = str(path)
        slots = [item['material'] for item in state['steps'][step]['outputs']]
        emitted = []
        set_context(SubAgentContext(
            task_id='paper-' + workspace.name, conversation_id='paper-test', agent_type='workflow_step',
            objective='Mock contract test', params={'step_id': step, 'remote_inputs': bound,
                'output_slot_types': {slot: types[slot] for slot in slots}},
            workspace_path=str(workspace), input_slots=list(inputs), output_slots=slots,
            db=None, emit=emitted.append,
        ))
        assert tools.run_paper_step()['status'] == 'completed'
        artifacts = [event for event in emitted if event['type'] == 'artifact']
        assert {event['slot'] for event in artifacts} == set(slots)
        for event in artifacts:
            latest[event['slot']] = event['value']
            if event['slot'].endswith('_pool'):
                assert event['content_type'] == 'file'
                assert event['value']['filename'] == event['slot'] + '.json'
                tools.validate_pool(tools._read_bound(event['value']['path']))
            if 'path' in event['value']:
                offloaded.append(event['slot'])
        if step == 'peer_review':
            structured = json.loads(Path(latest['peer_review_data']['path']).read_text())
            pool = tools._read_bound(latest['peer_review_pool']['path'])
            assert structured == tools.material(pool, 'review_data')['content']
            inputs = tools.business_input('revise_document', pool)
            api.contracts.validate_input('revise_document', inputs)
            assert inputs['review_data'] == structured
        events.extend(artifacts)

    try:
        run(start, {'paper_input': {'demo': True}})
        for _ in range(10):
            run(feedback, {start+'_pool': latest[start+'_pool'], start+'_document': latest[start+'_document']})
            # Mimics the exact frozen pool/document pair supplied by Core rewind.
            run(start, {recovery+'_pool': latest[feedback+'_pool'],
                        recovery+'_document': latest[feedback+'_document']})
        value = latest[start+'_pool']
        pool = tools._read_bound(value['path']) if 'path' in value else value['data']
        assert pool['sequence'] == 21
        assert offloaded, 'must exercise real file publication, not only inline mocks'
    finally:
        if previous is None:
            lazyllm.globals.pop('subagent_ctx', None)
        else:
            lazyllm.globals['subagent_ctx'] = previous
