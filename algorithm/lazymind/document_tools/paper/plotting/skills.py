"""Load bundled plotting references or an explicit override; never execute example code."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

from .templates import NATIVE_FUNCTION_GUIDANCE, paper_template

STYLES = {
    'bar_paired_delta': ('bar_paired.py', 'paired baseline/method bars'),
    'bar_grouped_hatch': ('bar_grouped.py', 'grouped bars with a highlighted method'),
    'line_confidence_band': ('line_selfdistill.py', 'curves with measured uncertainty'),
    'line_training_curve': ('line_training.py', 'training steps and measured metrics'),
    'box_raw_points': ('box_raw_points.py', 'independent-run distribution: boxes and all valid raw points'),
    'scatter_tradeoff': ('scatter_tradeoff.py', 'paired continuous measurements with stable shapes and colors'),
    'line_loss_with_inset': ('line_loss_inset.py', 'loss curves and a zoomed panel'),
    'scatter_tsne_cluster': ('scatter_tsne.py', 'existing embedding coordinates'),
    'scatter_broken_axis': ('scatter_break.py', 'scatter with an explicitly justified broken axis'),
    'radar_dual_series': ('radar_comparison.py', 'comparable dimensions with explicit scales'),
    'heatmap_matrix': ('paper_extended.py', 'quantitative matrix, signed differences, correlations or confusion counts'),
    'point_interval': ('paper_extended.py', 'verified point estimates and asymmetric interval endpoints'),
    'histogram_distribution': ('paper_extended.py', 'raw distribution with shared bins and explicit density units'),
    'ecdf_distribution': ('paper_extended.py', 'exact empirical cumulative distribution, without smoothing'),
    'bar_stacked': ('paper_extended.py', 'complete additive components and optional within-category proportions'),
    'classification_roc_pr': ('paper_extended.py', 'binary ROC and PR from paired true labels and prediction scores'),
    'density_hexbin': ('paper_extended.py', 'count-valued hexagonal bins preserving all paired observations'),
    'contour_response': ('paper_extended.py', 'response contours on an actual complete ordered two-dimensional grid'),
    'violin_raw_points': ('paper_extended.py', 'sample density shape, all raw points, median and IQR'),
}


class PlotSkills:
    def __init__(self, directory: str | None = None):
        configured = directory or os.environ.get('LAZYMIND_PAPER_PLOT_SKILLS')
        # Ship the reviewed templates with F4 so deployment does not depend on a sibling checkout.
        # Explicit overrides remain authoritative; a broken override must fail rather than silently fall back.
        self.root = (Path(configured).expanduser() if configured else
                     Path(__file__).resolve().parents[1] / 'plot_skills').resolve()
        self.hashes: dict[str, str] = {}
        self.instructions = self.read('SKILL.md')
        preset = 'references/paper_profile.json'
        self.paper_profile = json.loads(self.read(preset))
        if self.paper_profile.get('base', {}).get('version') != 'paper-v1':
            raise ValueError('未知的论文绘图 preset 版本，请核对 skill 与应用版本')

    def read(self, relative: str) -> str:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('绘图 skill 引用越出配置目录')
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            raise RuntimeError(f'缺少绘图 skill 资源或资源过大: {relative}；请配置 LAZYMIND_PAPER_PLOT_SKILLS')
        raw = path.read_bytes()
        self.hashes[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode('utf-8')

    def catalog(self) -> dict:
        available = {name: description for name, (_, description) in STYLES.items()
                     if name in self.paper_profile['styles']}
        return {**available,
                'custom': 'other chart geometry; MUST inherit appearance from a named catalog base_style'}

    def script(self, style: str) -> str:
        name = STYLES[style][0]
        path = 'scripts/' + name
        return self.read(path)

    def style_contract(self, style: str, base_style: str | None = None) -> dict:
        selected = base_style if style == 'custom' else style
        if selected not in STYLES or selected not in self.catalog():
            raise ValueError('每张图必须选择已有 skill；custom 必须明确有效的 base_style')
        reference_path = f'references/{selected}.md'
        self.read(reference_path)
        result = self._paper_contract(selected)
        result.update(source_style=selected, reference_path=reference_path, adapted=style == 'custom',
                      helper_module=Path(STYLES[selected][0]).stem,
                      enforce_appearance=True,
                      title_placement='metadata_only',
                      bar_colors=result['colors'])
        result['reference_legend_location'] = result.get('legend_location', 'upper right')
        result['legend_location'] = (result['reference_legend_location'] if result.get('preserve_legend_location') else
                                     'upper right' if selected == 'bar_grouped_hatch' else 'best')
        result['rules'] += [
            'Geometry and appearance are separate. Adapt this skill to the actual chart without importing '
            'sample measurements, labels, reference lines, embeddings or axis limits.',
            'Use style_contract.series_colors[group] for method series and bar_series_colors[group] for method bars. '
            'Native bar_stacked uses the explicit component_colors palette keyed by component identity; '
            'quantitative heatmaps and density fields use the selected numeric colormap. '
            'Do not assign new colors based on sorted positions or use a default Matplotlib palette.',
            'Label each series artist with its actual group. For category bars or boxes, label ticks with '
            'actual groups so the renderer can apply the same group-to-color binding after sorting.',
            'The renderer applies the declared fonts, axes and series appearance. Keep numeric positions, '
            'scales, data filtering, statistics and chart geometry in the generated code.',
            'Legend position adapts to actual data. Do not copy a reference corner when it covers observations.',
            'Return the figure title in Chart.title and panel descriptions in caption. '
            'Keep the full title/caption outside the image. Retain host-provided short scope labels on panels.',
        ]
        return result

    def _paper_contract(self, selected: str) -> dict:
        base = copy.deepcopy(self.paper_profile['base'])
        style = copy.deepcopy(self.paper_profile['styles'][selected])
        base.update({key: value for key, value in style.items() if key not in {'rc', 'rules'}})
        base['rc'].update(style.get('rc', {}))
        base['rules'].extend(style.get('rules', []))
        if (not base['colors'] or any(not re.fullmatch(r'#[0-9a-fA-F]{6}', c) for c in base['colors'])
                or base['native_template'] not in {'draw_reference_panel', 'draw_training_panel',
                                                   'draw_distribution', 'draw_tradeoff', 'draw_confidence_panel',
                                                   'draw_loss_zoom', 'draw_embedding', 'draw_broken_scatter',
                                                   'draw_radar', 'draw_paired_panel', 'draw_heatmap',
                                                   'draw_point_intervals', 'draw_histogram', 'draw_ecdf',
                                                   'draw_stacked', 'draw_classification', 'draw_hexbin',
                                                   'draw_contour', 'draw_violin'}):
            raise ValueError('论文绘图 preset 的配色或模板入口无效')
        return base

    def reference(self, style: str, base_style: str | None = None) -> str:
        guidance = ('Use only the actual data contract. Style does not prescribe sample data, categories, '
                    'axis limits, statistical methods or output paths. The host handles fonts and file export.\n')
        contract = self.style_contract(style, base_style)
        result = guidance + json.dumps(contract, ensure_ascii=False)
        result += '\n' + self.read('references/paper_style.md')
        result += '\n' + self.read(contract['reference_path'])
        script = self.script(contract['source_style'])
        common = self.read('scripts/paper_common.py')
        template = paper_template(script, common, contract['function'], contract['native_template'],
                                  tuple(contract.get('helpers', [])))
        availability = (NATIVE_FUNCTION_GUIDANCE if style != 'custom' else
                        'For custom geometry, include reviewed helper source if needed; '
                        'skill modules cannot be imported.\n')
        return result + (
            '\nAPPROVED DRAWING HELPERS (no example data or file I/O):\n' + availability
            + 'For native styles the host loads these reviewed functions unchanged. Return draw and '
            'data-mapping helpers, and CALL the native_template with its exact signature. '
            'Do not redefine native template helpers. '
            'Bind actual data, stable per-group appearance and data-dependent '
            'limits through the arguments. Missing values are not zero. For a genuinely custom '
            'geometry, inherit appearance without forcing the native helper.\n'
            'For one panel use figsize=layout["single_inches"] and '
            'fig.subplots_adjust(**layout["single_panel"]); for two use "two_inches" and "two_panels". '
            'Exceptions: draw_loss_zoom and draw_broken_scatter take an empty Figure created with '
            'plt.figure(figsize=style_contract["rc"]["figure.figsize"]); they create and lay out their axes. '
            'draw_radar requires subplot_kw={"projection":"polar"}. '
            'Heatmaps and count/response fields use their explicit data and colorbar axis positions; '
            'call draw_colorbar on the dedicated colorbar axis so it stays vector. '
            'Do not use constrained layout together with these margins. '
            'Native grouped bars and point intervals have legends fitted outside the observations; '
            'identical legends share a top strip unless host-bound observations use the compact statistical footer. '
            'The host handles original-observation overlays and notes; do not duplicate them. '
            'Other styles follow their selected reference. '
            'Titles/captions are returned as metadata, never drawn. Pass an empty string or only (a)/(b) '
            'to native title arguments. Put panel descriptions in caption. '
            'Read series_colors, series_edges, series_markers, series_line_styles and series_sizes '
            'by group name, never by a panel-specific sorted index.\n```python\n' + template + '\n```')
