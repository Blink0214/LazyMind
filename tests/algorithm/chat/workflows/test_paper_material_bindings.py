"""Pure binding checks: no model, files, or monkeypatch required."""
import copy

import pytest

from lazymind.document_tools.paper.material_bindings import MaterialBindings
from lazymind.document_tools.paper.analysis.plan_bindings import bind_figure_mapping, bind_job_fields
from lazymind.document_tools.paper.analysis.plan_repair import apply_updates


def test_canonical_identity_round_trip_without_model_recopy():
    identifier = 'upload-17cae16226173b1c269c5a71cde4c14c6a34c189ed9e69380936615c861bc78a'
    source = {'id': identifier, 'title': 'System brief', 'metadata': {'file_id': identifier}}
    original = copy.deepcopy(source)
    bindings = MaterialBindings([source])
    view = bindings.model_view({'papers': [source], 'question': f'Background [{identifier}]'})
    assert view['papers'][0]['id'] == 'M1'
    assert view['question'] == 'Background [M1]'
    assert bindings.restore_citations('Research [M1]') == f'Research [{identifier}]'
    view['papers'][0]['metadata']['file_id'] = 'changed'
    assert source == original
    assert bindings.materials['M1'] == original


def test_unknown_markers_cannot_become_material_ids():
    with pytest.raises(ValueError, match='unavailable source'):
        MaterialBindings([{'id': 'known'}]).restore_citations('[M2]')


def test_material_views_do_not_replace_id_prefixes_inside_other_words():
    bindings = MaterialBindings([{'id': 'SRC-1'}, {'id': 'a'}])
    assert bindings.model_view('[SRC-1] [SRC-10] data') == '[M1] [SRC-10] data'


@pytest.mark.parametrize('hints', [{}, {'j_score': 'Score', 'ghost': 'Wrong'},
                                 {'data.score': 'Score', 'latency': 'Latency'}])
def test_figure_metric_inventory_comes_from_jobs(hints):
    jobs = {f'j_{column}': dict(id=f'j_{column}', column=column, table_id='data',
                              operation='mean', filters=[], baseline_filters=[], unit=None, interval_level=None)
            for column in ('score', 'latency')}
    figure = dict(id='comparison', style='point_interval', finding_ids=list(jobs),
                  data_mapping=dict(category_column='finding_id', series_column=None, facets=[], value_labels=hints))
    original = copy.deepcopy(jobs)
    assert bind_figure_mapping(figure, jobs, {'data'}) == []
    assert set(figure['data_mapping']['value_labels']) == {'score', 'latency'}
    assert jobs == original


def test_duplicate_dimensions_still_require_semantic_repair():
    job = dict(id='j_score', column='score', table_id='data', operation='mean',
               filters=[], baseline_filters=[], unit=None, interval_level=None)
    figure = dict(id='comparison', style='point_interval', finding_ids=['j_score'],
                  data_mapping=dict(category_column='finding_id', series_column='finding_id', facets=[]))
    issues = bind_figure_mapping(figure, {'j_score': job}, {'data'})
    assert issues and '互不重复' in issues[0]['message']


@pytest.mark.parametrize('operation,expected', [('mean', 'percent'), ('difference', 'pp'), ('pearson', None)])
def test_units_are_derived_from_source_and_operation(operation, expected):
    table = dict(columns=['score'], units=['percent'], rows=[[80], [90]])
    before = copy.deepcopy(table)
    job = dict(id='score', operation=operation, table_id='data', column='score', unit='invented')
    assert bind_job_fields(job, {'data': table}, {}) == []
    assert job['unit'] == expected
    assert table == before


def test_figure_only_repair_does_not_require_empty_job_updates():
    figure = dict(id='chart', columns=[], finding_ids=[], style='point_interval',
                  data_mapping={'category_column': 'finding_id', 'series_column': 'finding_id'})
    plan = dict(jobs=[], models=[], figures=[figure])
    before = copy.deepcopy(plan)
    repaired = copy.deepcopy(figure)
    repaired['data_mapping']['series_column'] = None
    issues = [dict(figure_id='chart', field='data_mapping', message='duplicate dimension')]
    response = dict(figure_updates=[dict(figure_id='chart', figure=repaired)])
    assert apply_updates(plan, response, issues)['figures'] == [repaired]
    assert plan == before
    repaired['finding_ids'] = ['invented']
    with pytest.raises(ValueError, match='原始来源'):
        apply_updates(plan, response, issues)
