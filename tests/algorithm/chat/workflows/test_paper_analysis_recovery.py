"""Deterministic identity and numerical fallback tests; no model substitutes."""
import copy

import pytest

from lazymind.document_tools.paper.analysis.job_identity import (
    bind_jobs, bind_with_repair, generation_schema, JobReferenceError,
)
from lazymind.document_tools.paper.analysis.plan import Job
from lazymind.document_tools.paper.analysis.reference import (
    reference, reference_with_fallback, normalize_inference_fields, verify,
)
from lazymind.document_tools.paper.analysis.schema import normalize_input
from lazymind.document_tools.paper.analysis.views import materialize
from lazymind.document_tools.paper.analysis.pipeline import build_analysis


def raw_job(**changes):
    return dict(id='legacy', target_id='target', operation='mean', table_id='data', column='score', **changes)


def test_generated_schema_has_positional_references_not_model_job_ids():
    schema = generation_schema()['$defs']
    assert 'id' not in schema['Job']['properties']
    assert 'finding_indices' in schema['FigurePlan']['required']
    assert 'finding_ids' not in schema['FigurePlan']['properties']


def test_identical_duplicate_jobs_and_figure_references_are_collapsed():
    first = raw_job()
    second = {**first, 'interval_level': None, 'filters': []}
    raw = dict(jobs=[first, second], figures=[dict(finding_ids=['legacy', 'legacy'])])
    before = copy.deepcopy(raw)
    result = bind_jobs(raw)
    assert len(result['jobs']) == 1
    identifier = result['jobs'][0]['id']
    assert identifier.startswith('job_') and identifier != 'legacy'
    assert result['figures'][0]['finding_ids'] == [identifier]
    assert bind_jobs(result) == result
    assert raw == before


def test_same_alias_different_calculations_is_not_silently_bound():
    raw = dict(jobs=[raw_job(), raw_job(filters=[dict(column='group', operator='eq', value='a')])],
               figures=[dict(finding_ids=['legacy'])])
    with pytest.raises(JobReferenceError):
        bind_jobs(raw)
    raw['figures'][0] = dict(finding_indices=[1])
    result = bind_jobs(raw)
    assert len(result['jobs']) == 2
    assert result['figures'][0]['finding_ids'] == [result['jobs'][1]['id']]


def test_source_bound_fields_do_not_rename_jobs_during_figure_repair():
    plan = bind_jobs(dict(jobs=[raw_job()], figures=[dict(finding_indices=[0])]))
    identifier = plan['jobs'][0]['id']
    plan['jobs'][0].update(unit='points', design_evidence='Source text copied by host')
    rebound = bind_jobs(plan)
    assert rebound['jobs'][0]['id'] == identifier
    assert rebound['figures'][0]['finding_ids'] == [identifier]


def test_codegen_receives_explicit_no_inference_switches():
    from lazymind.document_tools.paper.analysis.prompt_payload import execution_plan
    plan, defaults = execution_plan({'jobs': [raw_job()]})
    assert 'interval_level' in plan['jobs'][0]
    assert plan['jobs'][0]['interval_level'] is None
    assert plan['jobs'][0]['correlation_test'] is False
    assert defaults['interval_level'] is None


def test_unrequested_uncertainty_is_removed_but_wrong_estimates_still_fail():
    inputs, job, plan = fixture('mean', [['a', 1, 2.], ['b', 1, 1.]])
    expected = reference_with_fallback(job, inputs, {}, materialize(inputs, []), plan)
    raw = {job['id']: dict(value=2., lower=1., upper=3., statistic=8., p_value=.001)}
    cleaned = normalize_inference_fields(raw, plan)
    assert cleaned[job['id']] == expected[0]
    assert raw[job['id']]['lower'] == 1.
    assert verify(cleaned, plan, inputs, {job['id']: expected})
    cleaned[job['id']]['value'] = 3.
    with pytest.raises(ValueError, match='数值复核'):
        verify(cleaned, plan, inputs, {job['id']: expected})


def test_requested_inference_is_not_erased_by_output_normalization():
    inputs, job, plan = fixture('welch_t', [['a', 1, 2.], ['a', 2, 4.], ['b', 1, 1.], ['b', 2, 2.]])
    expected = reference(job, inputs, {}, materialize(inputs, []))
    result = {job['id']: expected[0]}
    assert normalize_inference_fields(result, plan) == result


@pytest.mark.parametrize('indices', [[-1], [2], [True], ['0'], []])
def test_invalid_position_is_not_guessed(indices):
    with pytest.raises(JobReferenceError):
        bind_jobs(dict(jobs=[raw_job()], figures=[dict(finding_indices=indices)]))


def test_reference_repair_changes_only_the_ambiguous_selector():
    calls = []
    def repair(payload):
        calls.append(payload)
        return {'updates': [{'figure_index': 0, 'finding_indices': [1]}]}
    raw = dict(jobs=[raw_job(), raw_job(interval_level=0.95)],
               figures=[dict(title='Preserve this', finding_ids=['legacy'])])
    result = bind_with_repair(raw, repair)
    assert len(calls) == 1
    assert result['figures'][0]['title'] == 'Preserve this'
    assert result['figures'][0]['finding_ids'] == [result['jobs'][1]['id']]


def fixture(operation, values):
    inputs = normalize_input(dict(topic='Simulated comparison', data=dict(
        columns=['group', 'pair', 'score'], column_types=['string', 'integer', 'number'],
        units=[None, None, 'points'], rows=values)))
    job = Job.model_validate({**raw_job(), 'operation': operation,
                              'filters': [dict(column='group', operator='eq', value='a')],
                              'baseline_filters': [] if operation == 'mean' else [
                                  dict(column='group', operator='eq', value='b')],
                              'pair_by': ['pair'] if operation == 'paired_t' else [],
                              'interval_level': .95}).model_dump()
    plan = dict(jobs=[job], figures=[], limitations=[], methods=['planned inference'],
                targets=[dict(id='target', question='How large?', estimand='score', scope='given rows',
                              columns=[dict(table_id='data', column='score')])],
                models=[], joins=[], multiplicity='none')
    return inputs, job, plan


@pytest.mark.parametrize('operation', ['mean', 'difference', 'welch_t', 'paired_t'])
@pytest.mark.parametrize('values', [
    [['a', 1, 2.], ['a', 2, 2.], ['b', 1, 1.], ['b', 2, 1.]],
    [['a', 1, 2.], ['b', 1, 1.]],
])
def test_unavailable_inference_preserves_estimate_and_clears_false_certainty(operation, values):
    inputs, job, plan = fixture(operation, values)
    before = copy.deepcopy(inputs)
    result = reference_with_fallback(job, inputs, {}, materialize(inputs, []), plan)
    assert result[0]['value'] == (2. if operation == 'mean' else 1.)
    assert all(result[0][key] is None for key in ('lower', 'upper', 'statistic', 'p_value'))
    assert job['interval_level'] is None
    assert job['operation'] not in ('welch_t', 'paired_t')
    assert inputs == before
    checked = {job['id']: result}
    assert verify({job['id']: result[0]}, plan, inputs, checked) == checked
    output = build_analysis(inputs, plan, checked)
    assert output['findings'][0]['interval'] is None
    assert output['findings'][0]['test'] is None
    assert any('不可估计' in text for text in output['limitations'])


def test_valid_inference_is_unchanged():
    inputs, job, plan = fixture('mean', [['a', 1, 1.], ['a', 2, 3.], ['a', 3, 5.]])
    expected = reference(job, inputs, {})
    assert reference_with_fallback(job, inputs, {}, materialize(inputs, []), plan) == expected
    assert job['interval_level'] == .95 and not plan['limitations']


def test_missing_or_invalid_data_is_not_converted_to_an_estimate():
    inputs, job, plan = fixture('mean', [['b', 1, 1.]])
    with pytest.raises(ValueError, match='没有可分析'):
        reference_with_fallback(job, inputs, {}, materialize(inputs, []), plan)
    assert job['interval_level'] == .95 and not plan['limitations']
