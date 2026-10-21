# Point estimates with verified intervals

Call `paper_extended.py::draw_point_intervals(ax, summaries, methods, categories, colors, edges, markers, title, xlabel, xlim)`.

`summaries[method]` is one `[estimate, lower, upper]` triple per actual category. All triples must be finite and ordered lower ≤ estimate ≤ upper. Use supplied/host-verified endpoints exactly, including asymmetry. The helper does not estimate uncertainty, resample or infer pairing. When a verified estimate has no interval, retain its point without an error bar; never invent endpoints. Unobserved cells stay missing and are never replaced by zero.

Use horizontal intervals with small identity-bound markers, x grid, category labels and a shared legend for adjacent related panels. The approved two-panel 182×78 mm layout reserves a top strip (top=0.78). Actual effects may use a null reference (zero for differences, one for ratios) only when meaningful. Means of positive outcomes need not start at zero because no bar length encodes the value. Keep every interval inside the x range.

Captions define the interval kind/level, independent sample unit and pairing (only when verified). The helper never converts interval overlap to a significance star.

## Difference plots: make the comparison explicit

For verified arithmetic differences, also call `paper_extended.py::label_difference_axis(ax, comparisons, better, direction_label)` after `draw_point_intervals`. This helper is included in the model reference. It returns `(handles, display_labels)` for `ax.legend` or `fig.legend`; **use the returned labels**, not the original baseline-only labels.

- `comparisons` maps every actual series identity to `[minuend_display_name, subtrahend_display_name]`, in the exact order used to compute the estimate and interval. A point for Proposed minus Baseline A must read **Proposed - Baseline A**, never just **Baseline A**. Do not reverse subtraction to make a result look positive. Keep the original artist labels for stable color/marker binding; the helper changes only legend display text.
- State the measured change and its unit on the x axis, e.g. **Accuracy change (pp)** or **Latency change (ms)**. Use actual method names in the legend. The separate title/caption states which method is compared with which baselines; zero means no difference. `pp` means percentage points, not relative percent change. Do not draw a panel title.
- Supply `better='higher'` or `'lower'` only when the metric's preference is known from the task or data contract. `direction_label` is a short human-readable description, e.g. **Higher accuracy is better** or **Lower latency is better**. The helper draws the corresponding right/left arrow above the plotting area. With unknown or non-monotonic preference, pass `None, None` and explain the sign without claiming improvement.
- Include zero and every interval in an increasing linear x range; the helper adds the zero reference. Use the two-panel top=0.78 strip (or equivalent space) to keep the legend, subplot identifiers and direction cues apart. Never place cues across observations.
- Share a legend only when the panels have the same ordered comparisons and appearances. An absolute-mean panel and a difference panel need separate, explicitly scoped legends.

This helper does not apply to absolute means, ratios, log ratios or odds ratios. Keep their original semantics and appropriate reference value. It never changes points, endpoints, inference or units. A confidence interval crossing zero is not by itself an instruction to label a result significant or nonsignificant.

## Posterior parameter probabilities

Absolute success probabilities remain on the horizontal numeric axis. A threshold such
as p=0.80 is a **vertical** reference; `axhline(0.80)` incorrectly draws it on the category
axis. F4 derives available thresholds from verified posterior events for the same model,
exposes them in `style_contract.quantity_semantics.reference_values` and draws them itself.
Do not add a second line/annotation. Standalone callers use `axvline` with the supplied
threshold. Never add a zero-difference reference or a subtraction legend to absolute probabilities.
Use only a subplot identifier in the title argument; the separate caption explains the posterior model and credible interval.
