"""Human-readable labels from verified scope/quantity metadata, never result-ID spelling."""
import re
import json


DIFFERENCES = {'difference', 'paired_difference', 'paired_t', 'welch_t'}
CJK = re.compile(r'[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]')


def english_display(value, aliases):
    """Translate display text only; source identities and numeric cells are never renamed."""
    text = str(value)
    for original, label in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
        if CJK.search(original):
            text = text.replace(original, label)
    return text


def comparison_coordinate(coordinates, scope):
    """A contrast identity is the ordered pair of actual conditions, never a finding ID."""
    if any(rule['operator'] != 'eq' for field in ('filters', 'baseline_filters') for rule in scope[field]):
        return None  # Range contrasts need their full explicit scope, not an equality-only legend.
    pairs = [(key, value, coordinates['baseline.' + key]) for key, value in coordinates.items()
             if not key.startswith('baseline.') and 'baseline.' + key in coordinates
             and value != coordinates['baseline.' + key]]
    if not pairs:
        return None
    pairs.sort(key=lambda pair: pair[0])
    identity = json.dumps(pairs, ensure_ascii=False, separators=(',', ':'))
    sides = [' / '.join((f'{display_name(pair[0].split(".", 1)[-1])} = ' if len(pairs) > 1 else '')
                        + str(pair[side]) for pair in pairs) for side in (1, 2)]
    return identity, sides


def result_unit(operation, unit):
    if operation in DIFFERENCES and str(unit).lower() in ('%', 'percent', 'percentage'):
        return 'pp'  # Subtraction changes the unit description, never the verified numeric scale.
    return unit


def display_name(value):
    return str(value).replace('_', ' ').strip()


def axis_label(metric, operation, unit, label, title):
    name = label if label and label.strip() != title.strip() else display_name(metric)
    if operation in DIFFERENCES:
        name = re.sub(r'^Selected\s*-\s*baseline:\s*', '', name, flags=re.I)
        name = re.sub(r'^Paired\s+', '', name, flags=re.I)
        name = re.sub(r'^(?:Difference|Change)\s+in\s+', '', name, flags=re.I)
        name = re.sub(r'\s+(?:difference|change)$', '', name, flags=re.I).strip()
        if name.lower() in ('difference', 'change', ''):
            name = display_name(metric)
        name = name[:1].upper() + name[1:] + ' difference'
    unit = result_unit(operation, unit)
    symbol = {'minute': 'min', 'minutes': 'min', 'second': 's', 'seconds': 's',
              'millisecond': 'ms', 'milliseconds': 'ms'}.get(unit, unit)
    return name + (f' ({symbol})' if symbol and symbol != '1' else '')


def result_label(record, excluded=()):
    """Keep observed context and full subtraction order; opaque IDs stay in provenance only."""
    coordinates = record['coordinates']
    comparisons = {key: pair for key, pair in record['comparisons'].items()
                   if not key.startswith('baseline.')}
    context = []
    for key, value in coordinates.items():
        if key in excluded or key == 'finding_id' or key in comparisons:
            continue
        if key.startswith('baseline.'):
            source = key.removeprefix('baseline.')
            if source in comparisons or coordinates.get(source) == value:
                continue
        context.append(f'{display_name(key.split(".", 1)[-1])}: {value}')
    symbols = {'eq': '=', 'ne': '!=', 'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<='}

    def condition(rule):
        return f'{display_name(rule["column"])} {symbols.get(rule["operator"], rule["operator"])} {rule["value"]}'

    context.extend(condition(rule) for rule in record.get('scope', {}).get('filters', [])
                   if rule['operator'] != 'eq')
    pairs = list(dict.fromkeys(tuple(pair) for pair in comparisons.values()))
    if pairs:
        comparison = ' / '.join(f'{left} - {right}' for left, right in pairs)
    elif record['operation'] in DIFFERENCES:
        scope = record['scope']

        def rules(name):
            return '; '.join(condition(rule) for rule in scope[name])

        if not scope.get('filters') or not scope.get('baseline_filters'):
            raise ValueError('A displayed difference needs its actual selected and baseline conditions')
        comparison = rules('filters') + ' - ' + rules('baseline_filters')
    else:
        comparison = ''
    definition = record.get('definition', {})
    detail = [f'{display_name(key)}: {value}' for key, value in definition.items()]
    parts = [*context, *detail, comparison]
    return '\n'.join(part for part in parts if part) or display_name(record['metric'])
