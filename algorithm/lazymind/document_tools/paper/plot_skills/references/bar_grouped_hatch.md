# Grouped method comparisons

Select this representation when the question is how named methods compare within
the same categories. Keep experimental settings in separate panels or apply an
explicit filter first. Each displayed method/category cell must refer to a single
observation or a verified summary of the intended replicates.

## Interface and binding

The source callable is `scripts/bar_grouped.py::draw_panel`. In an application
generated program it is installed under the name **`draw_reference_panel`**:

```python
draw_reference_panel(ax, data_dict, colors, hatches, labels, title, x_positions,
                     total_w, xlim, legend_anchor, benchmarks, best_method,
                     xlabel, ylabel, y_limits)
```

- `labels` contains distinct method names; `benchmarks` contains the displayed category names.
- `data_dict[label]` has one value per category in exactly that order. Use NaN for an unknown cell; infinite values are invalid.
- `colors` comes from `bar_series_colors` keyed by `labels`. `hatches` contains `//` only for an explicitly designated `best_method`, otherwise empty strings. Use `best_method=None` when none was designated.
- `x_positions` has one position per category. For unit-spaced positions starting at zero, use `total_w=0.78` and `xlim=(-0.56, len(benchmarks)-0.44)`.
- `y_limits` must include zero and every visible value, with extra space for annotations. `xlabel` and `ylabel` name the actual quantities and units.
- `title` is empty or a panel identifier. `legend_anchor` is an axes-relative position chosen with the panel layout.

The helper divides the total group width among all methods. It omits unknown bars
and their value labels, adds white seams and numeric labels, and emphasizes only
the designated focal method. Label boldness does not identify a statistical winner.

## Presentation checks

Preserve method appearance across panels even if category or method order changes.
Do not collapse unexamined duplicate cells with an arbitrary average, silently
drop a method to fit the palette, or turn missing cells into zero-height bars.

The application can fit method legends outside the observations after applying
final font metrics. A category-only display should not repeat its axis labels in a
legend. Do not overlay another symbol for the same mean. This helper has no interval
argument: if uncertainty is central, use `point_interval` or a deliberately planned
custom geometry with verified endpoints.

Return the research question as figure metadata. The caption explains panel scope,
aggregation, units and any absent cells; the drawing does not establish significance.
