# Approved common paper preset

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
  Every helper's `title`/`titles` arguments accept only an empty string or subplot identifiers such as `(a)`/`(b)`.
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
