"""Choose a small figure set from executed results, without coordinate/mapping schemas."""
import copy
from typing import Annotated

from pydantic import Field

from .schema import Object, Text, parse


class FigureDraft(Object):
    title: Text
    purpose: Text
    style: Annotated[Text, Field(description='Exact catalog key, or custom when using a free-form geometry with base_style.')]
    result_indices: Annotated[list[int], Field(min_length=1)]
    requirements: str = ''
    base_style: str | None = Field(default=None, description='Exact reusable catalog key supplying appearance/helpers for custom geometry.')


class FigureArrangement(Object):
    figures: Annotated[list[FigureDraft], Field(max_length=20)]
    terminology: dict[str, str] = Field(default_factory=dict)
    series_order: list[str] = Field(default_factory=list)


def bind_figures(value, analysis):
    """Only bind real result references and available skill names; do not validate aesthetic choices."""
    from ..plotting.skills import PlotSkills
    arrangement = parse(FigureArrangement, value, 'analysis.figures', extra='ignore')
    catalog = PlotSkills().catalog()
    findings = analysis['findings']
    figures = []
    for index, draft in enumerate(arrangement['figures'], 1):
        # A free-form style description plus an explicit available base has one
        # unambiguous execution meaning: custom geometry using that base's helpers.
        if draft['style'] not in catalog and draft.get('base_style') in catalog and draft['base_style'] != 'custom':
            draft['requirements'] = '\n'.join(filter(None, (draft['style'], draft['requirements'])))
            draft['style'] = 'custom'
        style = draft['base_style'] if draft['style'] == 'custom' else draft['style']
        if style not in catalog or style == 'custom':
            raise ValueError(f'Figure {index}: unavailable skill {style!r}; available: {list(catalog)}')
        indices = list(dict.fromkeys(draft.pop('result_indices')))
        if any(position < 0 or position >= len(findings) for position in indices):
            raise ValueError(f'Figure {index}: result_indices must refer to positions 0..{len(findings)-1}')
        selected = [findings[position] for position in indices]
        columns = {(ref['table_id'], ref['column']): ref for row in selected for ref in row['columns']}
        figures.append(dict(copy.deepcopy(draft), id=f'fig{index}', caption='',
                            finding_ids=[row['id'] for row in selected], columns=list(columns.values())))
    return dict(figures=figures, terminology=arrangement['terminology'], series_order=arrangement['series_order'])
