"""Bind verified results to figure inputs before asking a model for presentation code."""
import copy
import math
import json

from .result_api import result_grid
from .labels import DIFFERENCES, display_name, axis_label, result_label, result_unit, english_display, CJK
from .observations import grid_observations, observation_note


GRID_STYLES = {'point_interval', 'bar_grouped_hatch', 'line_confidence_band', 'line_training_curve'}


def _tradeoff_panels(figure, metadata, contract):
    """Join verified x/y means by exact observation scope, never by row order or result IDs."""
    mapping = figure['data_mapping']
    rows, dimensions = metadata['result_records'], metadata['result_dimensions']
    x_metric = mapping['category_column']
    metrics = set(row['metric'] for row in rows)
    if len(metrics) != 2 or x_metric not in metrics or any(row['operation'] != 'mean' for row in rows):
        raise ValueError('A prepared tradeoff needs two verified mean quantities and its explicit x metric')
    y_metric = next(metric for metric in metrics if metric != x_metric)
    series, facets = mapping['series_column'], mapping.get('facets', [])
    varying = {key for key in dimensions if len({row['coordinates'].get(key) for row in rows}) > 1}
    if varying - {*facets, *([series] if series else [])}:
        raise ValueError('Separate varying tradeoff conditions with explicit series/facets; do not pool scopes')
    pairs = {}
    for row in rows:
        if row['sample_size']['excluded_n'] or row.get('missing_policy', 'error') != 'error':
            raise ValueError('Summary tradeoff coordinates need the same complete observations; do not pair independently excluded means')
        key = json.dumps([row['scope']['table_id'], sorted(row['scope']['filters'], key=lambda r: json.dumps(r, sort_keys=True))], sort_keys=True)
        cell = pairs.setdefault(key, {})
        if row['metric'] in cell:
            raise ValueError('Each tradeoff scope needs exactly one verified x and y mean')
        cell[row['metric']] = row
    panels = {}
    for cell in pairs.values():
        if set(cell) != metrics or cell[x_metric]['sample_size'] != cell[y_metric]['sample_size']:
            raise ValueError('Tradeoff x/y means must cover the same observation scope and sample')
        x, y = cell[x_metric], cell[y_metric]
        facet = {key: x['coordinates'][key] for key in facets}
        key = json.dumps(facet, sort_keys=True)
        panel = panels.setdefault(key, dict(id=f'panel_{len(panels)+1}', finding_ids=[], facet=facet,
                                           quantity=dict(metric=y_metric, operation='mean', unit=y['unit']),
                                           interval={}, comparisons={}, series_labels={}, arguments={}))
        panel['finding_ids'].extend([x['id'], y['id']])
        panel.setdefault('pairs', []).append((x, y))
    for panel in panels.values():
        interval_kinds = {(r['interval']['kind'], r['interval']['level'], r['interval']['method'])
                          for pair in panel['pairs'] for r in pair if r.get('interval')}
        if len(interval_kinds) == 1:
            kind, level, method = next(iter(interval_kinds))
            panel['interval'] = dict(kind=kind, level=level, method=method)
        items = ([item for item in dimensions[series] if any(x['coordinates'][series] == item['value'] for x, _ in panel['pairs'])]
                 if series else [dict(value=None, artist_label='Estimate', display_label='Estimate', color=contract['colors'][0],
                                      edge=contract['edge_colors'][0], marker=contract['markers'][0])])
        values = {item['artist_label']: [[x['value'], y['value']] for x, y in panel['pairs']
                                         if series is None or x['coordinates'][series] == item['value']] for item in items}
        panel['observations'] = []
        for x, y in panel['pairs']:
            rx, ry = x.get('observations'), y.get('observations')
            if not rx or not ry or rx['row_indices'] != ry['row_indices'] or rx['table_id'] != ry['table_id']:
                continue
            i = next(i for i, item in enumerate(items) if series is None or x['coordinates'][series] == item['value'])
            panel['observations'].append(dict(finding_ids=[x['id'], y['id']], series_index=i,
                color=items[i]['edge'], values=list(map(list, zip(rx['values'], ry['values']))),
                kind='coordinate_pair', row_indices=rx['row_indices'], table_id=rx['table_id'],
                mean=[x['value'], y['value']], intervals=[x.get('interval'), y.get('interval')]))
        labels = mapping['value_labels']
        x, y = panel.pop('pairs')[0]
        panel['series_labels'] = {item['artist_label']: item['display_label'] for item in items}
        panel['arguments'] = dict(series=values, labels=[i['artist_label'] for i in items],
                                  colors=[i['color'] for i in items], edges=[i['edge'] for i in items],
                                  markers=[i['marker'] for i in items], sizes=[24]*len(items), title='',
                                  xlabel=axis_label(x_metric, 'mean', x['unit'], labels[x_metric], figure['title']),
                                  ylabel=axis_label(y_metric, 'mean', y['unit'], labels[y_metric], figure['title']),
                                  legend_location=contract['legend_location'])
        panel['quantity']['label'] = panel['arguments']['ylabel']
        for coordinate, limits_key in [(0, 'xlim'), (1, 'ylim')]:
            numbers = ([point[coordinate] for points in values.values() for point in points]
                       + [point[coordinate] for layer in panel['observations'] for point in layer['values']]
                       + [interval[key] for layer in panel['observations']
                          for interval in [layer['intervals'][coordinate]] if interval
                          for key in ('lower', 'upper')])
            low, high = min(numbers), max(numbers)
            pad = max((high-low)*.12, max(abs(low), abs(high))*.02, .01)
            panel['arguments'][limits_key] = [low-pad, high+pad]
    for key in ('xlim', 'ylim'):
        low = min(panel['arguments'][key][0] for panel in panels.values())
        high = max(panel['arguments'][key][1] for panel in panels.values())
        for panel in panels.values():
            panel['arguments'][key] = [low, high]
    return list(panels.values())


class FigureDataError(ValueError):
    def __init__(self, figure_id, message):
        self.figure_id = figure_id
        super().__init__(message)


def english_panel_labels(panels, aliases):
    """Apply presentation aliases without touching values, scopes or artist identities."""
    def label(value):
        text = english_display(value, aliases)
        if CJK.search(text):
            raise ValueError('Supply English data_mapping display labels for: ' + text)
        return text

    for panel in panels:
        panel['description'] = label(panel['description'])
        panel['quantity']['label'] = label(panel['quantity']['label'])
        panel['series_labels'] = {key: label(value) for key, value in panel['series_labels'].items()}
        panel['comparisons'] = {key: [label(value) for value in pair]
                                for key, pair in panel['comparisons'].items()}
        arguments = panel['arguments']
        for key in ('title', 'xlabel', 'ylabel'):
            if key in arguments:
                arguments[key] = label(arguments[key])
        for key in ('categories', 'benchmarks'):
            if key in arguments:
                arguments[key] = [label(value) for value in arguments[key]]
                if len(set(arguments[key])) != len(arguments[key]):
                    raise ValueError('English display labels must keep categories distinct')


def panel_caption(view):
    """Keep the exact panel-to-scope key outside the image, even after a model edits its caption."""
    panels = view.get('panels', [])
    if not panels:
        return ''
    key = ('Scope: ' + panels[0]['description'] if len(panels) == 1 else
           'Panels: ' + '; '.join(f"{panel['label']} {panel['description']}" for panel in panels)) + '.'
    if any(panel.get('test_adjustment') == 'holm' and panel['interval'].get('kind') == 'confidence'
           for panel in panels):
        key += ' Intervals are marginal; Holm correction applies to p values, not interval endpoints.'
    if view.get('observation_note'):
        key += ' ' + view['observation_note']
    return key


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
        dimensions[category] = [dict(value=row['id'], artist_label=row['id'],
                                    display_label=result_label(row, facets))
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
        observations = (grid_observations(selected, dimensions, grid, series, category,
                                          (mapping['series_column'] is None and category != 'finding_id')
                                          or (figure['style'] == 'point_interval' and category == 'comparison'))
                        if figure['style'] == 'point_interval' else [])
        if observations:
            numbers = [v for layer in observations for v in layer['values']]
            for key in ('value_limits', 'difference_limits'):
                low, high = min(grid[key][0], min(numbers)), max(grid[key][1], max(numbers))
                pad = max((high-low)*.04, .01)
                grid[key] = [low-pad, high+pad]
        arguments = dict(title='')
        difference = operation in DIFFERENCES
        category_label = mapping.get('category_label') or display_name(category.split('.', 1)[-1])
        value_label = mapping.get('value_labels', {}).get(metric, mapping.get('value_label'))
        ylabel = axis_label(metric, operation, unit, value_label, figure['title'])
        if len(set(grid['categories'])) != len(grid['categories']):
            raise ValueError('Result labels must distinguish actual scopes/estimands; do not use opaque IDs')
        if (difference and category != 'comparison' and mapping['series_column'] is not None
                and set(grid['comparisons']) != set(grid['methods'])):
            raise ValueError('Each difference series needs one explicit subtraction; use finer facets or result categories')
        style = figure['style']
        # With no separate series, categorical rows/bars carry identity themselves.
        # Numeric category IDs are still categories; trajectory x values are colored by series instead.
        category_styles = (grid['category_styles'] if mapping['series_column'] is None
                           and category != 'finding_id' else None)
        if style == 'point_interval' and category == 'comparison':
            category_styles = grid['category_styles']
        if style == 'point_interval':
            arguments.update(summaries=grid['summaries'], methods=grid['methods'], categories=grid['categories'],
                             colors=grid['colors'], edges=grid['edges'], markers=grid['markers'],
                             xlabel=ylabel,
                             xlim=grid['difference_limits'] if difference else grid['value_limits'])
            arguments['category_styles'] = category_styles
        elif style == 'bar_grouped_hatch':
            arguments.update(data_dict=grid['values'], colors=grid['bar_colors'], hatches=[''] * len(grid['methods']),
                             labels=grid['methods'], x_positions=list(range(len(grid['categories']))), total_w=.78,
                             xlim=[-.56, len(grid['categories']) - .44],
                             legend_anchor=contract['layout']['legend_anchor'], benchmarks=grid['categories'],
                             best_method=None, xlabel=category_label, ylabel=ylabel, y_limits=grid['bar_limits'])
            arguments['category_styles'] = category_styles
            arguments['intervals'] = {key: [triple[1:] for triple in values]
                                      for key, values in grid['summaries'].items()}
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
                             markers=grid['markers'], line_styles=grid['line_styles'], xlabel=category_label,
                             ylabel=ylabel, xlim=grid['x_limits'], ylim=grid['value_limits'],
                             legend_location=contract['legend_location'])
            if style == 'line_confidence_band':
                arguments['xscale'] = scale
            elif scale != 'linear':
                raise ValueError('The selected training-curve helper uses a linear x axis')
        panels.append(dict(id=f'panel_{len(panels) + 1}', finding_ids=[row['id'] for row in selected],
                           observations=observations,
                           quantity=dict(metric=metric, operation=operation, unit=result_unit(operation, unit), label=ylabel),
                           interval={key: value for key, value in (selected[0]['interval'] or {}).items()
                                     if key in ('kind', 'level', 'method')},
                           facet={key: selected[0]['coordinates'][key] for key in facets},
                           test_adjustment=('holm' if any((row.get('test') or {}).get('adjustment') == 'holm'
                                                         for row in selected) else None),
                           comparisons=grid['comparisons'],
                           series_labels=dict(zip(grid['methods'], grid['display_labels'])), arguments=arguments))
    # Facets of the same quantity use a common scale; different metrics retain their own units.
    limits_key = 'xlim' if figure['style'] == 'point_interval' else 'y_limits' if figure['style'] == 'bar_grouped_hatch' else 'ylim'
    for panel in panels:
        comparable = [p['arguments'][limits_key] for p in panels if p['quantity'] == panel['quantity']]
        panel['arguments'][limits_key] = [min(pair[0] for pair in comparable), max(pair[1] for pair in comparable)]
    return panels


def _label_panels(panels):
    for index, panel in enumerate(panels):
        label = f'({chr(97 + index)})' if len(panels) <= 26 else f'({index + 1})'
        panel['label'] = label if len(panels) > 1 else ''
        # A short scope label is part of the encoding, not the external figure title.
        scope_label = ' / '.join(str(value) if len(panel['facet']) == 1 else
                                 f'{display_name(key.split(".", 1)[-1])} = {value}'
                                 for key, value in panel['facet'].items())
        panel['arguments']['title'] = ' '.join(part for part in [panel['label'], scope_label] if part)
        scope = [str(value) if len(panel['facet']) == 1 else
                 f'{display_name(key.split(".", 1)[-1])} = {value}' for key, value in panel['facet'].items()]
        quantity = panel['quantity']
        scope.append(quantity['label'])
        panel['description'] = '; '.join(scope)
    return panels


def prepare_figure_data(figure, metadata, contract):
    """Preserve all selected findings once; native-ready arguments never require model-side joins."""
    rows = metadata.get('result_records')
    if not rows:
        return None
    try:
        if len({row['id'] for row in rows}) != len(rows):
            raise ValueError('Duplicate finding identities in figure input')
        view = dict(schema='paper.figure.v1', chart_type=figure['kind'], style=figure['style'], title=figure['title'],
                    points=[_point(row) for row in rows], panels=[],
                    context={key: metadata.get(key, '') for key in ('user_request', 'source_description')})
        if figure['style'] in GRID_STYLES or figure['style'] == 'scatter_tradeoff':
            view['panels'] = (_tradeoff_panels(figure, metadata, contract) if figure['style'] == 'scatter_tradeoff'
                              else _panels(figure, metadata, contract))
            _label_panels(view['panels'])
            english_panel_labels(view['panels'], (figure.get('data_mapping') or {}).get('display_labels', {}))
            view['observation_note'] = observation_note(view)
            actual = [identifier for panel in view['panels'] for identifier in panel['finding_ids']]
            if sorted(actual) != sorted(row['id'] for row in rows):
                raise ValueError('Prepared panels must retain every selected finding exactly once')
        return view
    except (ValueError, KeyError, TypeError) as exc:
        raise FigureDataError(figure['id'], str(exc)) from exc
