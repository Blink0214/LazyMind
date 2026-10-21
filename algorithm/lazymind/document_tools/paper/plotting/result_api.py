"""Trusted, data-only adapters injected beside the reviewed native drawing helpers.

No inference or figure choice happens here. A model selects the metric, series,
category and facet; this adapter binds those coordinates without parsing IDs.
Keep this module dependency-free so its source also runs in the plot container.
"""


def result_grid(data, metric, series_column, category_column, where) -> dict:
    """Return native-ready arrays for one exact result cell per series/category.

    where is a dict of exact coordinate values ({} for an already scoped figure).
    Names are qualified columns from metadata.result_dimensions, including
    baseline.<table>.<column>. Missing cells stay None; ambiguous cells fail.
    """
    metadata = data['metadata']
    rows = [row for row in metadata['result_records'] if row['metric'] == metric
            and all((row['coordinates'][key] if key in row['coordinates'] else row.get(key)) == value
                    for key, value in where.items())]
    if not rows:
        raise ValueError('No verified results match the requested metric/facet')
    if len({(row['operation'], row['unit']) for row in rows}) != 1:
        raise ValueError('Select one operation and unit per grid; do not mix different quantities')
    dimensions = metadata['result_dimensions']
    series = [item for item in dimensions[series_column]
              if any(row['coordinates'].get(series_column) == item['value'] for row in rows)]
    categories = [item for item in dimensions[category_column]
                  if any(row['coordinates'].get(category_column) == item['value'] for row in rows)]
    if categories and all(isinstance(item['value'], (int, float)) and not isinstance(item['value'], bool)
                          for item in categories):
        categories = sorted(categories, key=lambda item: item['value'])
    if not series or not categories:
        raise ValueError('Requested result dimensions have no observed coordinates')
    cells = {}
    for row in rows:
        if series_column not in row['coordinates'] or category_column not in row['coordinates']:
            raise ValueError('Every selected result needs both requested coordinates')
        key = (row['coordinates'][series_column], row['coordinates'][category_column])
        if key in cells:
            raise ValueError('Multiple verified results occupy one cell; select the operation or a finer facet')
        cells[key] = row
    methods = [item['artist_label'] for item in series]
    summaries, values, curves, comparisons = {}, {}, {}, {}
    for item in series:
        selected = [cells.get((item['value'], category['value'])) for category in categories]
        triples = [[row['value'], (row['interval'] or {}).get('lower'), (row['interval'] or {}).get('upper')]
                   if row else [None, None, None] for row in selected]
        name = item['artist_label']
        summaries[name] = triples
        values[name] = [triple[0] for triple in triples]
        curves[name] = [[category['value'] for category in categories], values[name],
                        [triple[1] for triple in triples], [triple[2] for triple in triples]]
        pairs = [row['comparisons'].get(series_column) for row in selected if row]
        if pairs and pairs[0] and all(pair == pairs[0] for pair in pairs):
            comparisons[name] = pairs[0]
    finite = [number for triples in summaries.values() for triple in triples
              for number in triple if number is not None]
    if not finite:
        raise ValueError('No finite verified values to draw')
    low, high = min(finite), max(finite)
    pad = max((high - low) * .10, max(abs(low), abs(high)) * .02, .01)
    difference_low, difference_high = min(0, low), max(0, high)
    difference_pad = max((difference_high - difference_low) * .10, .01)
    x = [item['value'] for item in categories]
    numeric_x = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in x)
    xpad = max((max(x) - min(x)) * .04, .01) if numeric_x else None
    return dict(methods=methods, display_labels=[item['display_label'] for item in series],
                categories=[item['display_label'] for item in categories], x=x,
                summaries=summaries, values=values, curves=curves, comparisons=comparisons,
                colors=[item['color'] for item in series], bar_colors=[item['bar_color'] for item in series],
                edges=[item['edge'] for item in series], markers=[item['marker'] for item in series],
                line_styles=[item['line_style'] for item in series],
                value_limits=[low - pad, high + pad],
                difference_limits=[difference_low - difference_pad, difference_high + difference_pad],
                bar_limits=[min(0, low - pad), max(0, high) + max(abs(high), high - low, .1) * .20],
                x_limits=[min(x) - xpad, max(x) + xpad] if numeric_x else None,
                unit=rows[0]['unit'], sample_sizes=[row['sample_size'] for row in rows])
