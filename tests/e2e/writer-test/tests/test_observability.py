"""Trace collection must preserve current topology and wait for completed metrics."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / 'tests/e2e'), str(ROOT / 'tests/e2e/writer-test/analyzers')]
from shared.observability import (
    _normalize_local, _stable_signature, _trace_matches, _mentions_identity,
    _merge_local_traces, _closed_traces, _within_run, find_langfuse_trace, fetch_trace,
)
from shared.execution import collect_trace_evidence
from analyze_perf import extract
from test_analyze_perf import span, trace, attempt


class TraceCollectionTests(unittest.TestCase):
    def test_later_turn_in_same_trace_does_not_extend_this_run(self):
        data = trace(attempt('p', 'prepare', 10))
        data['observations'] = [o for o in data['observations'] if o['id'] != 'main']
        data['observations'].append(span('later-turn', 'llm', 400, 450))
        selected = _within_run(data, 1790035200, 1790035230)
        self.assertEqual(len(selected['observations']), 5)
        self.assertEqual(selected['latency'], 10)
        self.assertEqual(len(data['observations']), 6)

    def test_local_shape_preserves_tree_payload_usage(self):
        data = trace(attempt('p', 'prepare', 10), attempt('d', 'write_document', 30))
        rows = []
        for o in data['observations']:
            attrs = dict((o.get('metadata') or {}).get('attributes') or {})
            attrs['session.id'] = 'conversation'
            if o['name'] == 'llm':
                attrs.update({'lazyllm.semantic_type': 'llm', 'gen_ai.usage.input_tokens': 10,
                              'gen_ai.usage.output_tokens': 5})
            rows.append({'context': {'trace_id': 'a' * 32, 'span_id': o['id']},
                         'name': o['name'], 'parent_id': o['parentObservationId'],
                         'start_time': o['startTime'], 'end_time': o['endTime'], 'attributes': attrs})
        normalized = _normalize_local('current.jsonl', rows, 'conversation')
        result = extract(normalized)
        self.assertEqual(result['phases']['prepare']['input_chars'], 6)
        self.assertEqual(result['phases']['write_document']['input'], 20)
        merged = _merge_local_traces([normalized, normalized], 'conversation')
        self.assertEqual(len(merged['observations']), len(rows))

    def test_stability_tracks_usage_endtime_and_parent_not_only_ids(self):
        data = trace(attempt('p', 'prepare', 10))
        signature = _stable_signature([data])
        for key, value in [('endTime', None), ('usageDetails', {'input': 500}), ('parentObservationId', 'new')]:
            other = copy.deepcopy(data); other['observations'][-1][key] = value
            self.assertNotEqual(signature, _stable_signature([other]))
        other = copy.deepcopy(data); other['observations'].reverse()
        self.assertEqual(signature, _stable_signature([other]))
        self.assertTrue(_closed_traces([data]))
        other['observations'][0]['endTime'] = None
        self.assertFalse(_closed_traces([other]))
        other = copy.deepcopy(data)
        other['observations'][-1]['usageDetails'] = {}
        self.assertFalse(_closed_traces([other]))

    def test_exact_identity_and_anonymous_workflow_correlation(self):
        self.assertFalse(_trace_matches({'sessionId': 'conversation-other'}, 'conversation', None))
        self.assertTrue(_trace_matches({'sessionId': 'conversation_123'}, 'conversation', None))
        self.assertFalse(_mentions_identity({'output': 'workflow-other'}, ['workflow']))
        stamp = '2026-09-22T00:00:00+00:00'
        candidates = [{'id': 'own', 'timestamp': stamp, 'sessionId': None},
                      {'id': 'other', 'timestamp': stamp, 'sessionId': None}]
        own = trace(attempt('p', 'prepare', 10)); own.update(id='own', metadata={'workflow_session_id': 'workflow'})
        other = copy.deepcopy(own); other.update(id='other', metadata={'workflow_session_id': 'workflow-other'})
        with patch('shared.observability._list_langfuse_traces', return_value=candidates), \
             patch('shared.observability.langfuse_get', side_effect=lambda url, *a, **kw: own if url.endswith('/own') else other), \
             patch('shared.observability.time.sleep'):
            result = find_langfuse_trace('conversation', 'http://trace', 'p', 's', 1, 'workflow',
                                        after=1790035199, before=1790035300)
        self.assertEqual(result['id'], 'own')

    def test_waits_for_metrics_to_finish_updating(self):
        base = trace(attempt('p', 'prepare', 10)); base.update(id='t', sessionId='conversation')
        versions = []
        for tokens in (1, 2, 3, 3, 3):
            version = copy.deepcopy(base); version['observations'][-1]['usageDetails']['input'] = tokens
            versions.append(version)
        with patch('shared.observability._list_langfuse_traces', return_value=[{'id': 't', 'sessionId': 'conversation'}]), \
             patch('shared.observability.langfuse_get', side_effect=versions) as get, \
             patch('shared.observability.time.sleep'):
            result = find_langfuse_trace('conversation', 'http://trace', 'p', 's', 1)
        self.assertEqual(get.call_count, 5)
        self.assertEqual(result['observations'][-1]['usageDetails']['input'], 3)

    def test_backend_auto_honors_env_file_and_retry_success_clears_error(self):
        with patch('shared.observability.config_from_env', return_value={'LAZYLLM_TRACE_BACKEND': 'local'}), \
             patch('shared.observability.find_local_trace', return_value={'id': 't'}) as local:
            self.assertEqual(fetch_trace('conversation'), {'id': 't'})
            local.assert_called_once()
        with tempfile.TemporaryDirectory() as td, \
             patch('shared.execution.fetch_trace', side_effect=[RuntimeError('pending'), {'id': 't'}]) as fetch:
            result = collect_trace_evidence(conversation_id='conversation', workflow_session_id='workflow',
                started_at=1, finished_at=2, output_path=Path(td) / 'trace.json', retry_delay_s=0)
            self.assertIsNone(result.error)
            self.assertEqual(result.trace_id, 't')
            self.assertEqual(result.trace['metadata']['run_context'], {
                'conversation_id': 'conversation', 'workflow_session_id': 'workflow',
            })
            self.assertEqual(json.loads((Path(td) / 'trace.json').read_text()), result.trace)


if __name__ == '__main__':
    unittest.main()
