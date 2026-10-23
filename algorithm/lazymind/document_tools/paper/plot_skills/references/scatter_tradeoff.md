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

A fixed configuration may serve as a descriptive x coordinate when that relationship
answers the research question. In the verified-summary interface expose it with a
descriptive mean and no interval; repeated copies do not supply independent replication
or uncertainty in the configuration. Pair it with performance from the same group/scope.
Label which configuration belongs to which method, retain its unit, and avoid causal
claims about the configuration's independent effect when it is confounded with method.

In the prepared F4 interface, data_mapping names the x mean metric in category_column,
its group identity in series_column and scope facets. value_labels supplies both
actual metric names. The host pairs verified x/y means by identical complete scopes.
When original row identities also match, it adds faint measured coordinate pairs and
any separately verified marginal intervals. Fixed configurations have no invented
intervals. Do not redraw these layers or imply a fitted relationship or joint
confidence region. Short scope labels remain inside; full titles remain external.
