# Quantitative matrix heatmaps

Call `paper_extended.py::draw_heatmap` then `draw_colorbar` on a dedicated colorbar axis. All cells use a shared numeric scale; do not assign colors by method or normalize each row unless the chosen mode explicitly calls for it. Render with pcolormesh, not an embedded bitmap.

The helper arguments are `(ax, values, row_labels, column_labels, title, xlabel, ylabel, limits, signed, number_format, suffix, palette, mode)`.

- `matrix`: actual/verified cells, including None/NaN gaps labeled NA; use **viridis** or **cividis**, signed=False. The benchmark matrix's **Blues alternative was rejected** and must not be offered or generated.
- `difference`: already verified absolute differences in consistent units (e.g. pp); use PuOr, signed=True, symmetric limits around zero, and a signed format such as `+.1f`.
- `correlation`: actual verified symmetric correlations with the same features on both axes, defined diagonal=1, fixed (-1,1), PuOr. Disclose the method and missing-data policy. Do not infer causal or significance claims from color.
- `correlation_pairs`: verified anchor-variable associations, optionally in separate cohort rows. Rectangular cells use PuOr, signed=True and fixed (-1,1); this is not a full symmetric matrix. In F4 use the host's `quantity_semantics.pair_rectangle` coordinates/values. Name the anchor and method, disclose per-cell n and missing handling; do not invent absent variable pairs.
- `confusion_counts`: complete nonnegative integer counts, same ordered classes on both axes, true rows/predicted columns; Blues and signed=False.
- `confusion_row`: the SAME raw integer counts; the helper divides each nonempty row by its support exactly once. Use (0,100), Blues, and a colorbar labeled row proportion (%). A zero-support row is NA. Do not feed in percentages.

Use `heatmap_layouts` for the chosen mode. Its positions give data axes and separate colorbar axes; confusion uses two panels and two independently labeled color scales. Matrix 89×88 mm, difference 89×84, correlation 89×91, two confusion panels 182×82. Long labels or more cells may require more space. Correlation column labels may rotate 40 degrees, anchored right.

`draw_colorbar(fig, image, colorbar_ax, label, ticks)` keeps the colorbar vector and its compact 7–7.5 pt type. Use the same limits across comparable panels. Captions state values/units, any actual normalization, missing cells and provenance. Mark missing cells rather than filling them with zero.

Bind `ax = fig.add_axes(entry['panel'])` and `colorbar_ax = fig.add_axes(entry['colorbar'])`
from the selected heatmap layout. Pass `ax` to `draw_heatmap` and `colorbar_ax` to
`draw_colorbar`; using the data axis for the colorbar is rejected before it changes
the panel. The paper renderer wraps long column names at word boundaries using
measured cell widths, or rotates an unbreakable label, while preserving the font
size and exact label words. Overlapping labels still fail the final layout check.

For verified posterior-event probabilities use matrix mode with limits (0,1), and label
the scale with the actual event, e.g. `P(p > 0.80 | data)`. This is neither a posterior mean
nor a p value. F4 supplies the label in `quantity_semantics.value_label` and retains it on
the dedicated colorbar. `number_format='probability'` displays values below 0.001 as
`<0.001` and above 0.999 as `>0.999`, avoiding apparent exact zero/one from rounding.
Keep event/model details in the separate caption and return a short title, e.g. "Threshold exceedance", as metadata.
The helper uses Matplotlib mathtext for inequality signs; literal `<`, `>` and `|` can
map to different glyphs in the Computer Modern text font. Preserve the supplied mathtext
event label instead of stripping its dollar delimiters or replacing `\mid` with a text bar.
