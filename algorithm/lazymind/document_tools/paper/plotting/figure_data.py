"""Bind verified results to figure inputs before asking a model for presentation code."""
import copy
import math

from .result_api import result_grid


GRID_STYLES = {'point_interval', 'bar_grouped_hatch', 'line_confidence_band', 'line_training_curve'}
DIFFERENCES = {'difference', 'paired_difference', 'paired_t', 'welch_t'}


class FigureDataError(ValueError):
    def __init__(self, figure_id, message):
        self.figure_id = figure_id
        super().__init__(message)


def _point(record):
    estimate, interval = record['value'], record['interval']
    if type(estimate) not in (int, float) or not math.isfinite(estimate):
        raise ValueError('Every observed figure point needs a finite verified estimate')
    if interval is not None:
        low, high = interval['lower'], interval['upper']
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (low, high))
                or not low <= estimate <= high):
            raise ValueError('Verified interval endpoints must be finite and enclose their estimate')
        interval = {**{key: value for key, value in interval.items() if key not in ('lower', 'upper')},
                    'low': low, 'high': high}
    return dict(id=record['id'], metric=record['metric'], operation=record['operation'],
                unit=record['unit'], estimate=estimate, interval=copy.deepcopy(interval),
                sample_size=copy.deepcopy(record['sample_size']), coordinates=copy.deepcopy(record['coordinates']),
                comparisons=copy.deepcopy(record['comparisons']), scope=copy.deepcopy(record.get('scope', {})))


def _panels(figure, metadata, contract):
    mapping = figure.get('data_mapping')
    if not mapping:
        raise ValueError('data_mapping is required; declare series_column/category_column/facets')
    series, category = mapping['series_column'], mapping['category_column']
    facets = mapping.get('facets', [])
    dimensions = copy.deepcopy(metadata['result_dimensions'])
    if category == 'finding_id':
        dimensions[category] = [dict(value=row['id'], artist_label=row['id'], display_label=row['id'])
                                for row in metadata['result_records']]
    if (category not in dimensions or (series is not None and series not in dimensions)
            or category == series or len(set(facets)) != len(facets)
            or any(key not in dimensions or key in (series, category) for key in facets)):
        raise ValueError('data_mapping must reference distinct supplied result dimensions')
    rows = copy.deepcopy(metadata['result_records'])
    for row in rows:
        if category == 'finding_id':
            row['coordinates'][category] = row['id']
        if any(key not in row['coordinates'] for key in [category, *facets, *([series] if series else [])]):
            raise ValueError('Every selected finding must have its planned figure coordinates')
    if series is None:
        series = '_quantity'
        appearance = {key: contract[key][0] for key in ('colors', 'bar_colors', 'edge_colors', 'markers', 'line_styles')}
        dimensions[series] = [dict(value='Estimate', artist_label='Estimate', display_label='Estimate',
                                  color=appearance['colors'], bar_color=appearance['bar_colors'],
                                  edge=appearance['edge_colors'], marker=appearance['markers'],
                                  line_style=appearance['line_styles'])]
        for row in rows:
            row['coordinates'][series] = 'Estimate'
    groups = {}
    for row in rows:
        interval = row['interval'] or {}
        key = (row['metric'], row['operation'], row['unit'], interval.get('kind'), interval.get('level'),
               interval.get('method'), *(row['coordinates'][key] for key in facets))
        groups.setdefault(key, []).append(row)
    panels = []
    for selected in groups.values():
        metric, operation, unit = (selected[0][key] for key in ('metric', 'operation', 'unit'))
        grid = result_grid({'metadata': dict(result_records=selected, result_dimensions=dimensions)},
                           metric, series, category, {})
        arguments = dict(title='')
        difference = operation in DIFFERENCES
        ylabel = ('Selected - baseline: ' if difference else '') + metric + (f' ({unit})' if unit else '')
        style = figure['style']
        if style == 'point_interval':
            arguments.update(summaries=grid['summaries'], methods=grid['methods'], categories=grid['categories'],
                             colors=grid['colors'], edges=grid['edges'], markers=grid['markers'],
                             xlabel=ylabel,
                             xlim=grid['difference_limits'] if difference else grid['value_limits'])
        elif style == 'bar_grouped_hatch':
            arguments.update(data_dict=grid['values'], colors=grid['bar_colors'], hatches=[''] * len(grid['methods']),
                             labels=grid['methods'], x_positions=list(range(len(grid['categories']))), total_w=.78,
                             xlim=[-.56, len(grid['categories']) - .44],
                             legend_anchor=contract['layout']['legend_anchor'], benchmarks=grid['categories'],
                             best_method=None, xlabel=category, ylabel=ylabel, y_limits=grid['bar_limits'])
        else:
            if grid['x_limits'] is None:
                raise ValueError('A trajectory requires numeric observed category coordinates; revise data_mapping/style')
            scale = mapping.get('xscale', 'linear')
            if scale == 'log':
                if min(grid['x']) <= 0:
                    raise ValueError('A logarithmic trajectory needs positive observed coordinates')
                grid['x_limits'] = [min(grid['x']) / 1.08, max(grid['x']) * 1.08]
            arguments.update(series=(grid['curves'] if style == 'line_confidence_band' else
                                     {key: value[:2] for key, value in grid['curves'].items()}),
                             labels=grid['methods'], colors=grid['colors'], edges=grid['edges'],
                             markers=grid['markers'], line_styles=grid['line_styles'], xlabel=category,
                             ylabel=ylabel, xlim=grid['x_limits'], ylim=grid['value_limits'],
                             legend_location=contract['legend_location'])
            if style == 'line_confidence_band':
                arguments['xscale'] = scale
            elif scale != 'linear':
                raise ValueError('The selected training-curve helper uses a linear x axis')
        panels.append(dict(id=f'panel_{len(panels) + 1}', finding_ids=[row['id'] for row in selected],
                           quantity=dict(metric=metric, operation=operation, unit=unit),
                           facet={key: selected[0]['coordinates'][key] for key in facets},
                           comparisons=grid['comparisons'], arguments=arguments))
    return panels


def prepare_figure_data(figure, metadata, contract):
    """Preserve all selected findings once; native-ready arguments never require model-side joins."""
    rows = metadata.get('result_records')
    if not rows:
        return None
    try:
        if len({row['id'] for row in rows}) != len(rows):
            raise ValueError('Duplicate finding identities in figure input')
        view = dict(schema='paper.figure.v1', chart_type=figure['kind'], title=figure['title'],
                    points=[_point(row) for row in rows], panels=[],
                    context={key: metadata.get(key, '') for key in ('user_request', 'source_description')})
        if figure['style'] in GRID_STYLES:
            view['panels'] = _panels(figure, metadata, contract)
            actual = [identifier for panel in view['panels'] for identifier in panel['finding_ids']]
            if sorted(actual) != sorted(row['id'] for row in rows):
                raise ValueError('Prepared panels must retain every selected finding exactly once')
        return view
    except (ValueError, KeyError, TypeError) as exc:
        raise FigureDataError(figure['id'], str(exc)) from exc
