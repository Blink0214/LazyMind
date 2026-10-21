"""Grouped bars using the gray/gray/blue paper preset.

Measurements, labels, ranges and focal method are arguments.
"""
import numpy as np
import matplotlib.patches as mpatches


def draw_panel(ax, data_dict, colors, hatches, labels, title, x_positions, total_w,
               xlim, legend_anchor, benchmarks, best_method, xlabel, ylabel, y_limits):
    if not labels or len(set(labels)) != len(labels):
        raise ValueError('Bar series labels must be nonempty and unique')
    if not (len(colors) == len(hatches) == len(labels)):
        raise ValueError('Supply one color and hatch per series')
    if len(x_positions) != len(benchmarks):
        raise ValueError('Category positions and labels must align')
    if not y_limits[0] <= 0 <= y_limits[1]:
        raise ValueError('Comparison bars require a zero baseline')
    n_groups = len(benchmarks)
    n_methods = len(labels)
    x = np.array(x_positions)
    bar_w = total_w / n_methods
    for i, (label, color, hatch) in enumerate(zip(labels, colors, hatches)):
        vals = np.asarray(data_dict[label], dtype=float)
        if len(vals) != n_groups or np.isinf(vals).any():
            raise ValueError('Bar values must align with categories and be finite or missing')
        if hatch and (label != best_method or hatch != '//'):
            raise ValueError('Only an explicitly supplied focal method may use // hatch')
        valid = np.isfinite(vals)
        offset = (i - n_methods / 2 + 0.5) * bar_w
        bars = ax.bar(x[valid] + offset, vals[valid], width=bar_w, color=color,
                      hatch=hatch, edgecolor='white', linewidth=0.45, zorder=2, label=label)
        is_best = label == best_method
        for bar, v in zip(bars, vals[valid]):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    v + (y_limits[1] - y_limits[0]) * (0.5 / 85), f'{v:.3g}',
                    ha='center', va='bottom', fontsize=7.2,
                    fontweight='bold' if is_best else 'normal', color='black', zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels(benchmarks, fontsize=8.0)
    ax.set_xlabel(xlabel, fontsize=8.5, labelpad=3.5)
    ax.set_ylabel(ylabel, fontsize=8.5, labelpad=4)
    ax.set_ylim(*y_limits)
    ax.set_xlim(*xlim)
    marker = title.split()[0] if title else ''
    marker = marker if len(marker) == 3 and marker[0] == '(' and marker[2] == ')' and marker[1].isalpha() else ''
    ax.set_title(marker, fontsize=9.0, pad=6)
    ax.yaxis.grid(True, color='#EBEBEB', linewidth=0.45, linestyle='--', zorder=0)
    ax.set_axisbelow(True)
    for side, spine in ax.spines.items():
        if side in ('top', 'right'):
            spine.set_visible(False)
        else:
            spine.set_linewidth(0.65)
            spine.set_color('#333333')
            spine.set_zorder(4)
    ax.tick_params(length=2.5, width=0.6, labelsize=8.0)
    handles = [mpatches.Patch(facecolor=color, hatch=hatch, edgecolor='white', linewidth=0.45, label=label)
               for label, color, hatch in zip(labels, colors, hatches)]
    leg = ax.legend(handles=handles, fontsize=7.3, loc='upper right', bbox_to_anchor=legend_anchor,
                    framealpha=1.0, facecolor='white', edgecolor='#C8C8C8', fancybox=False,
                    borderpad=0.28, labelspacing=0.26, handlelength=1.7,
                    handletextpad=0.45, borderaxespad=0.28)
    for text in leg.get_texts():
        if text.get_text() == best_method:
            text.set_fontweight('bold')
