# Approved common paper preset

For prepared figures with original-observation overlays, the host stacks the axis
labels, legends and short statistical note using measured 1.8 mm gaps and trims
outer whitespace to 1.5 mm. Preserve fonts and data-area size; do not add a second
footer, full embedded title or arbitrary blank strip. Sparse source observations
remain sparse; summary-only inputs must not become invented distributions.
Keep statistical footer notes subordinate at 6.2 pt; axes and legends retain their
own preset sizes. Use explicit per-glyph font fallback for Unicode names and
mathematical symbols rather than exporting empty boxes.
F4 publication output uses English display labels, titles and captions, independently
of report language. Preserve data and use explicit display aliases for source names.

Use `paper_profile.json` and the actual drawing functions as the parameter source.
This local adaptation was reviewed with synthetic previews and covers nineteen data
styles, including paired bars and seven newly approved chart families. It does not imply
acceptance by any journal.

- Color order: `#D3D3D3`, `#A9A9A9`, `#546BF5`; additional series use the listed blue shades.
  Bind identity from input group order before sorting or filtering any panel. Keep the
  same identity across figures of the same experiment. Color never means significance.
- Use marker shape / line style as a second cue for curves and scatter, with thin darker
  outlines to keep pale gray marks visible. Only an explicitly designated focal bar
  method gets sparse white `//` hatching and bold value labels. Do not infer it from ranks.
- Computer Modern serif: Matplotlib's `cmr10` and `cmb10`, no external TeX process.
  Title 9 pt; axes 8.5 pt; ticks 8 pt; legends 7.3 pt; bar values 7.2 pt.
- Single figure 89 × 72 mm; paired panels 182 × 74 mm. Use the supplied margins, preserve
  the full canvas at export, and allocate more space for crowded labels or extra panels.
  These are local design choices; final journal font/size rules may differ.
- Specialized profiles supply their own sizes: paired bars/confidence/zoom 182 × 78 mm,
  embedding 89 × 82 mm, broken scatter 182 × 76 mm and paired radar 182 × 92 mm.
  Zoom and broken scatter helpers create their axes from an empty Figure; radar
  requires polar axes. Preserve the exact zoom frame and common broken-axis scale.
  Paired bars reserve a shared legend above two panels and omit arrows shorter than
  3 pt at output size. They display absolute differences with explicit units.
- Open left/bottom axes, 0.65 pt. Subtle horizontal dashed grid except for trajectories.
  Native grouped bars and point intervals use exterior legends: share one when labels,
  comparisons and appearances agree, otherwise keep panel scopes. F4 fits margins to
  final font metrics without shrinking fonts. No repeated category legend or duplicate n.
- Figure titles are returned in `Chart.title`, with panel descriptions in `caption`, outside the canvas.
  Helper `title`/`titles` arguments use an empty string or a short verified scope label such as `(a) <cohort>`. These panel labels identify the data; they are distinct from the full external figure title.
  Captions carry synthetic-data disclosure, units,
  missing/excluded observations, exact valid sample sizes and statistical definitions.
- Retain every valid observation and its coordinate pairing. Null is not zero. Do not
  smooth, simulate uncertainty, manufacture repeated runs or copy source-paper values.
- Save vector PDF with `pdf.fonttype=42`; SVG text is editable in standalone output
  (`svg.fonttype='none'`), while an application may choose glyph paths for portable viewing.
  PNG is 300 DPI. Fonts are embedded in PDF; SVG text viewers need matching fonts.

Use the preset's single/double-column sizes and vector exports. Check the target
venue's required dimensions and resolution before publication.

Numeric fields use their own colormaps: viridis/cividis for result matrices, zero-centered PuOr for signed differences/correlations, and Blues only for confusion counts/proportions. The rejected Blues benchmark-matrix variant is excluded. Native additive stacks use component colors from their own input identity map; they must not be recolored as methods. Dedicated colorbar axes preserve 7–7.5 pt labels and all meshes remain vector.

Publication checks apply at final physical size: preserve readable type and thin but visible marks; avoid overlapping ticks, redundant category legends, repeated scope prefixes and unnecessary empty space. Use explicit comparison directions, common scales for comparable facets, and separate units for different quantities. Vector exports and successful execution are necessary output checks, not certification of journal acceptance. Target-venue font, size and accessibility requirements take precedence over this general preset. See the official [Nature artwork instructions](https://www.nature.com/nature/for-authors/final-submission) for one example of venue-specific requirements.

The approved double-column width is a physical export limit, including figures with three or more panels. Do not multiply canvas width by panel count and then shrink the whole figure to fit the paper. Reflow at fixed type size or separate figures when necessary.
