"""Offline regressions for the evidence-only functional runner."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'runners'), str(ROOT / 'analyzers'), str(ROOT.parent)]
import func_run
from validate_diagnostics import validate_diagnostics
from shared.observability import StreamRecord


def evidence(directory, **overrides):
    data = dict(
        case=SimpleNamespace(assertions={'content': {'min_chars': 9999}, 'steps': ['impossible']}, extras={}),
        output_dir=Path(directory), base_url='http://localhost', timeout=10,
        use_ui=True, compute_func_checks=False, started_at=1, finished_at=2,
        state={'writer_status': 'completed', 'session_id': 's'},
        chat_info={'final_panel_visible': False, 'rendered_images': 0},
        session_payload={}, final_artifact='short draft', final_artifact_text='short draft',
        final_revision=0, final_md_path=Path(directory)/'final.md',
        source_artifact='original', materialized_artifact=None,
        materialized_checks_required=False, use_provider_materialization=False,
        render_evidence=None, materialization_error=None,
        sse_capture={'streams': []}, sse_streams=[StreamRecord('stream', events=['artifact_stream_start'], text='short')],
        sse_text='short', trace_json={'id': 't', 'observations': []}, trace_id='t',
        trace_path=Path(directory)/'trace.json', trace_error=None, trace_error_spans=[],
        media_min_images=2, media_assets=None, feishu_before=None, feishu_after=None,
        feishu_materialization_summary=None, provider_checks=[],
    )
    data.update(overrides)
    return func_run.ExecutionEvidence(**data)


class EvidencePipelineTests(unittest.TestCase):
    def test_unmet_expectations_are_facts_not_verdicts_even_with_no_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            ev = evidence(directory)
            outcome = func_run._evaluate_execution(ev, '')
            self.assertEqual(outcome.checks, [])
            facts = outcome.facts['functional']
            self.assertEqual(facts['draft']['revision'], 0)
            self.assertFalse(facts['ui']['final_panel_visible'])
            self.assertEqual(facts['streams'][0]['events'], ['artifact_stream_start'])
            result = func_run._persist_execution(ev, outcome)
            self.assertEqual(result.execution_status, 'COMPLETED')
            self.assertEqual(result.acceptance_status, 'PENDING_AI_REVIEW')
            self.assertEqual(result.mechanical_verdict, 'PENDING_AI_REVIEW')
            saved = json.loads((Path(directory)/'evidence.json').read_text())
            self.assertEqual(saved['expected'], ev.case.assertions)
            self.assertEqual(json.loads((Path(directory)/'source_document.json').read_text()), 'original')
            self.assertIn('PENDING_AI_REVIEW', (Path(directory)/'report.md').read_text())

    def test_missing_evidence_and_failed_execution_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            ev = evidence(directory, trace_json=None, sse_capture=None,
                          final_artifact=None, state={'writer_status': 'failed'})
            result = func_run._persist_execution(ev, func_run._evaluate_execution(ev, ''))
            self.assertEqual(result.execution_status, 'FAILED')
            self.assertEqual(result.evidence_status, 'INCOMPLETE')
            self.assertEqual(len(result.evidence_issues), 3)
            self.assertEqual(result.acceptance_status, 'PENDING_AI_REVIEW')

    def test_provider_content_is_preserved_without_fixed_content_judgment(self):
        case = SimpleNamespace(assertions={'write_back': {'provider_revision': 'increase'},
                                          'provider_materialization': True},
                               prompt_text='https://example.feishu.cn/docx/abc',
                               extras={'feishu_reference': 'https://example.feishu.cn/docx/abc'})
        with patch.object(func_run, 'fetch_feishu_document', return_value=SimpleNamespace(
                revision_id=2, content='<document><p>unexpected body</p></document>')):
            revision, captured, checks, error = func_run._collect_provider_evidence(case, 'different', 5, 10)
        self.assertEqual(revision, 2)
        self.assertIn('unexpected body', captured['content'])
        self.assertEqual(checks, [])
        self.assertIsNone(error)

    def test_publication_links_remain_available_for_ai_and_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            ev = evidence(directory)
            ev.case.assertions['publication'] = {'providers': ['notion']}
            ev.chat_info['publication_evidence'] = {'publications': {'notion': {
                'status': 'succeeded', 'response': {
                    'provider': 'notion', 'provider_synced': True, 'artifact_saved': True,
                    'artifact_id': 'a', 'target_document': {'doc_id': 'n', 'uri': 'https://notion.so/n'},
                }}}}
            result = func_run._persist_execution(ev, func_run._evaluate_execution(ev, ''))
            self.assertIn('https://notion.so/n', (Path(directory)/'report.md').read_text())
            self.assertEqual(result.acceptance_status, 'PENDING_AI_REVIEW')

    def test_cli_exit_code_tracks_execution_not_business_acceptance(self):
        import contextlib
        import io
        for state, expected_code in [('completed', 0), ('failed', 1)]:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                ev = evidence(directory, state={'writer_status': state})
                result = func_run._persist_execution(ev, func_run._evaluate_execution(ev, ''))
                with patch.object(sys, 'argv', ['func_run', '--scenario', 'C01',
                                               '--output-dir', directory, '--no-checks']), \
                     patch.object(func_run, 'load_writer_e2e_scenario', return_value=ev.case), \
                     patch.object(func_run, 'run_case', return_value=result), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(func_run.main(), expected_code)

    def test_unknown_business_rules_are_preserved_without_mapping(self):
        import yaml
        from shared.case_loader import load_writer_e2e_scenario
        expected = {'future_business_rule': {'threshold': 4},
                    'artifacts': {'required': {'draft_document': {'new_property': 'anything'}}}}
        acceptance = [{'requirement': 'Keep tables', 'allowed_changes': ['styling']}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'cases.yaml'
            path.write_text(yaml.safe_dump({'scenarios': [{
                'id': 'T01', 'request': {'text': 'write'},
                'expected': expected, 'acceptance': acceptance,
            }]}))
            case = load_writer_e2e_scenario('T01', rules_path=path)
            self.assertEqual(case.assertions, expected)
            self.assertEqual(case.extras['acceptance'], acceptance)
            ev = evidence(directory, case=case)
            func_run._persist_execution(ev, func_run._evaluate_execution(ev, ''))
            saved = json.loads((Path(directory)/'evidence.json').read_text())
            self.assertEqual(saved['expected'], expected)
            self.assertEqual(saved['acceptance'], acceptance)
            self.assertEqual(saved['scenario_snapshot']['expected'], expected)
            self.assertEqual(len(saved['rule_source']['sha256']), 64)

    def test_slot_collection_does_not_require_matching_rule_fields(self):
        from analyze_common import analyze
        analysis = analyze({'observations': []})
        session = {'slots': [{'slot_id': 'new_slot', 'content_type': 'application/json',
                              'revision': 1, 'selected': True}]}
        with patch.object(func_run, 'load_slot', return_value=({'success': False}, 'application/json')) as load:
            func_run._merge_session_slots(analysis, session, 'http://localhost', '')
        load.assert_called_once()
        self.assertFalse(analysis.slot_view['new_slot'].properties['success'])

    def test_fixture_source_is_collected_without_cross_reference_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.md'
            path.write_text('Original text')
            case = SimpleNamespace(assertions={}, attachment_path=path)
            self.assertEqual(func_run._cross_ref_source(case), 'Original text')

    def test_runner_collects_export_and_media_without_business_rules(self):
        from shared.case_loader import Case
        case = Case('T01', 0, 'write', assertions={})
        session = {'session_id': 's', 'slots': []}
        prepared = SimpleNamespace(started_at=1, attachments=[], provider_revision_before=None,
                                   session=SimpleNamespace(token='token', user_id='u'))
        loaded = []
        def load_slot(base, token, state, slot):
            loaded.append(slot)
            return ({'draft_document': 'draft', 'source_document': 'source',
                     'resolved_media_assets': {'images': []}}[slot], 'text/plain')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(func_run, 'prepare_execution', return_value=prepared), \
             patch.object(func_run, '_run_api', return_value={
                 'conversation_id': 'c', 'session_id': 's', 'writer_status': 'completed'}), \
             patch.object(func_run, 'resolve_workflow_session', return_value=session), \
             patch.object(func_run, 'load_slot', side_effect=load_slot), \
             patch.object(func_run, '_export_writer_document', return_value=('export', {})) as export:
            result = func_run.run_case(case, Path(directory), use_ui=False, fetch_trace_after=False)
            export.assert_called_once()
            self.assertIn('resolved_media_assets', loaded)
            self.assertEqual((Path(directory)/'export_document.md').read_text(), 'export')
            self.assertEqual(result.acceptance_status, 'PENDING_AI_REVIEW')

    def test_statistics_and_integrity_gates_still_load(self):
        import perf_run
        from analyze_perf import document_metrics
        self.assertTrue(callable(perf_run.run_case))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'final.md'
            path.write_text('abc')
            self.assertEqual(document_metrics(path)['final_visible_chars'], 3)
        self.assertEqual(validate_diagnostics({'observations': []})['status'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
