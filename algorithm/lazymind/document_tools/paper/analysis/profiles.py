"""Bounded full-data support summaries for planning, never fabricated observations."""
from collections import Counter

from ..plotting.model import profile

MAX_LEVELS = 20
MAX_CELLS = 200


def support(values):
    counts = Counter(v for v in values if v is not None)
    result = dict(unique_count=len(counts), levels_complete=len(counts) <= MAX_LEVELS)
    if result['levels_complete']:
        result['levels'] = [dict(value=v, row_count=n) for v, n in sorted(counts.items())]
    return result


def planning_profile(table, grouping):
    result = profile({**table, 'options': {}})
    numeric = [c for c, t in zip(table['columns'], table['column_types']) if t in ('integer', 'number')]
    positions = {c: i for i, c in enumerate(table['columns'])}
    for column in numeric:
        result['columns'][column].update(support(row[positions[column]] for row in table['rows']))
    groups = list(dict.fromkeys([*grouping, *result['group_profiles'],
                                *[c for c in numeric if result['columns'][c]['levels_complete']]]))
    groups = [c for c in groups if c in positions]
    cells = {}
    for row in table['rows']:
        key = tuple(row[positions[c]] for c in groups)
        if key not in cells:
            cells[key] = dict(group=dict(zip(groups, key)), row_count=0,
                              non_null_counts=dict.fromkeys(numeric, 0)) if len(cells) < MAX_CELLS else None
        cell = cells[key]
        if cell is not None:
            cell['row_count'] += 1
            for column in numeric:
                cell['non_null_counts'][column] += row[positions[column]] is not None
    result['observed_cells'] = dict(grouping=groups, total_cells=len(cells),
                                    complete=len(cells) <= MAX_CELLS,
                                    cells=[cell for cell in cells.values() if cell is not None])
    return result
