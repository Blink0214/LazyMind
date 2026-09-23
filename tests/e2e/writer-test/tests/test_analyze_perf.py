"""Current Writer trace contracts: parent trees and identified remote subagents."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(REPO_ROOT / 'tests/e2e'), str(REPO_ROOT / 'tests/e2e/writer-test/analyzers')]
from analyze_perf import avg_traces, extract, render_table, sheet_rows


def span(oid, name, start, end, parent=None, *, error=False, tokens=None):
    epoch = datetime(2026, 9, 22, tzinfo=timezone.utc)
    result = {
        'id': oid, 'name': name, 'type': 'GENERATION' if name == 'llm' else 'SPAN',
        'startTime': (epoch + timedelta(seconds=start)).isoformat(),
        'endTime': (epoch + timedelta(seconds=end)).isoformat(),
        'parentObservationId': parent, 'level': 'ERROR' if error else 'DEFAULT',
    }
    if name == 'llm':
        result['usageDetails'] = dict(zip(('input', 'output'), tokens or (10, 5)))
        result['metadata'] = {'attributes': {'lazyllm.io.input': 'abc', 'lazyllm.io.output': 'de'}}
    return result


def attempt(prefix, phase, start, *, error=False):
    tool = {'prepare': 'writer_prepare_workspace', 'outline': 'writer_outline_workspace',
            'write_document': 'writer_draft_workspace'}[phase]
    return [span(prefix, 'advance_step', start, start + 10, 'main', error=error),
            span(prefix + '-agent', 'ReactAgent', start, start + 10, prefix),
            span(prefix + '-decision', 'llm', start, start + 2, prefix + '-agent'),
            span(prefix + '-ws', tool, start + 2, start + 10, prefix + '-agent', error=error),
            span(prefix + '-llm', 'llm', start + 3, start + 8, prefix + '-ws')]


def trace(*attempts):
    return {'id': 'sample', 'observations': [span('main', 'ReactAgent', 0, 100)]
            + [o for group in attempts for o in group]}


def dispatch(oid, start, end, parent, tool, phase=None):
    result = span(oid, 'llm', start, end, parent)
    result['output'] = {'tool_calls': [{'function': {'name': tool,
        'arguments': json.dumps({'step_ids': [phase]} if phase else {})}}]}
    return result


class PerfExtractTests(unittest.TestCase):
    def test_payload_disabled_and_failed_dispatch_use_the_same_round_rule(self):
        data = trace(attempt('bad', 'prepare', 10, error=True), attempt('ok', 'prepare', 30))
        data['observations'] += [span('bad-call', 'llm', 7, 9, 'main'),
                                 span('ok-call', 'llm', 27, 29, 'main')]
        before = copy.deepcopy(data)
        stats = avg_traces([data], 'P04')
        self.assertEqual(data, before)
        self.assertFalse(stats['unresolved_dispatches'])
        self.assertEqual(stats['clean_full']['count'], 3)
        self.assertEqual(stats['phases']['prepare']['normal']['wall'], 10)
        self.assertEqual(stats['phases']['prepare']['abnormal']['llm_count'], 2)
        self.assertEqual(len(sheet_rows(stats, 'batch')[0]), 2)

    def test_slow_external_dispatch_never_changes_stage_metrics(self):
        data = trace(attempt('p', 'prepare', 50))
        before = extract(data)
        data['observations'].append(dispatch('external', 1, 49, 'main', 'advance_step', 'prepare'))
        after = extract(data)
        self.assertEqual(before['phases'], after['phases'])
        self.assertEqual(after['clean_full']['count'], before['clean_full']['count'] + 1)
        self.assertEqual(after['clean_full']['input'], before['clean_full']['input'] + 10)
        self.assertEqual(after['clean_full']['wall'], 60)
        self.assertEqual(after['decision_links'][0]['scope'], 'full_flow_only')
        self.assertIn('external', after['unassigned_llm_ids'])

    def test_ambiguous_failed_dispatch_blocks_export_not_guessed(self):
        data = trace(attempt('bad', 'prepare', 10, error=True), attempt('ok', 'prepare', 30))
        bad = dispatch('bad-call', 7, 9, 'main', 'advance_step', 'outline')
        data['observations'].append(bad)
        result = avg_traces([data], 'P01')
        self.assertTrue(result['unresolved_dispatches'])
        with self.assertRaisesRegex(ValueError, 'incomplete phase attribution'):
            sheet_rows(result, 'batch')

    def test_truncated_payload_counts_the_captured_fragment(self):
        data = trace(attempt('p', 'prepare', 10))
        data['observations'][-1]['metadata']['attributes']['lazyllm.io.input'] = 'abc...<truncated>'
        stats = avg_traces([data], 'P01')
        row = stats['phases']['prepare']['normal']
        self.assertEqual(row['input_chars'], 3 + len('abc...<truncated>'))
        self.assertEqual(row['output_chars'], 4)
        self.assertEqual(row['input'], 20)
        self.assertEqual(sheet_rows(stats, 'batch')[0][0][9], 3 + len('abc...<truncated>'))

    def test_dispatch_is_full_flow_only_and_failed_dispatch_is_removed(self):
        data = trace(attempt('p', 'prepare', 10),
                     attempt('bad', 'write_document', 30, error=True),
                     attempt('ok', 'write_document', 50))
        data['observations'] += [
            dispatch('p-dispatch', 7, 9, 'main', 'advance_step', 'prepare'),
            dispatch('bad-dispatch', 27, 29, 'main', 'advance_step', 'write_document'),
            dispatch('ok-dispatch', 47, 49, 'main', 'advance_step', 'write_document')]
        original = copy.deepcopy(data)
        result = extract(data)
        self.assertEqual(data, original)
        self.assertEqual(result['phases']['prepare']['normal']['llm_count'], 2)
        row = result['phases']['write_document']
        self.assertEqual(row['normal']['llm_count'], 2)
        self.assertEqual(row['abnormal']['llm_count'], 2)
        self.assertEqual(row['normal']['wall'], 10)
        self.assertEqual(result['clean_full']['count'], 6)
        self.assertEqual(len(result['decision_links']), 3)
        self.assertNotIn('bad-dispatch', result['unassigned_llm_ids'])
        self.assertIn('ok-dispatch', result['unassigned_llm_ids'])

    def test_unproven_or_competing_dispatch_stays_in_full_flow(self):
        for variant in ('wrong_phase', 'multiple_calls', 'intervening_tool', 'overlap', 'following_tool', 'next_round_overlap', 'different_trace'):
            data = trace(attempt('p', 'prepare', 10))
            decision = dispatch('dispatch', 7, 9, 'main', 'advance_step', 'prepare')
            if variant == 'wrong_phase': decision['output']['tool_calls'][0]['function']['arguments'] = '{"step_ids":["outline"]}'
            if variant == 'multiple_calls': decision['output']['tool_calls'] *= 2
            if variant == 'intervening_tool': data['observations'].append(span('other', 'get_ready_steps', 9, 9.5, 'main'))
            if variant == 'overlap': decision['endTime'] = span('', '', 0, 11)['endTime']
            if variant == 'following_tool': data['observations'].append(span('other', 'get_ready_steps', 20, 21, 'main'))
            if variant == 'next_round_overlap': data['observations'].append(span('next', 'llm', 19, 21, 'main'))
            if variant == 'different_trace':
                decision['traceId'] = 'other'
                data['observations'][1]['traceId'] = 'current'
            data['observations'].append(decision)
            with self.subTest(variant=variant):
                result = extract(data)
                self.assertEqual(result['phases']['prepare']['normal']['llm_count'], 2)
                self.assertEqual(result['clean_full']['count'], 4 if variant == 'next_round_overlap' else 3)
                self.assertIn('dispatch', result['unassigned_llm_ids'])
                self.assertFalse(result['decision_links'])
                self.assertFalse(result['unresolved_dispatches'])
                self.assertTrue(sheet_rows(avg_traces([data], 'P01'), 'batch')[0])

    def test_successful_workspace_retry_keeps_its_dispatch_and_parallel_time(self):
        data = trace(attempt('d', 'write_document', 10))
        for o in data['observations']:
            if o['id'] == 'd-ws': o['level'] = 'ERROR'
            if o['id'] in ('d', 'd-agent'): o['endTime'] = span('', '', 0, 40)['endTime']
        data['observations'] += [
            span('late-failed-child', 'llm', 18, 35, 'd-ws'),
            dispatch('retry-decision', 23, 25, 'd-agent', 'writer_draft_workspace'),
            span('retry-ws', 'writer_draft_workspace', 26, 40, 'd-agent'),
            span('retry-llm', 'llm', 27, 39, 'retry-ws')]
        result = extract(data)
        row = result['phases']['write_document']['normal']
        self.assertEqual((row['llm_count'], row['input'], row['wall']), (2, 20, 16))
        self.assertEqual(result['clean_full']['count'], 2)
        # Failed child overlaps decision and successful workspace; only the
        # unprotected 25..26 gap is removed in addition to the failed prefix.
        self.assertEqual(result['clean_full']['wall'], 26)
        self.assertEqual(len(result['decision_links']), 1)

    def test_failed_descendant_beyond_parent_is_removed_once(self):
        data = trace(attempt('bad', 'prepare', 10, error=True), attempt('ok', 'prepare', 30))
        data['observations'].append(span('late', 'llm', 18, 27, 'bad-ws'))
        result = extract(data)
        self.assertEqual(result['clean_full']['wall'], 23)  # 0..40 minus union(10..27)
        self.assertEqual(result['phases']['prepare']['abnormal']['wall'], 17)

    def test_backend_failed_terminal_overrides_successful_workspace(self):
        data = trace(attempt('p', 'prepare', 10))
        data['observations'][1]['output'] = {
            'attempt_results': [{'step_id': 'prepare', 'status': 'failed'}]}
        result = extract(data)
        self.assertIsNone(result['phases']['prepare']['normal'])
        self.assertEqual(result['clean_full']['count'], 0)

    def test_missing_retry_payload_does_not_block_serial_round(self):
        data = trace(attempt('d', 'write_document', 10))
        for o in data['observations']:
            if o['id'] == 'd-ws': o['level'] = 'ERROR'
            if o['id'] in ('d', 'd-agent'): o['endTime'] = span('', '', 0, 40)['endTime']
        data['observations'] += [span('decision', 'llm', 23, 25, 'd-agent'),
                                 span('retry-ws', 'writer_draft_workspace', 26, 40, 'd-agent'),
                                 span('retry-llm', 'llm', 27, 39, 'retry-ws')]
        stats = avg_traces([data], 'P04')
        self.assertEqual(stats['full_link']['count'], 4)
        self.assertEqual(stats['clean_full']['count'], 2)
        self.assertFalse(stats['unresolved_dispatches'])
        self.assertEqual(stats['decision_links'][0]['basis'], 'same_agent_serial_retry_round')
        self.assertTrue(sheet_rows(stats, 'batch')[0])

    def test_real_remote_trace_preserves_metrics_and_source(self):
        path = REPO_ROOT / 'tests/e2e/writer-test/fixtures/perf/trace_remote_steps.json'
        data = json.loads(path.read_text())
        original = copy.deepcopy(data)
        result = extract(data, {'final': {'visible_characters': 2735}, 'draft_sections': 6})
        self.assertEqual(data, original)
        self.assertEqual(result['attribution'], 'workspace_step_tree')
        self.assertEqual(len(result['step_links']), 2)
        expected = {'prepare': (2, 2668, 730, 16.908),
                    'outline': (3, 8636, 11550, 195.081),
                    'write_document': (8, 28786, 6182, 75.212)}
        for phase, values in expected.items():
            row = result['phases'][phase]['normal']
            for key, value in zip(('llm_count', 'input', 'output', 'wall'), values):
                self.assertAlmostEqual(row[key], value, places=3)
        stats = avg_traces([data], 'P01')
        self.assertEqual(len(stats['step_links']), 2)
        self.assertFalse(stats['unresolved_dispatches'])
        self.assertEqual(len(sheet_rows(stats, 'batch')[0]), 4)

    def test_remote_identity_and_enclosing_step_must_be_unambiguous(self):
        path = REPO_ROOT / 'tests/e2e/writer-test/fixtures/perf/trace_remote_steps.json'
        source = json.loads(path.read_text())
        for variant in ('identity', 'missing_context', 'missing_step', 'overlap', 'broken_parent'):
            data = copy.deepcopy(source)
            root = next(o for o in data['observations'] if o['id'] == '451a807b087a7865')
            step = next(o for o in data['observations'] if o['id'] == '9683f4515b975f05')
            if variant == 'identity': root['metadata']['attributes']['session.id'] = 'other-workflow'
            elif variant == 'missing_context': data['metadata'] = {}
            elif variant == 'missing_step': data['observations'].remove(step)
            elif variant == 'overlap': data['observations'].append({**step, 'id': 'competing-step'})
            elif variant == 'broken_parent': root['parentObservationId'] = 'missing'
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                extract(data)

    def test_remote_retries_keep_errors_and_exclude_concurrent_main_llm(self):
        data = trace(attempt('p', 'prepare', 10), attempt('d1', 'write_document', 30, error=True),
                     attempt('d2', 'write_document', 50))
        data['metadata'] = {'run_context': {'workflow_session_id': 'workflow'}}
        for obs in data['observations']:
            obs['traceId'] = 'main-trace'
            if obs['id'] in ('d1-agent', 'd2-agent'):
                obs.update(parentObservationId=None, traceId=obs['id'])
                obs['metadata'] = {'attributes': {'session.id': 'workflow',
                                                  'lazyllm.trace.tags': '["subagent"]'}}
        data['observations'].append(span('outside', 'llm', 54, 58, 'main'))
        result = extract(data)
        row = result['phases']['write_document']
        self.assertEqual(row['llm_count'], 4)
        self.assertEqual(row['normal']['llm_count'], 2)
        self.assertEqual(row['abnormal']['llm_count'], 2)
        self.assertEqual(len(result['step_links']), 2)

    def test_real_trace_without_advance_payload(self):
        # Sanitized real 2026-09-22 trace: topology, timings and token usage only.
        path = REPO_ROOT / 'tests/e2e/writer-test/fixtures/perf/trace_parent_tree.json'
        data = json.loads(path.read_text())
        result = extract(data, doc_stats={'final': {'visible_characters': 2712}})
        self.assertEqual(result['attribution'], 'workspace_parent_tree')
        expected = {
            'prepare': (2, 2669, 925, 20.535, 22.314, .069),
            'outline': (3, 8457, 3382, 53.206, 54.272, .251),
            'write_document': (8, 27546, 8803, 178.735, 111.519, .338),
        }
        for phase, values in expected.items():
            row = result['phases'][phase]['normal']
            for key, value in zip(('llm_count', 'input', 'output', 'llm_latency', 'wall', 'tool_time'), values):
                self.assertAlmostEqual(row[key], value, places=3)
            self.assertEqual(row['tool_count'], 1)
        self.assertEqual(result['phases']['write_document']['chapters'], 6)
        self.assertEqual(result['phases']['write_document']['gen_chars'], 2712)
        self.assertEqual(sum(p['llm_count'] for p in result['phases'].values()), 13)

    def test_sibling_decisions_and_parallel_calls_belong_to_parent_step(self):
        data = trace(attempt('p', 'prepare', 10), attempt('d', 'write_document', 30))
        data['observations'] += [span('parallel', 'llm', 34, 38, 'd-ws'),
                                 span('outside', 'llm', 34, 38, 'main')]
        r = extract(data)['phases']['write_document']
        self.assertEqual(r['llm_count'], 3)  # Includes decision, excludes concurrent main agent.
        self.assertEqual(r['llm_latency'], 11)
        self.assertEqual(r['wall'], 10)
        self.assertEqual(r['tool_time'], 3)  # 8 - union(33..38,34..38), not 8 - 9.
        self.assertEqual(r['input_chars'], 9)
        self.assertEqual(r['output_chars'], 6)
        data['observations'][-2]['metadata']['attributes'].pop('lazyllm.io.input')
        stats = avg_traces([data], 'P01')
        self.assertEqual(stats['phases']['write_document']['normal']['input_chars'], 6)
        rows, _ = sheet_rows(stats, 'batch')
        self.assertEqual(next(row for row in rows if row[2] == 'write_document')[9], 6)

    def test_step_retry_and_failed_last_attempt(self):
        data = trace(attempt('p', 'prepare', 0), attempt('d1', 'write_document', 20, error=True),
                     attempt('d2', 'write_document', 40))
        row = extract(data)['phases']['write_document']
        self.assertEqual(row['llm_count'], 4)
        self.assertEqual(row['abnormal']['llm_count'], 2)
        self.assertEqual(row['normal']['llm_count'], 2)
        self.assertEqual((row['wall'], row['normal']['wall'], row['normal_attempt']), (20, 10, 2))
        for obs in data['observations']:
            if obs['id'] == 'd2-ws': obs['level'] = 'ERROR'
        row = extract(data)['phases']['write_document']
        self.assertIsNone(row['normal'])
        self.assertEqual(row['abnormal']['llm_count'], 4)
        rows, failed = sheet_rows(avg_traces([data], 'P04'), 'batch')
        self.assertIn(['P04', 'write_document'], failed)
        self.assertTrue(all(r[3] == '—' for r in rows))
        self.assertNotIn('全流程', [r[2] for r in rows])

    def test_workspace_retry_inside_one_step(self):
        data = trace(attempt('p', 'prepare', 0), attempt('d', 'write_document', 20))
        for o in data['observations']:
            if o['id'] == 'd-ws': o['level'] = 'ERROR'
        data['observations'] += [span('retry-ws', 'writer_draft_workspace', 30, 40, 'd-agent'),
                                 span('retry-llm', 'llm', 31, 39, 'retry-ws')]
        for o in data['observations']:
            if o['id'] in {'d', 'd-agent'}: o['endTime'] = span('', '', 0, 40)['endTime']
        row = extract(data)['phases']['write_document']
        self.assertEqual(row['normal_attempt'], 2)
        self.assertEqual(row['normal']['llm_count'], 1)
        self.assertEqual(row['normal']['wall'], 10)
        self.assertEqual(row['abnormal']['llm_count'], 2)

    def test_failed_samples_do_not_dilute_successful_export(self):
        ok = trace(attempt('p', 'prepare', 10), attempt('d', 'write_document', 30))
        bad = trace(attempt('p', 'prepare', 10), attempt('d', 'write_document', 30, error=True))
        doc = {'final': {'visible_characters': 100}, 'revision': {'present': True, 'changed_final_characters': 8}}
        s = avg_traces([ok, bad], 'P04', doc_stats=[doc, None])
        self.assertEqual(s['n_successful_traces'], 1)
        self.assertEqual(s['clean_full']['count'], 4)
        self.assertEqual(s['phases']['write_document']['normal']['gen_chars'], 100)
        self.assertEqual(len(s['failed_samples']), 1)
        rows, failed = sheet_rows(s, 'batch')
        self.assertFalse(failed)
        wd = next(row for row in rows if row[2] == 'write_document')
        self.assertEqual(len(wd), 16)
        self.assertEqual(wd[12:15], ['—', 100, 8])
        self.assertIn('LLM 累计输入字符', render_table(s))

    def test_routing_is_separate_and_errors_excluded_from_clean_full(self):
        data = trace(attempt('p', 'prepare', 10), attempt('d', 'write_document', 30))
        data['observations'] += [span('route', 'llm', 1, 3, 'main'),
                                 span('route-error', 'llm', 4, 6, 'main', error=True),
                                 span('between', 'llm', 25, 27, 'main'),
                                 span('tail', 'llm', 41, 45, 'main')]
        result = extract(data)
        self.assertEqual(result['full_link']['count'], 8)
        self.assertEqual(result['clean_full']['count'], 6)
        self.assertEqual(result['clean_full']['wall'], 38)
        self.assertTrue(result['tail']['present'])

    def test_clean_full_keeps_all_successful_calls_without_retry(self):
        data = trace(attempt('p', 'prepare', 0), attempt('d', 'write_document', 20))
        data['observations'] += [span('between', 'llm', 12, 14, 'main'),
                                 span('parallel-main', 'llm', 24, 26, 'main')]
        result = extract(data)
        self.assertEqual(result['clean_full']['count'], result['full_link']['count'])
        self.assertEqual(result['clean_full']['input'], result['full_link']['input'])
        self.assertEqual(result['clean_full']['output'], result['full_link']['output'])
        self.assertEqual(result['clean_full']['latency'], result['full_link']['latency'])
        self.assertEqual(result['clean_full']['wall'], 30)

    def test_clean_full_removes_failed_subtree_once_and_keeps_routing(self):
        data = trace(attempt('p', 'prepare', 0),
                     attempt('d1', 'write_document', 20, error=True),
                     attempt('d2', 'write_document', 40))
        data['observations'] += [span('between', 'llm', 32, 34, 'main')]
        result = extract(data)
        self.assertEqual(result['clean_full']['count'], 5)
        self.assertEqual(result['clean_full']['input'], 50)
        self.assertEqual(result['clean_full']['wall'], 40)  # Nested ERROR spans count once.

    def test_cumulative_usage_normalized_before_failed_attempt_filter(self):
        data = trace(attempt('p', 'prepare', 0),
                     attempt('bad', 'write_document', 20, error=True),
                     attempt('ok', 'write_document', 40))
        data['metadata'] = {'llm_usage_semantics': 'cumulative_entity'}
        for o in data['observations']:
            if o['name'] == 'llm':
                o['metadata']['attributes']['lazyllm.entity.id'] = o['id']
        bad = next(o for o in data['observations'] if o['id'] == 'bad-llm')
        ok = next(o for o in data['observations'] if o['id'] == 'ok-llm')
        bad['metadata']['attributes']['lazyllm.entity.id'] = 'shared'
        ok['metadata']['attributes']['lazyllm.entity.id'] = 'shared'
        ok['usageDetails'] = {'input': 30, 'output': 12}
        original = copy.deepcopy(data)
        result = extract(data)
        self.assertEqual(data, original)
        self.assertEqual(result['phases']['write_document']['normal']['input'], 30)
        self.assertEqual(result['phases']['write_document']['normal']['output'], 12)
        self.assertEqual(result['usage_corrections'][0]['per_call'], {'input': 20, 'output': 7})
        self.assertEqual(result['clean_full']['input'], 50)
        self.assertEqual(result['full_link']['input'], 70)
        # Increasing per-call values are not implicitly differenced.
        data['metadata']['llm_usage_semantics'] = 'per_call'
        self.assertEqual(extract(data)['clean_full']['input'], 60)

    def test_cumulative_usage_rejects_ambiguous_counters(self):
        for variant in ('missing_id', 'reset', 'overlap', 'error_gap'):
            data = trace(attempt('p', 'prepare', 0))
            data['metadata'] = {'llm_usage_semantics': 'cumulative_entity'}
            llms = [o for o in data['observations'] if o['name'] == 'llm']
            for o in llms:
                o['metadata']['attributes']['lazyllm.entity.id'] = 'same'
            llms[1]['usageDetails'] = {'input': 30, 'output': 12}
            if variant == 'missing_id': llms[1]['metadata']['attributes'].pop('lazyllm.entity.id')
            elif variant == 'reset': llms[1]['usageDetails']['input'] = 1
            elif variant == 'overlap': llms[1]['startTime'] = llms[0]['startTime']
            else:
                llms[0]['level'] = 'ERROR'
                llms[0]['usageDetails'] = {}
            with self.subTest(variant=variant), self.assertRaises(ValueError): extract(data)

    def test_missing_or_ambiguous_evidence_fails_instead_of_zero_report(self):
        good = trace(attempt('p', 'prepare', 10))
        variants = []
        orphan = copy.deepcopy(good); orphan['observations'][4]['parentObservationId'] = 'missing'; variants.append(orphan)
        missing = copy.deepcopy(good); missing['observations'][2]['endTime'] = None; variants.append(missing)
        cycle = copy.deepcopy(good); cycle['observations'][0]['parentObservationId'] = 'p'; variants.append(cycle)
        duplicate = copy.deepcopy(good); duplicate['observations'].append(duplicate['observations'][-1]); variants.append(duplicate)
        usage = copy.deepcopy(good); usage['observations'][-1]['usageDetails'] = {}; variants.append(usage)
        variants.append({'observations': []})
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError): extract(data)


if __name__ == '__main__':
    unittest.main()
