"""Data-free appearance parameters for the shared plotting preset.

These profiles describe appearance, not chart geometry or measurements. The model
still chooses and implements the figure. Reference palettes are loaded separately.
"""
from __future__ import annotations

import copy

BAR_COLORS = ['#D3D3D3', '#A9A9A9', '#546BF5']
BLUE_EXTENSION = ['#AEC0FA', '#344BB5', '#7894DD', '#21356F']


def bind_series(contract: dict, groups: list) -> dict:
    result = copy.deepcopy(contract)
    colors = result['colors']
    # Stable input order, independent of sorting by mean/median in any individual figure.
    result['series_colors'] = {str(group): colors[i % len(colors)] for i, group in enumerate(groups)}
    bars = result.get('bar_colors', [*BAR_COLORS, *BLUE_EXTENSION])
    result['bar_series_colors'] = {str(group): bars[i % len(bars)] for i, group in enumerate(groups)}
    for source, target in [('edge_colors', 'series_edges'), ('markers', 'series_markers'),
                           ('line_styles', 'series_line_styles'), ('scatter_sizes', 'series_sizes')]:
        if source in result:
            values = result[source]
            result[target] = {str(group): values[i % len(values)] for i, group in enumerate(groups)}
    return result
