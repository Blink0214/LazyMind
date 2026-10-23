"""Carry verified finding scopes and original group identities into v2.1 plotting."""
import copy
import json

from ..analysis.schema import table_map
from .style_profiles import bind_series
from .labels import comparison_coordinate
from .observations import bind_observations


APPEARANCE = ('series_colors', 'bar_series_colors', 'series_edges', 'series_markers',
              'series_line_styles', 'series_sizes')


def correlation_rectangle(jobs, findings):
    """Optional sparse association view: one anchor variable against several outcomes."""
    if len({(j['operation'], j['table_id'], j['column']) for j in jobs}) != 1:
        return None
    if any(j['column'] == j['other_column'] for j in jobs):
        return None
    rules = [{rule['column']: rule['value'] for rule in j['filters'] if rule['operator'] == 'eq'} for j in jobs]
    # Only equality scopes can be represented by a cohort's exact raw label.
    if any(len(values) != len(j['filters']) for values, j in zip(rules, jobs)):
        return None
    keys = set(rules[0])
    if any(set(rule) != keys for rule in rules):
        return None
    varying = [key for key in sorted(keys) if len({json.dumps(rule[key]) for rule in rules}) > 1]
    if len(varying) == 2 and len({j['other_column'] for j in jobs}) == 1:
        row_key = next((key for key in varying if key.rsplit('.', 1)[-1] == 'method'), varying[0])
        column_key = next(key for key in varying if key != row_key)
        row_labels = list(dict.fromkeys(str(rule[row_key]) for rule in rules))
        column_labels = list(dict.fromkeys(str(rule[column_key]) for rule in rules))
        values = [[None] * len(column_labels) for row in row_labels]
        for job, rule in zip(jobs, rules):
            row_id, column_id = row_labels.index(str(rule[row_key])), column_labels.index(str(rule[column_key]))
            values[row_id][column_id] = findings[job['id']]['value']
        return dict(row_labels=row_labels, column_labels=column_labels, values=values,
                    anchor=jobs[0]['column'], outcome=jobs[0]['other_column'], method=jobs[0]['operation'],
                    row_factor=row_key, column_factor=column_key)
    if len(varying) > 1:
        return None
    labels = ([str(rule[varying[0]]) for rule in rules] if varying else [jobs[0]['column']] * len(jobs))
    rows = list(dict.fromkeys(labels))
    columns = list(dict.fromkeys(j['other_column'] for j in jobs))
    values = [[None] * len(columns) for row in rows]
    for job, label in zip(jobs, labels):
        values[rows.index(label)][columns.index(job['other_column'])] = findings[job['id']]['value']
    return dict(row_labels=rows, column_labels=columns, values=values, anchor=jobs[0]['column'],
                method=jobs[0]['operation'])


def figure_semantics(selected, selected_jobs, all_findings):
    """Describe verified posterior quantities, never infer a threshold from a title."""
    operations = {job['operation'] for job in selected_jobs}
    if operations and operations <= {'pearson', 'spearman'}:
        label = ('Pearson r' if operations == {'pearson'} else 'Spearman rho' if operations == {'spearman'}
                 else 'Correlation coefficient')
        matrices = {}
        findings = {f['id']: f for f in selected}
        for job in selected_jobs:
            scope = json.dumps([job['operation'], job['table_id'],
                                sorted(job['filters'], key=lambda f: json.dumps(f, sort_keys=True))], sort_keys=True)
            matrix = matrices.setdefault(scope, dict(method=job['operation'], variables=[], cells=[]))
            for column in (job['column'], job['other_column']):
                if column not in matrix['variables']:
                    matrix['variables'].append(column)
            matrix['cells'].append(dict(x=job['column'], y=job['other_column'],
                                        value=findings[job['id']]['value'], finding_id=job['id']))
        return dict(value_kind='correlation_coefficient', value_label=label, reference_values=[],
                    methods=sorted(operations), limits=[-1, 1], missing_cells='NA',
                    diagonal='Use explicit verified self-correlation findings only; otherwise NA.',
                    cell_sample_sizes={f['id']: f['sample_size']['n'] for f in selected},
                    matrices=list(matrices.values()),
                    pair_rectangle=correlation_rectangle(selected_jobs, findings))
    if operations == {'beta_probability'}:
        events = [finding['probability'] for finding in selected]
        signatures = {(event['operator'], event['threshold']) for event in events if event}
        if len(signatures) == 1 and all(events):
            operator, threshold = next(iter(signatures))
            symbol = {'gt': '>', 'ge': r'\geq', 'lt': '<', 'le': r'\leq'}[operator]
            return dict(value_kind='posterior_event_probability',
                        value_label=rf'$P(p {symbol} {threshold:.2f} \mid \mathrm{{data}})$', reference_values=[])
    if operations == {'beta_mean'}:
        models = {finding['model_id'] for finding in selected}
        events = [f for f in all_findings if f.get('model_id') in models and f.get('probability')]
        covered = {f['model_id'] for f in events}
        thresholds = {f['probability']['threshold'] for f in events}
        references = []
        if models and None not in models and models <= covered and len(thresholds) == 1:
            threshold = next(iter(thresholds))
            references.append(dict(value=threshold, label=f'p = {threshold:.2f}'))
        return dict(value_kind='posterior_parameter_mean', value_label='Success probability',
                    reference_values=references)
    return {}


def bind_context(contract, inputs, jobs):
    result = copy.deepcopy(contract)
    if inputs is None:
        return result, {}
    tables = table_map(inputs)
    refs = [(ref['table_id'], ref['column']) for ref in (inputs.get('study_design') or {}).get('grouping', [])]
    for job in jobs:
        for rule in job['filters'] + job['baseline_filters']:
            table, column = job['table_id'], rule['column']
            if table not in tables and '.' in column:
                table, column = column.split('.', 1)
            if table in tables and column in tables[table]['columns']:
                kind = tables[table]['column_types'][tables[table]['columns'].index(column)]
                if kind in ('string', 'integer', 'boolean', 'date'):
                    refs.append((table, column))
    groups, aliases = [], {}
    for table_id, column in dict.fromkeys(refs):
        table = tables[table_id]
        index = table['columns'].index(column)
        values = list(dict.fromkeys(row[index] for row in table['rows'] if row[index] is not None))
        selected = bind_series(contract, values)
        keys, labels = [], []
        for value in values:
            label = str(value)
            key = 'group:' + json.dumps([table_id, column, value], ensure_ascii=False, separators=(',', ':'))
            appearance = {name: selected[name][label] for name in APPEARANCE if name in selected}
            for name, item in appearance.items():
                result.setdefault(name, {})[key] = item
            aliases.setdefault(label, []).append(appearance)
            keys.append(key)
            labels.append(label)
        groups.append(dict(table_id=table_id, column=column, values=values,
                           canonical_labels=keys, display_labels=labels))
    # Bare labels are convenient only when the source columns agree on every appearance attribute.
    for label, choices in aliases.items():
        if all(choice == choices[0] for choice in choices[1:]):
            for name, item in choices[0].items():
                if label not in result[name] or result[name][label] == item:
                    result[name][label] = item
    for group in groups:
        group['artist_labels'] = []
        for label, key in zip(group['display_labels'], group['canonical_labels']):
            unambiguous = all(result[name].get(label) == result[name][key]
                              for name in APPEARANCE if name in result)
            group['artist_labels'].append(label if unambiguous else key)
    contrast_order = []
    for job in jobs:
        if job['operation'] not in ('difference', 'paired_t', 'paired_difference', 'welch_t'):
            continue
        coordinates = {prefix + (job['table_id'] + '.' + rule['column'] if job['table_id'] in tables
                                  else rule['column']): rule['value']
                       for field, prefix in [('filters', ''), ('baseline_filters', 'baseline.')]
                       for rule in job[field] if rule['operator'] == 'eq'}
        contrast = comparison_coordinate(coordinates, job)
        if contrast and contrast[0] not in contrast_order:
            contrast_order.append(contrast[0])
    scopes = {job['id']: {key: copy.deepcopy(job[key]) for key in
              ('table_id', 'column', 'operation', 'filters', 'baseline_filters', 'pair_by')} for job in jobs}
    for job in jobs:
        if job['operation'] in ('pearson', 'spearman'):
            scopes[job['id']].update(other_column=job['other_column'],
                                     missing_policy=job['missing_policy'], correlation_test=job['correlation_test'])
    return result, dict(source_groups=groups, finding_scopes=scopes, comparison_order=contrast_order,
                        user_request=inputs['request'],
                        source_description=(inputs.get('data_context') or {}).get('description', ''))


def finding_table(findings, jobs, inputs):
    """Expose exact scalar result cells and their verified grouping coordinates, without guessing IDs."""
    columns = ['finding_id', 'value', 'lower', 'upper']
    core = [[f['id'], f['value'], (f['interval'] or {}).get('lower'),
             (f['interval'] or {}).get('upper')] for f in findings]
    if inputs is None or not jobs:
        return columns, core
    tables = table_map(inputs)
    indexed = {job['id']: job for job in jobs}
    coordinates, names = [], []
    for finding in findings:
        job = indexed[finding['id']]
        values = {'metric': job['column'], 'operation': job['operation'], 'unit': finding['unit'],
                  'n': finding['sample_size']['n'], 'n_unit': finding['sample_size']['unit']}
        if job['operation'] in ('pearson', 'spearman'):
            values.update(variable_x=job['column'], variable_y=job['other_column'],
                          correlation_method=job['operation'], excluded_n=finding['sample_size']['excluded_n'])
        for field, prefix in [('filters', ''), ('baseline_filters', 'baseline.')]:
            for rule in job[field]:
                if rule['operator'] == 'eq':
                    name = (job['table_id'] + '.' + rule['column'] if job['table_id'] in tables
                            else rule['column'])
                    values[prefix + name] = rule['value']
        coordinates.append(values)
        names.extend(name for name in values if name not in names)
    rows = [row + [values.get(name) for name in names] for row, values in zip(core, coordinates)]
    return columns + names, rows


def result_view(findings, jobs, inputs, contract, context):
    """Bind verified values, scopes and identity once, outside the model's drawing code."""
    if inputs is None or not jobs:
        return {}
    indexed = {job['id']: job for job in jobs}
    tables = table_map(inputs)
    records, observed, dimensions = [], {}, {}
    for finding in findings:
        job = indexed[finding['id']]
        coordinates = {}
        for field, prefix in [('filters', ''), ('baseline_filters', 'baseline.')]:
            for rule in job[field]:
                if rule['operator'] == 'eq':
                    ref = (job['table_id'] + '.' + rule['column'] if job['table_id'] in tables
                           else rule['column'])
                    coordinates[prefix + ref] = rule['value']
                    values = observed.setdefault(prefix + ref, [])
                    if rule['value'] not in values:
                        values.append(rule['value'])
        comparisons = {}
        if job['operation'] in ('difference', 'paired_t', 'paired_difference', 'welch_t'):
            for ref, value in coordinates.items():
                if not ref.startswith('baseline.') and 'baseline.' + ref in coordinates:
                    baseline = coordinates['baseline.' + ref]
                    if baseline != value:
                        pair = [str(value), str(baseline)]
                        comparisons[ref] = pair
                        comparisons['baseline.' + ref] = pair
        record = {key: copy.deepcopy(value) for key, value in finding.items()
                  if key not in ('statement', 'columns', 'calculation')}
        record.update(metric=job['column'], operation=job['operation'], coordinates=coordinates,
                      definition={key: job[key] for key in ('coefficient', 'other_column', 'threshold', 'operator')
                                  if job.get(key) is not None and
                                  (key != 'operator' or job.get('threshold') is not None)},
                      comparisons=comparisons,
                      scope={key: copy.deepcopy(job[key]) for key in
                             ('table_id', 'filters', 'baseline_filters', 'pair_by')},
                      missing_policy=job.get('missing_policy', 'error'))
        observations = bind_observations(finding, job, tables)
        if observations:
            record['observations'] = observations
        contrast = comparison_coordinate(coordinates, job) if comparisons else None
        if contrast:
            coordinates['comparison'] = contrast[0]
            comparisons['comparison'] = contrast[1]
            observed.setdefault('comparison', [])
            if contrast[0] not in observed['comparison']:
                observed['comparison'].append(contrast[0])
        records.append(record)
    for ref, values in observed.items():
        source_ref = ref.removeprefix('baseline.')
        source = next((group for group in context['source_groups']
                       if group['table_id'] + '.' + group['column'] == source_ref), None)
        universe = (context['comparison_order'] if ref == 'comparison' else
                    source['values'] if source else values)
        ordered = [value for value in universe if value in values]
        appearance = bind_series(contract, universe)
        items = []
        for value in ordered:
            label = str(value)
            if ref == 'comparison':
                pair = next(row['comparisons'][ref] for row in records if row['coordinates'].get(ref) == value)
                label = f'{pair[0]} - {pair[1]}'
                # Appearance follows a contrast identity, not either individual method.
            artist = source['artist_labels'][source['values'].index(value)] if source else label
            style = {field: (appearance[field][str(value)] if ref == 'comparison' else
                             contract.get(field, {}).get(artist, appearance[field][label])) for field in APPEARANCE}
            if ref == 'comparison':
                pair = json.loads(value)
                owner = next((group for group in context['source_groups']
                              if len(pair) == 1 and group['table_id'] + '.' + group['column'] == pair[0][0]), None)
                # Color retains the minuend group's identity; shape distinguishes its baselines.
                selected = (owner['canonical_labels'][owner['values'].index(pair[0][1])] if owner else None)
                for field, palette in [('series_colors', 'colors'), ('bar_series_colors', 'bar_colors'),
                                       ('series_edges', 'edge_colors')]:
                    style[field] = contract[field][selected] if selected else contract.get(palette, contract['colors'])[0]
            items.append(dict(value=value, artist_label=artist, display_label=label,
                              color=style['series_colors'], bar_color=style['bar_series_colors'],
                              edge=style['series_edges'], marker=style['series_markers'],
                              line_style=style['series_line_styles']))
        dimensions[ref] = items
    return dict(result_records=records, result_dimensions=dimensions)
