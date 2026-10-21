# scatter_tradeoff

Call `scripts/scatter_tradeoff.py:draw_tradeoff` with `series[label] = [[x,y], ...]`,
stable labels/colors/edges/markers/sizes, title, units, data-dependent limits and legend
location. Use the [common paper preset](paper_style.md).

Keep each measured coordinate pair together. Drop a row only when its x or y is missing,
and disclose exclusions. Three-series defaults use circle/square/triangle and gray/gray/blue;
identity is bound before sorting or selecting a facet. Mark size is fixed unless an actual
third variable and scale are explicitly planned.

Do not connect configurations as a time series, fit/smooth a trend without a stated method,
create quartile bars implying matched budgets, compute t-SNE, or add a broken axis just
because a source example had one. Use specialized styles only when the actual question
and measurements require them.
