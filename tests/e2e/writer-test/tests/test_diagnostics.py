"""Remote payload integrity and fail-fast orchestration contracts."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / 'tests/e2e'), str(ROOT / 'tests/e2e/writer-test/analyzers'),
               str(ROOT / 'tests/e2e/writer-test/runners')]
from validate_diagnostics import validate_diagnostics
from analyze_perf import extract
from perf_run import run_case, _exit_code


def payload(value):
    body = json.dumps(value, ensure_ascii=False)
    return dict(json=body, original_chars=len(body), captured_chars=len(body),
                truncated=False, sha256=hashlib.sha256(body.encode()).hexdigest())


def trace():
    diag = dict(request_preparation_ms=0, output=payload('正文'), attempts=[dict(
        request=payload({'messages': [{'content': '中文'}]}), streaming=False,
        capture_ms=0, started_offset_ms=0, duration_ms=10, headers_ms=9,
        first_body_chunk_ms=None, first_semantic_chunk_ms=None)])
    return {'observations': [dict(id='model', name='llm', type='GENERATION',
        metadata={'attributes': {'lazyllm.diagnostics.llm': diag}})]}


class DiagnosticsTests(unittest.TestCase):
    def test_payload_and_timing_contracts(self):
        self.assertEqual(validate_diagnostics(trace())['status'], 'PASS')
        for defect in ['hash', 'truncated', 'length', 'output', 'timing', 'stream', 'json', 'empty']:
            with self.subTest(defect=defect):
                t = trace(); d = t['observations'][0]['metadata']['attributes']['lazyllm.diagnostics.llm']
                a = d['attempts'][0]
                if defect == 'hash': a['request']['sha256'] = 'wrong'
                if defect == 'truncated': a['request']['truncated'] = True
                if defect == 'length': a['request']['original_chars'] += 1
                if defect == 'output': del d['output']
                if defect == 'timing': a['headers_ms'] = None
                if defect == 'stream': a['streaming'] = True
                if defect == 'json': t['observations'][0]['metadata']['attributes']['lazyllm.diagnostics.llm'] = '{'
                if defect == 'empty': t['observations'] = []
                self.assertEqual(validate_diagnostics(t)['status'], 'BLOCKED')
        t = trace(); a = t['observations'][0]['metadata']['attributes']['lazyllm.diagnostics.llm']['attempts'][0]
        a.update(streaming=True, first_body_chunk_ms=9)
        # Unknown semantic parser legitimately has no first_semantic_chunk timing.
        self.assertEqual(validate_diagnostics(t)['status'], 'PASS')

    def test_vlm_span_is_not_silently_omitted(self):
        t = trace()
        t['observations'].append(dict(id='main', name='llm', type='SPAN', metadata={
            'attributes': {'lazyllm.entity.config.type': 'VLM', 'gen_ai.usage.input_tokens': '100'}}))
        before = copy.deepcopy(t)
        audit = validate_diagnostics(t)
        self.assertEqual((audit['model_calls'], audit['diagnosed_calls'], audit['status']), (2, 1, 'BLOCKED'))
        with self.assertRaisesRegex(ValueError, 'unclassified model calls'):
            extract(t)
        self.assertEqual(t, before)

    def test_runner_preserves_evidence_and_skips_statistics(self):
        t = trace(); t['observations'][0]['type'] = 'SPAN'
        prepared = SimpleNamespace(started_at=0, session=SimpleNamespace(token='t'), attachments=[])
        session = {'status': 'completed', 'session_id': 's'}
        case = SimpleNamespace(scenario='P01')
        with tempfile.TemporaryDirectory() as td, \
                patch('perf_run.prepare_execution', return_value=prepared), \
                patch('perf_run._send', return_value=SimpleNamespace(conversation_id='c', finish_reason='STOP')), \
                patch('perf_run._wait_and_approve', return_value=session), \
                patch('perf_run.resolve_workflow_session', return_value=session), \
                patch('perf_run.save_final_markdown', return_value={'path': 'final.md'}), \
                patch('perf_run._compute_document_stats', return_value={}), \
                patch('perf_run.collect_trace_evidence', return_value=SimpleNamespace(trace=t, trace_id='t', error=None)), \
                patch('perf_run.avg_traces') as stats:
            r = run_case(case, Path(td))
            self.assertEqual(_exit_code(r, fetch_trace_after=True, compute_stats=True), 2)
            self.assertTrue((Path(td) / 'run_N.json').exists())
            self.assertEqual(json.loads((Path(td) / 'diagnostics_audit.json').read_text())['status'], 'BLOCKED')
            stats.assert_not_called()

    def test_orchestrator_stops_before_second_scenario(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td); (p / 'scripts').mkdir()
            script = p / 'scripts/full_run.sh'
            script.write_text((ROOT / 'tests/e2e/writer-test/scripts/full_run.sh').read_text())
            fake = p / 'fake-python'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\nexit 2\n')
            fake.chmod(0o755)
            result = subprocess.run(['bash', str(script), '--venv', str(fake), 'P01', 'P02', '--cases', '1'],
                                    env={**os.environ, 'CALLS': str(p / 'calls')}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            calls = (p / 'calls').read_text().splitlines()
            self.assertEqual(len(calls), 1)
            self.assertIn('--scenario P01', calls[0])
            existing = next((p / 'reports/perf').iterdir()).name
            result = subprocess.run(['bash', str(script), '--venv', str(fake), '--run-id', existing,
                                     'P02', 'P01', '--cases', '1'], capture_output=True, text=True,
                                    env={**os.environ, 'CALLS': str(p / 'calls')})
            self.assertEqual(result.returncode, 2)
            self.assertIn('拒绝覆盖', result.stderr)
            self.assertEqual(len((p / 'calls').read_text().splitlines()), 1)



if __name__ == '__main__':
    unittest.main()
