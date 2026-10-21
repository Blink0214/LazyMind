---
name: plot-from-data
description: Select scientific figures for supplied observations or verified findings, map their fields to maintained Matplotlib helpers, and return publication-oriented plots with explicit units and captions.
---

# Scientific figure preparation

A figure must answer a stated research question with available evidence. Select its
geometry from the data and the comparison being made. Use the maintained drawing
interfaces to apply the agreed paper appearance; keep scientific computation and
presentation decisions explicit.

## Establish the evidence for each figure

Before generating a drawing program, identify:

1. The question, source table or verified finding IDs, and relevant experimental setting.
2. The displayed variables, units, method identities, categories and panel boundaries.
3. Whether each value is an observation, a summary estimate or a derived quantity.
4. The independent sampling unit, any pairing, and the handling of missing observations.
5. For uncertainty, the matching estimate and endpoints, interval definition and sample size.

Preserve the selected scope throughout field mapping. Repeated rows may represent
seeds, checkpoints or different conditions; establish their meaning before grouping.
A missing combination is unknown, not a measured zero. Do not expand aggregate
findings into artificial observations or obtain checkpoint intervals from a whole-run
summary. Estimate statistics in the analysis stage and draw the verified results.

Choose the number of figures from the distinct questions supported by the data.
There is no prescribed figure count. Combine complementary panels when useful, and
give each figure an independent specification suitable for parallel generation.

## Select a supported representation

| Evidence and purpose | Style | Input rules |
| --- | --- | --- |
| Compare method values within categories | `bar_grouped_hatch` | [Grouped cells](references/bar_grouped_hatch.md) |
| Display an ordered observed trajectory | `line_training_curve` | [Checkpoints](references/line_training_curve.md) |
| Show independent-run spread and individual values | `box_raw_points` | [Raw samples](references/box_raw_points.md) |
| Compare two measured continuous quantities | `scatter_tradeoff` | [Coordinate pairs](references/scatter_tradeoff.md) |
| Annotate absolute differences between two methods | `bar_paired_delta` | [Matched categories](references/bar_paired_delta.md) |
| Display an estimate and its uncertainty across x | `line_confidence_band` | [Pointwise endpoints](references/line_confidence_band.md) |
| Inspect a measured trajectory at two resolutions | `line_loss_with_inset` | [Linked zoom](references/line_loss_with_inset.md) |
| Present existing embedding coordinates | `scatter_tsne_cluster` | [Embedding evidence](references/scatter_tsne_cluster.md) |
| Display distant x ranges separated by an empty gap | `scatter_broken_axis` | [Complete window coverage](references/scatter_broken_axis.md) |
| Compare two methods across commensurate dimensions | `radar_dual_series` | [Common radial scale](references/radar_dual_series.md) |
| Show a measured or verified matrix | `heatmap_matrix` | [Matrix semantics](references/heatmap_matrix.md) |
| Compare estimates and explicit interval endpoints | `point_interval` | [Intervals and differences](references/point_interval.md) |
| Inspect frequency over common value bins | `histogram_distribution` | [Observed frequencies](references/histogram_distribution.md) |
| Compare exact empirical cumulative distributions | `ecdf_distribution` | [Empirical ranks](references/ecdf_distribution.md) |
| Compare additive totals or their component proportions | `bar_stacked` | [Additive components](references/bar_stacked.md) |
| Evaluate binary prediction scores | `classification_roc_pr` | [Paired labels and scores](references/classification_roc_pr.md) |
| Summarize dense paired coordinates by cell counts | `density_hexbin` | [Spatial counts](references/density_hexbin.md) |
| Display response over an observed complete grid | `contour_response` | [Grid completeness](references/contour_response.md) |
| Inspect sample density with individual observations | `violin_raw_points` | [Raw distributions](references/violin_raw_points.md) |

A style name does not establish that its prerequisites hold. Use an ordinary
scatter for measured tradeoffs, not an embedding label; use a line without a band
when only a trajectory is available. Differences require a full subtraction label,
including both compared methods. A direction cue requires known metric preference.
If no native geometry fits, declare `custom` with an explicit supported `base_style`
and implement the required geometry using that appearance.

## Build the drawing program

Read [paper_style.md](references/paper_style.md), the selected style's instructions
and its supplied helper interface. The merged `style_contract` from
[paper_profile.json](references/paper_profile.json) determines dimensions, fonts,
layout and appearance. Use the helper source supplied by the application as the
authority for callable names, arguments and return values.

The application installs native helpers into the generated program. Return the
`draw` entry point and the necessary data-binding functions, then call the declared
`native_template`. Do not redefine installed helpers or import demonstration scripts.
For grouped bars the installed name is `draw_reference_panel`; its source function
is `draw_panel` in `scripts/bar_grouped.py`.

Bind arrays and dictionaries from the actual input. Keep group appearance keyed by
identity using `series_colors`, `bar_series_colors`, `series_edges`,
`series_markers`, `series_line_styles` and `series_sizes`. Obtain each panel's
appearance arrays by looking up its ordered labels; filtering or sorting must not
reassign a method's appearance. All methods must remain represented even when their
count exceeds the first three palette entries. The focal method is an explicit
input designation, never a value inferred from a maximum.

For standard axes, choose `layout.single_inches` / `layout.single_panel` or
`layout.two_inches` / `layout.two_panels`. More panels need an explicit legible
layout. `draw_loss_zoom` and `draw_broken_scatter` instead receive an empty Figure
using `rc['figure.figsize']` and create their own axes. Radar axes must be polar.
Matrix and density styles need the dedicated colorbar axes required by their
references. Do not combine manually positioned axes with a conflicting automatic
layout engine.

## Keep visual encodings consistent

Method colors begin with light gray, darker gray and blue, as defined in the
contract. Extra method colors use its blue extensions. Shape and line style provide
additional identity cues. Sparse white hatching belongs only to a designated focal
method in method-comparison bars.

Some encodings describe quantities or components rather than methods:

- Result matrices use viridis or cividis.
- Signed differences and correlations use zero-centered symmetric PuOr; correlation bounds are -1 and 1.
- Confusion counts or row proportions use Blues. Normalize raw counts once; zero-support rows remain undefined.
- Hexbin counts and response grids use viridis with a labeled colorbar.
- Additive stacks use the contract's component identity palette, beginning with blue, orange and green.

Keep these meanings separate. A component or numeric field must not be recolored
using the method palette merely to make every figure look alike.

## Return interpretable, verifiable outputs

Return the figure title in `charts[].title` and its explanation in
`charts[].caption`. On-canvas helper title arguments contain only an empty string
or panel identifiers such as `(a)` and `(b)`. Describe each panel in the caption.
Axis names, units, legends, scientific annotations and justified direction cues
remain on the figure.

The caption records data provenance, exclusions, valid sample sizes, aggregation,
uncertainty definitions and any normalization. Explain absolute versus relative
differences, and label synthetic test data explicitly. Statistical significance
requires the corresponding verified analysis, not a color, arrow or visual gap.

The application validates and exports generated code in its execution environment;
standalone callers use the shared configuration and export helpers. Preserve the
physical canvas and readable type in PNG, PDF and SVG. Repair a reported mapping or
execution defect within the allowed retry budget without changing the scientific
question silently. Automated visual-model review is deferred.
