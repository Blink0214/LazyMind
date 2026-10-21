"""Standalone generated-code runner, invoked only through container execution transports."""
from __future__ import annotations

import base64
import contextlib
import copy
import inspect
import io
import json
import re
import sys
import traceback


def prepared_call_guard(namespace, payload):
    """Verify actual native calls retain every host-bound number, identity and range."""
    if payload['spec'].get('data_contract', {}).get('prepared_view_version') != 1:
        return lambda figure: None
    panels = copy.deepcopy(payload['table']['figure']['panels'])
    name = payload['spec']['style_contract']['native_template']
    native = namespace[name]
    signature = inspect.signature(native)
    used, axes = set(), set()
    states = []

    def plain(value):
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(item) for item in value]
        if hasattr(value, 'tolist'):
            return plain(value.tolist())
        return value

    def guarded(*args, **kwargs):
        bound = dict(signature.bind(*args, **kwargs).arguments)
        axis = bound.pop('ax')
        title = bound.pop('title', '')
        title = title if isinstance(title, str) and re.fullmatch(r'\([A-Za-z]\)', title) else ''
        actual = plain(bound)
        match = next((i for i, panel in enumerate(panels) if i not in used and actual == plain(
            {key: value for key, value in panel['arguments'].items() if key != 'title'})), None)
        if match is None:
            raise ValueError("Call the native function with one unchanged panel['arguments']; "
                             'do not rebuild data, interval endpoints, series identities or axis limits')
        if id(axis) in axes:
            raise ValueError('Prepared panel scopes need separate axes; do not overlay different quantities')
        used.add(match)
        axes.add(id(axis))
        result = native(ax=axis, title=title, **bound)
        states.append((axis, axis.get_xlim(), axis.get_ylim(), axis.get_xscale(), axis.get_yscale()))
        return result

    namespace[name] = guarded

    def complete(figure=None):
        if len(used) != len(panels):
            raise ValueError('Draw every prepared panel exactly once; selected findings cannot be omitted')
        for axis, xlim, ylim, xscale, yscale in states:
            if ((figure is not None and axis not in figure.axes) or axis.get_xlim() != xlim
                    or axis.get_ylim() != ylim or axis.get_xscale() != xscale or axis.get_yscale() != yscale):
                raise ValueError('Keep prepared numeric/category axis limits and scales; do not crop or reverse them')

    return complete


def apply_title_policy(figure, spec):
    """Titles belong to Chart.title/caption; retain only subplot letters on the canvas."""
    removed = [spec.get('title', '')]
    if figure._suptitle is not None:
        removed.append(figure._suptitle.get_text())
        figure._suptitle.set_text('')
    for axis in figure.axes:
        for artist in (axis._left_title, axis.title, axis._right_title):
            title = artist.get_text()
            removed.append(title)
            match = re.match(r'^\s*(\([A-Za-z]\))(?:\s|$)', title)
            artist.set_text(match.group(1) if match else '')
    titles = {' '.join(text.split()) for text in removed if text.strip()}
    for text in [*figure.texts, *(text for axis in figure.axes for text in axis.texts)]:
        if ' '.join(text.get_text().split()) in titles:
            text.set_text('')


def validate_correlation_heatmaps(figure, spec):
    """Check cell identities and coefficients against host-bound verified findings."""
    import numpy as np
    from matplotlib.collections import QuadMesh
    from matplotlib.colors import TwoSlopeNorm

    semantics = spec.get('style_contract', {}).get('quantity_semantics', {})
    if spec.get('style') != 'heatmap_matrix' or semantics.get('value_kind') != 'correlation_coefficient':
        return
    observed = []
    for axis in figure.axes:
        if axis.get_gid() == 'paper-colorbar':
            continue
        meshes = [item for item in axis.collections if isinstance(item, QuadMesh)]
        if len(meshes) != 1:
            raise ValueError('相关热力图每面板须使用原生 draw_heatmap 绘制一个矩阵')
        mesh = meshes[0]
        xs = [' '.join(t.get_text().split()) for t in axis.get_xticklabels()]
        ys = [' '.join(t.get_text().split()) for t in axis.get_yticklabels()]
        actual = np.ma.filled(np.ma.asarray(mesh.get_array(), dtype=float), np.nan)
        if (not isinstance(mesh.norm, TwoSlopeNorm) or mesh.norm.vcenter != 0
                or mesh.get_clim() != (-1, 1) or mesh.cmap.name != 'PuOr'):
            raise ValueError('相关热力图必须使用零中心 PuOr 及统一 [-1,1] 色域')
        observed.append((xs, ys, actual))

    def matches(observation, matrix):
        xs, ys, actual = observation
        if 'row_labels' in matrix:
            expected_x = [' '.join(name.split()) for name in matrix['column_labels']]
            expected_y = [' '.join(name.split()) for name in matrix['row_labels']]
            values = np.asarray(matrix['values'], dtype=float)
            for columns, rows, cells in [(expected_x, expected_y, values), (expected_y, expected_x, values.T)]:
                if (len(xs) == len(columns) and len(ys) == len(rows)
                        and set(xs) == set(columns) and set(ys) == set(rows)):
                    expected = np.asarray([[cells[rows.index(y), columns.index(x)] for x in xs] for y in ys])
                    if actual.shape == expected.shape and np.allclose(
                            actual, expected, rtol=1e-7, atol=1e-10, equal_nan=True):
                        return True
            return False
        else:
            variables = {' '.join(name.split()) for name in matrix['variables']}
            if len(xs) != len(variables) or len(ys) != len(variables) or set(xs) != variables or set(ys) != variables:
                return False
            expected = np.full((len(ys), len(xs)), np.nan)
            for cell in matrix['cells']:
                x, y = ' '.join(cell['x'].split()), ' '.join(cell['y'].split())
                expected[ys.index(y), xs.index(x)] = cell['value']
                expected[ys.index(x), xs.index(y)] = cell['value']
        return actual.shape == expected.shape and np.allclose(actual, expected, rtol=1e-7, atol=1e-10, equal_nan=True)

    alternatives = [semantics['matrices']]
    if semantics.get('pair_rectangle'):
        alternatives.append([semantics['pair_rectangle']])
    valid = False
    for representation in alternatives:
        pending = list(representation)
        for observation in observed:
            matched = next((i for i, matrix in enumerate(pending) if matches(observation, matrix)), None)
            if matched is None:
                break
            pending.pop(matched)
        else:
            if not pending:
                valid = True
                break
    if not valid:
        raise ValueError('相关热力图单元格、变量标签、队列或缺失位置与已验证结果不一致；禁止错位或补零/补一')
    colorbars = [axis for axis in figure.axes if axis.get_gid() == 'paper-colorbar']
    if not colorbars:
        raise ValueError('相关热力图须调用 draw_colorbar，明确标注 Pearson r 或 Spearman rho')
    labels = {('Pearson r' if method == 'pearson' else 'Spearman rho') for method in semantics['methods']}
    if any(axis.get_ylabel() not in labels for axis in colorbars):
        raise ValueError('相关热力图色条必须标注实际系数 Pearson r 或 Spearman rho')


def apply_quantity_semantics(figure, spec):
    """Use verified quantity types for references/scale labels; never alter estimates."""
    import numpy as np

    contract = spec.get('style_contract', {})
    semantics = contract.get('quantity_semantics', {})
    if spec.get('style') == 'heatmap_matrix' and semantics.get('value_kind') == 'posterior_event_probability':
        colorbars = [axis for axis in figure.axes if axis.get_gid() == 'paper-colorbar']
        if not colorbars:
            raise ValueError('后验概率热力图须调用 skill 的 draw_colorbar 创建带语义的色标')
        for axis in colorbars:
            axis.set_ylabel(semantics['value_label'])
    if spec.get('style') != 'point_interval' or semantics.get('value_kind') != 'posterior_parameter_mean':
        return
    for axis in figure.axes:
        if axis.get_gid() == 'paper-colorbar':
            continue
        for reference in semantics.get('reference_values', []):
            value = reference['value']
            for line in axis.lines:
                if (line.get_transform() == axis.get_yaxis_transform()
                        and np.allclose(line.get_ydata(), value)):
                    raise ValueError('概率阈值属于横向数值轴：不要使用 axhline；由宿主绘制 x=阈值的竖线')
            low, high = axis.get_xlim()
            axis.set_xlim(min(low, value - .02), max(high, value + .02))
            axis.axvline(value, color='#888888', linestyle='--', linewidth=.65,
                         zorder=0, label='_nolegend_')
            axis.set_title(axis.get_title(), pad=19)
            axis.text(value, 1.025, reference['label'], transform=axis.get_xaxis_transform(),
                      fontsize=7, color='#505050', va='center', ha='center', clip_on=False)


def fit_paper_layout(figure, spec):
    """Fit final-size labels and exterior legends using actual font bounds, without a vision model."""
    from matplotlib.text import Text
    from matplotlib.collections import LineCollection
    from matplotlib.container import ErrorbarContainer
    from matplotlib.colors import to_rgba
    import numpy as np

    contract = spec.get('style_contract', {})
    if (contract.get('version') != 'paper-v1' or not contract.get('fit_paper_layout')
            or spec.get('style') == 'custom'):
        return None
    axes = [axis for axis in figure.axes if axis.get_gid() != 'paper-colorbar']
    if not axes or any(axis.name == 'polar' for axis in axes):
        return None
    axes.sort(key=lambda axis: axis.get_position().x0)
    if max(axis.get_position().y0 for axis in axes) - min(axis.get_position().y0 for axis in axes) > .05:
        raise ValueError('当前论文布局适配器要求同一行面板，请为多行布局选择明确的版式')
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    width, height = figure.bbox.width, figure.bbox.height
    pad = figure.dpi * 4 / 72
    positions = [axis.get_window_extent(renderer).frozen() for axis in axes]
    colorbars = [(axis, axis.get_window_extent(renderer).frozen()) for axis in figure.axes
                 if axis.get_gid() == 'paper-colorbar']

    def off_view_tick_labels():
        hidden = set()
        for axis in figure.axes:
            for coordinate in (axis.xaxis, axis.yaxis):
                # Match Axis.draw in the pinned runtime: locators may return ticks outside the view.
                drawn = set(coordinate._update_ticks())
                hidden.update(label for tick in coordinate.get_major_ticks() + coordinate.get_minor_ticks()
                              if tick not in drawn for label in (tick.label1, tick.label2))
        return hidden

    hidden_ticks = off_view_tick_labels()

    def bounds(texts):
        return [text.get_window_extent(renderer) for text in texts
                if text not in hidden_ticks and text.get_visible() and text.get_text().strip()]

    def wrap(text, available):
        # Whitespace wrapping preserves words and the chosen point size. Math labels stay intact.
        if '$' in text.get_text():
            return
        lines = []
        for paragraph in text.get_text().split('\n'):
            line = ''
            for word in paragraph.split():
                candidate = (line + ' ' + word).strip()
                measured = renderer.get_text_width_height_descent(candidate, text.get_fontproperties(), False)[0]
                if line and measured > available:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        text.set_text('\n'.join(lines))

    overhangs = []
    for axis, box in zip(axes, positions):
        labels = bounds([*axis.get_xticklabels(), *axis.get_yticklabels(), axis.xaxis.label, axis.yaxis.label])
        overhangs.append((max([box.x0 - b.x0 for b in labels] + [0]),
                          max([b.x1 - box.x1 for b in labels] + [0]),
                          max([box.y0 - b.y0 for b in labels] + [0])))
    left = max(positions[0].x0, overhangs[0][0] + pad)
    right = max(width - positions[-1].x1, overhangs[-1][1] + pad)
    if colorbars:
        all_labels = bounds([text for axis, _ in colorbars for text in axis.findobj(Text)])
        right += max([b.x1 - width + pad for b in all_labels] + [0])
    gaps = [max(positions[i + 1].x0 - positions[i].x1,
                overhangs[i][1] + overhangs[i + 1][0] + 2 * pad) for i in range(len(axes) - 1)]
    panel_width = (width - left - right - sum(gaps)) / len(axes)
    if panel_width < 95 * figure.dpi / 100:
        raise ValueError('类别文字过长，当前栏宽无法容纳；请使用双栏画布或明确的短标签，不能缩小字体')
    bottom = max(min(box.y0 for box in positions), max(h[2] for h in overhangs) + pad)
    x = left
    for i, (axis, box) in enumerate(zip(axes, positions)):
        axis.set_position([x / width, bottom / height, panel_width / width, (box.y1 - bottom) / height])
        wrap(axis.title, panel_width)
        x += panel_width + (gaps[i] if i < len(gaps) else 0)

    # Identical legends share one strip. Different comparison semantics keep their panel scopes.
    entries = []
    for index, axis in enumerate(axes):
        legend = axis.get_legend()
        if legend is not None:
            entries.append((legend, [index]))
    entries.extend((legend, list(range(len(axes)))) for legend in list(figure.legends))
    groups = {}
    for legend, owners in entries:
        texts = legend.get_texts()
        labels = [text.get_text() for text in texts]
        handles = list(legend.legend_handles)
        # Legend.legend_handles stores only the first proxy for a composite errorbar.
        # Reuse the original container so relocating its key retains circle/square markers.
        if spec.get('style') == 'point_interval':
            candidates = [handle for i in owners for handle in axes[i].get_legend_handles_labels()[0]
                          if isinstance(handle, ErrorbarContainer)]
            for index, handle in enumerate(handles):
                if isinstance(handle, LineCollection) and len(handle.get_colors()):
                    matching = [candidate for candidate in candidates
                                if np.allclose(to_rgba(candidate.lines[0].get_markeredgecolor()),
                                               handle.get_colors()[0])]
                    if matching:
                        handles[index] = matching[0]
        styles = []
        for handle in handles:
            if isinstance(handle, ErrorbarContainer):
                handle = handle.lines[0]
            styles.append(tuple(str(getattr(handle, name)()) for name in
                                ['get_color', 'get_marker', 'get_markerfacecolor', 'get_markeredgecolor',
                                 'get_linestyle', 'get_facecolor', 'get_hatch']
                                if hasattr(handle, name)))
        key = (tuple(labels), tuple(styles))
        if key in groups:
            groups[key]['owners'].update(owners)
        else:
            groups[key] = dict(handles=handles, labels=labels, owners=set(owners),
                               fonts=[text.get_fontproperties().copy() for text in texts])
        legend.remove()
    top = height - pad
    if figure._suptitle is not None and figure._suptitle.get_text():
        title = figure._suptitle
        wrap(title, width - 2 * pad)
        title.set_position((.5, top / height))
        title.set_ha('center')
        title.set_va('top')
        figure.canvas.draw()
        top = title.get_window_extent(renderer).y0 - pad
    legends = []
    for group in groups.values():
        owners = sorted(group['owners'])
        first, last = axes[owners[0]].get_position(), axes[owners[-1]].get_position()
        available = width - 2 * pad if len(owners) == len(axes) else (last.x1 - first.x0) * width
        center = .5 if len(owners) == len(axes) else (first.x0 + last.x1) / 2
        for columns in range(len(group['labels']), 0, -1):
            legend = figure.legend(group['handles'], group['labels'], loc='upper center',
                                   bbox_to_anchor=(center, top / height), ncol=columns,
                                   prop=group['fonts'][0], frameon=True, fancybox=False, framealpha=1,
                                   edgecolor='#C8C8C8', borderpad=.28, columnspacing=1.1,
                                   handlelength=1.7, handletextpad=.45, borderaxespad=0)
            for text, font in zip(legend.get_texts(), group['fonts']):
                text.set_fontproperties(font)
            figure.canvas.draw()
            if legend.get_window_extent(renderer).width <= available or columns == 1:
                break
            legend.remove()
        legends.append(legend)
    ceiling = min([legend.get_window_extent(renderer).y0 - pad for legend in legends] + [top])
    figure.canvas.draw()
    adornment = max([text.get_window_extent(renderer).y1 - axis.bbox.y1
                    for axis in axes for text in [axis.title, *axis.texts]
                    if text.get_visible() and text.get_text()] + [0])
    data_top = min(min(box.y1 for box in positions), ceiling - adornment - pad)
    minimum = 34 / 25.4 * figure.dpi
    if data_top - bottom < minimum:
        extra = minimum - (data_top - bottom)
        figure.set_size_inches(figure.get_figwidth(), (height + extra) / figure.dpi)
        height += extra
        data_top += extra
        for legend in legends:
            anchor = legend.get_bbox_to_anchor()
            legend.set_bbox_to_anchor((anchor.x0 / width, (top + extra) / height))
        if figure._suptitle is not None:
            figure._suptitle.set_y(1 - pad / height)
    for axis in axes:
        box = axis.get_position()
        axis.set_position([box.x0, bottom / height, box.width, (data_top - bottom) / height])
    if colorbars:
        last = axes[-1].get_position()
        for axis, box in colorbars:
            offset = box.x0 - positions[-1].x1
            axis.set_position([last.x1 + offset / width, bottom / height,
                               box.width / width, (data_top - bottom) / height])
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    clipped = []
    hidden_ticks = off_view_tick_labels()
    for text in figure.findobj(Text):
        if text not in hidden_ticks and text.get_visible() and text.get_text().strip():
            box = text.get_window_extent(renderer)
            if (box.x0 < -.5 or box.y0 < -.5 or box.x1 > figure.bbox.width + .5
                    or box.y1 > figure.bbox.height + .5):
                clipped.append(text.get_text())
    if clipped:
        raise ValueError('论文画布存在被裁切文字，请调整标签或栏宽: ' + repr(clipped[:5]))
    return dict(texts_inside_canvas=True, legends_outside_data=True, figure_legends=len(legends),
                font_sizes_preserved=True)


def apply_appearance(figure, spec):
    """Apply declared skill appearance to existing artists; never change their data."""
    from matplotlib.container import BarContainer
    from matplotlib.collections import PathCollection
    from matplotlib.patches import PathPatch
    from matplotlib.text import Text

    contract = spec.get('style_contract', {})
    if not contract.get('enforce_appearance'):
        return
    rc = contract['rc']
    colors = contract.get('series_colors', {})
    bar_colors = contract.get('bar_series_colors', {})
    palette = contract['bar_colors']
    component_colors = {}
    for text in figure.findobj(Text):
        text.set_fontfamily(rc.get('font.family', 'serif'))
    if figure._suptitle is not None:
        figure._suptitle.set_fontsize(rc['axes.titlesize'])
        figure._suptitle.set_fontweight(rc['axes.titleweight'])
    for axis in figure.axes:
        if contract.get('version') == 'paper-v1' and axis.get_gid() == 'paper-colorbar':
            continue  # Dedicated numeric scale: preserve its vector mesh and compact typography.
        polar = axis.name == 'polar'
        preserve_axes = (contract.get('version') == 'paper-v1' and contract.get('preserve_template_axes')
                         and spec.get('style') != 'custom')
        if not polar and not preserve_axes:
            for side in ('top', 'right'):
                axis.spines[side].set_visible(rc.get('axes.spines.' + side, False))
            for spine in axis.spines.values():
                spine.set_linewidth(rc['axes.linewidth'])
                spine.set_edgecolor(rc['axes.edgecolor'])
            axis.grid(False, axis='both')
            if rc.get('axes.grid'):
                axis.grid(True, axis=rc.get('axes.grid.axis', 'both'), color=rc['grid.color'],
                          linewidth=rc['grid.linewidth'], linestyle=rc['grid.linestyle'])
            axis.set_axisbelow(rc.get('axes.axisbelow', True))
            axis.tick_params(axis='x', direction=rc.get('xtick.direction', 'out'),
                             labelsize=rc['xtick.labelsize'])
            axis.tick_params(axis='y', direction=rc.get('ytick.direction', 'out'),
                             labelsize=rc['ytick.labelsize'])
        axis.title.set_fontsize(rc['axes.titlesize'])
        axis.title.set_fontweight(rc['axes.titleweight'])
        for label in (axis.xaxis.label, axis.yaxis.label):
            label.set_fontsize(rc['axes.labelsize'])
            label.set_fontweight(rc.get('axes.labelweight', 'normal'))

        artists = {}
        for line in axis.lines:
            name = line.get_label()
            if name in colors:
                preserve_line_colors = (contract.get('version') == 'paper-v1'
                                        and contract.get('preserve_template_line_colors')
                                        and spec.get('style') != 'custom')
                if not preserve_line_colors:
                    line.set_color(colors[name])
                line.set_linewidth(contract.get('line_width', line.get_linewidth()))
                artists[name] = line
        for collection in axis.collections:
            name = collection.get_label()
            if isinstance(collection, PathCollection) and name in colors:
                collection.set_facecolor(colors[name])
                if 'scatter_size' in contract and len(collection.get_sizes()) <= 1:
                    collection.set_sizes([contract['scatter_size']])
                if 'scatter_alpha' in contract:
                    collection.set_alpha(contract['scatter_alpha'])
                    collection.set_linewidth(contract['scatter_linewidth'])
                artists[name] = collection

        containers = [c for c in axis.containers if isinstance(c, BarContainer) and c.patches]
        for index, container in enumerate(containers):
            tick_labels = (axis.get_yticklabels() if container.orientation == 'horizontal'
                           else axis.get_xticklabels())
            labels = [t.get_text() for t in tick_labels]
            for i, patch in enumerate(container.patches):
                name = container.get_label()
                native_components = (spec.get('style') == 'bar_stacked' and contract.get('version') == 'paper-v1'
                                     and contract.get('preserve_template_bar_colors'))
                if native_components:
                    from matplotlib.colors import to_hex

                    color = to_hex(patch.get_facecolor()).upper()
                    if color not in contract['component_colors']:
                        raise ValueError('堆叠组成图必须使用已确认的 component_colors 配色')
                    if name.startswith('_') or component_colors.get(name, color) != color:
                        raise ValueError('同一组成部分跨面板必须使用一致的标签和颜色')
                    component_colors[name] = color
                    artists[name] = patch
                    continue
                if name not in bar_colors and patch.get_label() in bar_colors:
                    name = patch.get_label()
                if name not in bar_colors and len(labels) == len(container.patches) and labels[i] in bar_colors:
                    name = labels[i]
                # When a group mapping exists, ambiguous series must be labelled before exporting.
                if bar_colors and name not in bar_colors:
                    raise ValueError('柱状图需用真实分组名标记柱系列或类别刻度，才能应用统一配色')
                color = bar_colors.get(name, palette[(index if len(containers) > 1 else i) % len(palette)])
                patch.set_facecolor(color)
                patch.set_edgecolor('white')
                patch.set_linewidth(contract.get('bar_linewidth', 0.8))
                artists[name] = patch
        # Box geometry is still generated by the model; label/tick binding preserves colors after sorting.
        box_geometry = str(spec.get('kind', contract.get('kind', ''))).lower() in {'box', 'boxplot'}
        boxes = [p for p in axis.patches if isinstance(p, PathPatch)] if box_geometry else []
        ticks = [t.get_text() for t in axis.get_xticklabels()]
        for i, patch in enumerate(boxes):
            name = patch.get_label()
            if name not in colors and len(boxes) == len(ticks):
                name = ticks[i]
            if name in colors:
                patch.set_facecolor(colors[name])

        legend = axis.get_legend()
        if legend is not None:
            legend.set_frame_on(rc.get('legend.frameon', True))
            legend.get_frame().set_edgecolor(rc['legend.edgecolor'])
            legend.get_frame().set_alpha(rc['legend.framealpha'])
            legend.get_frame().set_boxstyle('square', pad=0)
            # Template-based bars already choose a corner/anchor together with their panel layout.
            preserve_legend = contract.get('preserve_legend_location')
            if not preserve_legend:
                legend.set_loc('best')
            for handle, text in zip(legend.legend_handles, legend.get_texts()):
                text.set_fontsize(rc['legend.fontsize'])
                actual = artists.get(text.get_text())
                if actual is None:
                    continue
                if hasattr(actual, 'get_facecolor') and hasattr(handle, 'set_facecolor'):
                    handle.set_facecolor(actual.get_facecolor())
                    handle.set_edgecolor(actual.get_edgecolor())
                elif hasattr(actual, 'get_color') and hasattr(handle, 'set_color'):
                    handle.set_color(actual.get_color())
                if hasattr(actual, 'get_hatch') and hasattr(handle, 'set_hatch'):
                    handle.set_hatch(actual.get_hatch())
                    text.set_fontweight('bold' if actual.get_hatch() else 'normal')
    # Matplotlib ships Computer Modern regular/bold as separate font families; no external TeX process is needed.
    if contract.get('bold_font_family'):
        from matplotlib import font_manager

        for text in figure.findobj(Text):
            bold = text.get_fontweight() in ('bold', 'heavy', 'semibold', 'demibold', 600, 700, 800, 900)
            if contract.get('version') == 'paper-v1' and text.get_text().isascii():
                path = font_manager.findfont(contract['bold_font_family'] if bold else rc['font.serif'][0],
                                             fallback_to_default=False)
                text.set_fontproperties(font_manager.FontProperties(
                    fname=path, size=text.get_fontsize(), weight=text.get_fontweight()))
            elif bold:
                text.set_fontfamily([contract['bold_font_family'], 'serif'])


def render(payload: dict) -> dict:
    contract = payload['spec'].get('style_contract', {})
    if contract.get('version') not in (None, 'paper-v1'):
        raise ValueError('仅支持当前 paper-v1 绘图模板格式')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.container import BarContainer
    from matplotlib.collections import LineCollection, PathCollection, QuadMesh
    from matplotlib.figure import Figure
    from matplotlib.text import Text
    import numpy as np

    fonts = {font.name for font in font_manager.fontManager.ttflist}
    family = next((name for name in ('Noto Sans CJK SC', 'Noto Sans CJK JP', 'Arial Unicode MS')
                   if name in fonts), 'DejaVu Sans')
    rc = {'font.family': ['DejaVu Sans', family], 'font.size': 9}
    rc.update(payload['spec'].get('style_contract', {}).get('rc', {}))
    palette = payload['spec'].get('style_contract', {}).get('colors')
    if palette:
        rc['axes.prop_cycle'] = matplotlib.cycler(color=palette)
    # Serif Latin text remains serif; CJK falls back per glyph rather than replacing the entire font family.
    if rc['font.family'] == 'serif':
        rc['font.serif'] = [*rc.get('font.serif', ['STIXGeneral', 'DejaVu Serif']), family]
    # Vector glyph paths preserve the selected font even on a viewer without STIX/CJK installed.
    rc.update({'svg.fonttype': 'path', 'pdf.fonttype': 42, 'text.usetex': False, 'axes.unicode_minus': False,
               'svg.hashsalt': payload['data_sha256']})
    with plt.rc_context(rc):
        namespace = {}
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(payload['code'], '<generated-plot>', 'exec'), namespace)
            verify_prepared_calls = prepared_call_guard(namespace, payload)
            figure = namespace['draw'](payload['table'], payload['statistics'], payload['spec'])
            verify_prepared_calls(figure)
        if not isinstance(figure, Figure) or not figure.axes:
            raise ValueError('draw 必须返回包含坐标轴的 Matplotlib Figure')
        width, height = figure.get_size_inches()
        if not 1 <= width <= 20 or not 1 <= height <= 20:
            raise ValueError('画布宽高必须在 1..20 英寸之间')
        if not any(ax.has_data() for ax in figure.axes):
            raise ValueError('图表没有数据图元')
        for axis in figure.axes:
            for container in axis.containers:
                if isinstance(container, BarContainer) and container.patches:
                    bounds = axis.get_xlim() if container.orientation == 'horizontal' else axis.get_ylim()
                    if not min(bounds) <= 0 <= max(bounds):
                        raise ValueError('比较柱状图的数值轴必须包含零基线，不能截断轴范围夸大差异')
        validate_correlation_heatmaps(figure, payload['spec'])
        apply_quantity_semantics(figure, payload['spec'])
        apply_appearance(figure, payload['spec'])
        apply_title_policy(figure, payload['spec'])
        layout_checks = fit_paper_layout(figure, payload['spec'])
        for text in figure.findobj(Text):
            text.set_usetex(False)
        output = {}
        artist_data = None
        if payload.get('include_artist_data'):
            # Optional numeric integration diagnostics, not visual quality review.
            artist_data = {'bar_values': [], 'scatter_offsets': [], 'marker_offsets': [], 'axes_style': [],
                           'figure_inches': figure.get_size_inches().tolist(), 'layout_checks': layout_checks,
                           'figure_title': figure._suptitle.get_text() if figure._suptitle is not None else '',
                           'figure_texts': [text.get_text() for text in figure.texts if text.get_text()],
                           'figure_legend_labels': [[text.get_text() for text in legend.get_texts()]
                                                    for legend in figure.legends]}
            for axis in figure.axes:
                artist_data['axes_style'].append({
                    'title': axis.get_title(), 'title_font': axis.title.get_fontproperties().get_name(),
                    'side_titles': {loc: axis.get_title(loc=loc) for loc in ('left', 'right')},
                    'position': list(axis.get_position().bounds),
                    'texts': [{'text': text.get_text(), 'position': np.asarray(text.get_position()).tolist(),
                               'size': text.get_fontsize(), 'weight': text.get_fontweight()}
                              for text in axis.texts],
                    'spines': {side: spine.get_visible() for side, spine in axis.spines.items()},
                    'x_ticks': [t.get_text() for t in axis.get_xticklabels()],
                    'x_label': axis.get_xlabel(), 'y_label': axis.get_ylabel(),
                    'error_segments': [np.asarray(segment).tolist() for c in axis.collections
                                       if isinstance(c, LineCollection) for segment in c.get_segments()],
                    'legend': axis.get_legend() is not None,
                    'legend_labels': ([t.get_text() for t in axis.get_legend().get_texts()]
                                      if axis.get_legend() is not None else []),
                    'legend_keys': ([{'hatch': h.get_hatch(), 'edgecolor': np.asarray(h.get_edgecolor()).tolist(),
                                      'fontweight': t.get_fontweight()}
                                     for h, t in zip(axis.get_legend().legend_handles, axis.get_legend().get_texts())
                                     if hasattr(h, 'get_hatch')] if axis.get_legend() is not None else []),
                    'bars': [{'color': list(p.get_facecolor()), 'hatch': p.get_hatch(),
                              'label': c.get_label(), 'center': float(p.get_x() + p.get_width() / 2),
                              'value': float(p.get_height())}
                             for c in axis.containers if isinstance(c, BarContainer) for p in c.patches],
                    'lines': [{'label': line.get_label(), 'color': matplotlib.colors.to_hex(line.get_color()),
                               'x': np.asarray(line.get_xdata()).tolist(), 'y': np.asarray(line.get_ydata()).tolist()}
                              for line in axis.lines],
                    'collections': [{'label': c.get_label(), 'colors': c.get_facecolors().tolist(),
                                     'offsets': c.get_offsets().tolist()}
                                    for c in axis.collections if isinstance(c, PathCollection)],
                    'numeric_fields': [{'kind': type(c).__name__, 'values': np.ma.asarray(c.get_array()).tolist(),
                                        'cmap': c.get_cmap().name, 'limits': list(c.get_clim()),
                                        'rasterized': bool(c.get_rasterized())}
                                       for c in axis.collections if c.get_array() is not None
                                       and (isinstance(c, QuadMesh) or not isinstance(c, PathCollection))],
                })
                for container in axis.containers:
                    if isinstance(container, BarContainer):
                        artist_data['bar_values'].extend(
                            float(patch.get_width() if container.orientation == 'horizontal' else patch.get_height())
                            for patch in container.patches)
                for collection in axis.collections:
                    if isinstance(collection, PathCollection):
                        artist_data['scatter_offsets'].extend(collection.get_offsets().tolist())
                for line in axis.lines:
                    if line.get_marker() not in (None, '', 'None', 'none', ' ') and line.get_linestyle() == 'None':
                        artist_data['marker_offsets'].extend(
                            [[float(x), float(y)] for x, y in zip(line.get_xdata(), line.get_ydata())])
        for extension in ('svg', 'png', 'pdf'):
            stream = io.BytesIO()
            metadata = {'Creator': 'LazyMind F4'} if extension != 'png' else {'Software': 'LazyMind F4'}
            if extension == 'svg':
                metadata['Date'] = None
            preserve_canvas = payload['spec'].get('style_contract', {}).get('preserve_canvas')
            figure.savefig(stream, format=extension, dpi=300,
                           bbox_inches=None if preserve_canvas else 'tight', metadata=metadata)
            raw = stream.getvalue()
            limit = (2 if extension == 'svg' else 4) * 1024 * 1024
            if len(raw) > limit:
                raise ValueError(f'{extension} 图表过大，请减少图元或画布尺寸')
            if extension == 'svg':
                text = raw.decode('utf-8')
                output['svg'] = text[text.index('<svg'):]
            else:
                output[extension] = base64.b64encode(raw).decode('ascii')
        plt.close('all')
        result = {'svg': output['svg'], 'exports': {key: output[key] for key in ('png', 'pdf')}}
        if artist_data is not None:
            result['artist_data'] = artist_data
        return result


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        print(json.dumps(render(payload), ensure_ascii=False, allow_nan=False))
    except Exception:
        print(json.dumps({'error': traceback.format_exc(limit=8)[-6000:]}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
