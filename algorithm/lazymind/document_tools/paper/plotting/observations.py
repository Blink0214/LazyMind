"""Attach original observations to verified mean/paired estimates, never reconstruct samples."""
import math

from ..analysis.reference import select


def bind_observations(finding, job, tables):
    operation = job['operation']
    if operation not in ('mean', 'paired_difference', 'paired_t') or job['table_id'] not in tables:
        return None
    table = tables[job['table_id']]
    column = table['columns'].index(job['column'])
    rows = table['rows']
    selected = select(table, job['filters'])
    used = [i for i in selected if rows[i][column] is not None]
    excluded = len(selected) - len(used)
    identities = [[i] for i in used]
    values = [rows[i][column] for i in used]
    if operation != 'mean':
        if not job['pair_by']:
            return None
        base = select(table, job['baseline_filters'])
        keys = [table['columns'].index(key) for key in job['pair_by']]

        def keyed(indices):
            result = {}
            for i in indices:
                key = tuple(rows[i][k] for k in keys)
                if None in key or key in result:
                    return None
                result[key] = i
            return result

        first, second = keyed(selected), keyed(base)
        if first is None or second is None or set(selected) & set(base):
            return None
        common = [key for key in first if key in second
                  and rows[first[key]][column] is not None and rows[second[key]][column] is not None]
        identities = [[first[key], second[key]] for key in common]
        values = [rows[a][column] - rows[b][column] for a, b in identities]
        excluded = len(set(first) | set(second)) - len(common)
    if (not values or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
            or len(values) != finding['sample_size']['n']
            or excluded != finding['sample_size']['excluded_n']
            or not math.isclose(math.fsum(values) / len(values), finding['value'], rel_tol=1e-7, abs_tol=1e-9)):
        return None  # Aggregates without matching original observations must stay aggregates.
    return dict(kind='paired_difference' if operation != 'mean' else 'observation',
                table_id=job['table_id'], row_indices=identities, values=values, excluded_n=excluded)


def grid_observations(rows, dimensions, grid, series, category, category_colors=False):
    """Return exact host-owned layers aligned with the native series/category order."""
    names = {item['value']: item['artist_label'] for item in dimensions[series]}
    layers = []
    for row in rows:
        raw = row.get('observations')
        if not raw:
            continue
        name = names[row['coordinates'][series]]
        i, k = grid['methods'].index(name), grid['x'].index(row['coordinates'][category])
        appearance = grid['category_styles'][k] if category_colors and grid['category_styles'] else None
        layers.append(dict(finding_id=row['id'], series_index=i, category_index=k,
                           color=appearance['edge'] if appearance else grid['edges'][i], **raw))
    return layers


def observation_note(view):
    panels = view.get('panels', [])
    layers = [layer for panel in panels for layer in panel.get('observations', [])]
    if not layers:
        return ''
    sizes = sorted({len(layer['values']) for layer in layers})
    n = str(sizes[0]) if len(sizes) == 1 else f'{sizes[0]}-{sizes[-1]}'
    intervals = {(panel['interval'].get('kind'), panel['interval'].get('level')) for panel in panels}
    interval = (next(iter(intervals)) if len(intervals) == 1 else (None, None))
    uncertainty = (f"; {interval[1]*100:g}% credible interval" if interval[0] == 'credible' else
                   f"; pointwise {interval[1]*100:g}% CI" if interval[0] == 'confidence' else '')
    paired = all(layer['kind'] == 'paired_difference' for layer in layers)
    count = 'pairs' if paired else 'observations'
    return f'Small dots: {"paired differences" if paired else "original observations"}; large markers: verified estimates{uncertainty}. Valid n = {n} {count} per group.'
