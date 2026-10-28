"""Last-resort point display using result labels authored by analysis code, never filter strings."""


def generate_simple_code(data, statistics, spec, reference, previous, error):
    return '''import matplotlib.pyplot as plt

def draw(data, statistics, spec):
    selected = [row for row in data['analysis_results'] if row['id'] in spec['finding_ids']]
    groups = {}
    for row in selected:
        groups.setdefault((row['metric'], row['unit'], row['kind']), []).append(row)
    fig, axes = plt.subplots(len(groups), 1, squeeze=False,
                             figsize=(7.2, max(2.8, sum(.4*len(rows)+1.2 for rows in groups.values()))))
    for axis, ((metric, unit, kind), rows) in zip(axes.ravel(), groups.items()):
        for index, row in enumerate(rows):
            axis.scatter([row['value']], [index], s=24, color='#546BF5', zorder=3)
            interval = row['interval']
            if interval is not None and interval['lower'] is not None and interval['upper'] is not None:
                axis.plot([interval['lower'], interval['upper']], [index, index], color='#354CC8', linewidth=1)
        labels = [data['shared_style'].get('terminology', {}).get(row['label'], row['label']) for row in rows]
        axis.set_yticks(list(range(len(rows))), labels)
        axis.invert_yaxis()
        name = data['shared_style'].get('terminology', {}).get(metric, metric)
        symbol = rows[0].get('display_unit', unit)
        axis.set_xlabel(name + (' (' + symbol + ')' if symbol else ''))
        axis.spines['top'].set_visible(False)
        axis.spines['right'].set_visible(False)
        axis.grid(axis='x', alpha=.2)
        if kind == 'effect':
            axis.axvline(0, color='#707070', linestyle='--', linewidth=.8, zorder=1)
    fig.tight_layout(pad=1.2)
    return fig
'''
