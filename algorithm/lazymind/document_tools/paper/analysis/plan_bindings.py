"""Bind source-owned units, evidence and unambiguous figure coordinates without inference."""
import hashlib
import json
import re

from ..plotting.labels import result_unit, CJK, english_display, display_name


def bind_figure_language(figure, jobs):
    """Request local display repairs instead of changing data to obtain English labels."""
    mapping = figure.get('data_mapping') or {}
    aliases = mapping.get('display_labels', {})
    missing = [field for field in ('title', 'caption') if CJK.search(figure[field])]
    labels = [mapping.get(key) for key in ('value_label', 'category_label')]
    labels += list(mapping.get('value_labels', {}).values()) + list(aliases.values())
    missing.extend(label for label in labels if label and CJK.search(label))
    if mapping:
        source_labels = []
        for identifier in figure['finding_ids']:
            job = jobs[identifier]
            if not mapping.get('value_label') and job['column'] not in mapping.get('value_labels', {}):
                source_labels.append(display_name(job['column']))
            if job.get('unit'):
                source_labels.append(job['unit'])
            for field in ('filters', 'baseline_filters'):
                for rule in job[field]:
                    source_labels.append(str(rule['value']))
                    if mapping.get('category_column') == 'finding_id':
                        source_labels.append(display_name(rule['column']))
        if figure['style'] in ('bar_grouped_hatch', 'line_confidence_band', 'line_training_curve'):
            source_labels.append(mapping.get('category_label') or
                                 display_name(mapping['category_column'].split('.', 1)[-1]))
        missing.extend(text for text in source_labels if CJK.search(english_display(text, aliases)))
    return ([dict(figure_id=figure['id'], field='display_language',
                  message='All chart titles/captions and visible labels must be English. Repair title/caption '
                          'and data_mapping labels only; supply display_labels for these source texts: '
                          + ', '.join(dict.fromkeys(missing)))] if missing else [])


def evidence_catalog(inputs):
    entries = []
    excluded = {'results', 'rows', 'code', 'columns', 'units', 'column_types', 'table_id', 'column',
                'unit_keys', 'pairing_keys', 'grouping', 'repeated_measures'}

    def visit(value, path):
        if isinstance(value, dict):
            for key in sorted(value):
                if key not in excluded:
                    visit(value[key], path + '.' + key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f'{path}[{index}]')
        elif isinstance(value, str):
            for index, sentence in enumerate(re.split(r'(?<=[。！？!?])|\n+|(?<=[.;])\s+(?=[A-Z])', value)):
                if sentence.strip():
                    text = sentence.strip()
                    identity = hashlib.sha256(f'{path}:{index}:{text}'.encode()).hexdigest()[:12]
                    entries.append(dict(id='e_' + identity, source=path, text=text))

    for key in ('request', 'data_context', 'study_design', 'experiment'):
        visit(inputs.get(key), key)
    return entries


def bind_job_columns(job, tables):
    """Strip only the current table's redundant qualifier; exact source names win."""
    table = tables.get(job['table_id'])
    if table is None:
        return
    columns, prefix = table['columns'], job['table_id'] + '.'

    def local(name):
        if name not in columns and name.startswith(prefix) and name[len(prefix):] in columns:
            return name[len(prefix):]
        return name

    for field in ('column', 'other_column', 'trials_column', 'coefficient'):
        if job.get(field):
            job[field] = local(job[field])
    for field in ('pair_by', 'predictors'):
        job[field] = [local(name) for name in job.get(field, [])]
    for field in ('filters', 'baseline_filters'):
        for rule in job.get(field, []):
            rule['column'] = local(rule['column'])


def expected_unit(job, tables):
    operation = job['operation']
    if operation in ('pearson', 'spearman', 'beta_mean', 'beta_probability'):
        return None
    if operation == 'count':
        return 'count'
    table = tables[job['table_id']]
    response = table['units'][table['columns'].index(job['column'])]
    response = result_unit(operation, response)
    if operation != 'ols_coefficient' or job['coefficient'] == 'intercept' or response is None:
        return response
    column = job['coefficient']
    if column not in table['columns']:
        return None  # The job validator reports an invalid coefficient independently.
    predictor = table['units'][table['columns'].index(column)]
    return f'{response}/{predictor or column}'


def bind_job_fields(job, tables, catalog):
    issues = []
    # Source text is derived on every validation, never trusted from a model or cached plan.
    job['design_evidence'] = ''
    refs = job.get('design_evidence_refs', [])
    if refs:
        invalid = [ref for ref in refs if ref not in catalog]
        if invalid or len(set(refs)) != len(refs):
            issues.append(dict(job_id=job['id'], field='design_evidence_refs',
                               message='选择 evidence_catalog 中实际存在且不重复的原文编号；编号不证明统计假设成立'))
        else:
            job['design_evidence'] = '\n'.join(catalog[ref]['text'] for ref in refs)
    try:
        unit = expected_unit(job, tables)
    except (KeyError, ValueError):
        return issues  # Invalid source columns are reported by the job validator.
    # This is derived metadata, not a model choice. No observation or numeric value
    # is converted: source column units and the selected operation remain authoritative.
    job['unit'] = unit
    return issues


def bind_figure_mapping(figure, jobs, sources):
    """Qualify a unique alias; ambiguous selected/baseline axes require a local model repair."""
    mapping = figure.get('data_mapping')
    requires_mapping = figure['style'] in {
        'point_interval', 'bar_grouped_hatch', 'line_confidence_band', 'line_training_curve', 'scatter_tradeoff'
    }
    if not mapping and not requires_mapping:
        return []
    from ..plotting.labels import comparison_coordinate
    rows, dimensions = [], {'finding_id'}
    for identifier in figure['finding_ids']:
        job = jobs[identifier]
        row = {'finding_id': identifier}
        for name, prefix in (('filters', ''), ('baseline_filters', 'baseline.')):
            for rule in job[name]:
                if rule['operator'] == 'eq':
                    column = (job['table_id'] + '.' + rule['column'] if job['table_id'] in sources
                              else rule['column'])
                    row[prefix + column] = rule['value']
        if job['operation'] in ('difference', 'paired_t', 'paired_difference', 'welch_t'):
            contrast = comparison_coordinate(row, job)
            if contrast:
                row['comparison'] = contrast[0]
        dimensions.update(row)
        rows.append((job, row))
    if not mapping:
        return [dict(figure_id=figure['id'], field='data_mapping',
                     message='该图表必须明确 data_mapping 的系列、类别和分面；只补全映射，不修改统计任务或图型',
                     available_dimensions=sorted(dimensions))]
    errors = []
    metrics = {job['column'] for job, _ in rows}

    def metric_name(name):
        candidates = {job['column'] for job, _ in rows
                      if name in (job['column'], job['table_id'] + '.' + job['column'])}
        return next(iter(candidates)) if len(candidates) == 1 else name

    # Labels are presentation hints, not a model-authored inventory of the data.
    # Derive the inventory from bound jobs and accept only unambiguous hints.
    hints = {}
    for name, label in mapping.get('value_labels', {}).items():
        metric = metric_name(name)
        if metric not in metrics:
            matched = {job['column'] for job, _ in rows if job['id'] == name}
            metric = next(iter(matched)) if len(matched) == 1 else None
        if metric in metrics and isinstance(label, str) and label.strip():
            hints.setdefault(metric, set()).add(label.strip())
    mapping['value_labels'] = {
        metric: next(iter(hints[metric])) if len(hints.get(metric, ())) == 1 else display_name(metric)
        for metric in sorted(metrics)
    }

    def resolve(value):
        if value is None or value in dimensions:
            return value
        candidates = [key for key in dimensions if key.rsplit('.', 1)[-1] == value]
        if len(candidates) == 1:
            return candidates[0]
        selected = [key for key in candidates if not key.startswith('baseline.')]
        if len(selected) == 1 and set(candidates) == {selected[0], 'baseline.' + selected[0]} and all(
            selected[0] in row and 'baseline.' + selected[0] in row
            and row[selected[0]] == row['baseline.' + selected[0]] for _, row in rows
        ):
            return selected[0]  # The same source coordinate in both sides of every explicit match.
        errors.append(f'{value!r} 无法唯一定位，候选={sorted(candidates)}')
        return value

    mapping['series_column'] = resolve(mapping['series_column'])
    scatter = figure['style'] == 'scatter_tradeoff'
    mapping['category_column'] = (metric_name(mapping['category_column']) if scatter else
                                  resolve(mapping['category_column']))
    mapping['facets'] = [resolve(value) for value in mapping['facets']]
    keys = [*mapping['facets']] if scatter else [mapping['category_column'], *mapping['facets']]
    if scatter and (len(metrics) != 2 or mapping['category_column'] not in metrics):
        errors.append('权衡图需要两个实际指标；category_column 指定横轴指标，另一个为纵轴；用 value_labels 标注两者')
    contrasts = [row.get('comparison') for _, row in rows if row.get('comparison')]
    if (mapping['category_column'] == 'finding_id' and mapping['series_column'] is None
            and not mapping['facets'] and len(contrasts) != len(set(contrasts))):
        errors.append('同一比较跨条件重复，不能展开为冗长的 finding_id 行标签；用 comparison 系列与实际条件类别，或按条件分面')
    if mapping['series_column']:
        keys.append(mapping['series_column'])
    if len(set(keys)) != len(keys):
        errors.append('系列、类别和分面维度必须互不重复')
    if not errors and requires_mapping:
        occupied = set()
        for job, row in rows:
            if any(key not in row for key in keys):
                errors.append('每个结果必须有全部映射坐标；范围筛选阈值不是观测坐标')
                break
            cell = (job['column'], job['operation'], job['unit'], job['interval_level'],
                    *(json.dumps(row[key], ensure_ascii=False, sort_keys=True) for key in keys))
            if cell in occupied:
                errors.append('多个结果占据同一映射单元格；差值图应区分基线维度或增加分面，不能平均或丢弃结果')
                break
            occupied.add(cell)
    return ([dict(figure_id=figure['id'], field='data_mapping', message='；'.join(errors),
                  available_dimensions=sorted(dimensions))] if errors else [])
