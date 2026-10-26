"""Host-owned statistical job identities and bounded figure-reference repair."""
import copy
import hashlib
import json


class JobReferenceError(ValueError):
    def __init__(self, figures):
        self.figures = figures
        super().__init__('图表任务引用无效或存在歧义：' + str(figures))


def generation_schema():
    from .plan import Plan
    schema = Plan.model_json_schema()
    job = schema['$defs']['Job']
    job['properties'].pop('id')
    job['required'].remove('id')
    figure = schema['$defs']['FigurePlan']
    figure['properties'].pop('finding_ids')
    figure['required'].remove('finding_ids')
    figure['properties']['finding_indices'] = {'type': 'array', 'minItems': 1,
                                               'items': {'type': 'integer', 'minimum': 0}}
    figure['required'].append('finding_indices')
    return schema


def bind_jobs(value):
    """Assign IDs, collapse identical jobs, and resolve references without guessing."""
    from .plan import Job
    if not isinstance(value, dict) or 'jobs' not in value:
        return value
    plan = copy.deepcopy(value)
    jobs, indices, aliases = {}, [], {}
    for raw in plan['jobs']:
        # Restoring defaults makes omitted/default fields the same calculation.
        # These two fields are rebound from source data during plan validation.
        # They must not rename a job when a later figure-only repair rebinds it.
        body = Job.model_validate({**raw, 'id': 'unbound', 'unit': None,
                                   'design_evidence': ''}).model_dump(exclude={'id'})
        # Evidence ordering does not change a calculation, unlike filter values.
        for field in ('filters', 'baseline_filters', 'design_evidence_refs'):
            body[field] = sorted(body[field], key=lambda item: json.dumps(item, sort_keys=True))
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False)
        identifier = 'job_' + hashlib.sha256(encoded.encode()).hexdigest()[:24]
        bound = {'id': identifier, **body}
        if identifier in jobs and jobs[identifier] != bound:
            raise ValueError('统计任务内容哈希冲突')
        jobs[identifier] = bound
        indices.append(identifier)
        if isinstance(raw.get('id'), str):
            aliases.setdefault(raw['id'], set()).add(identifier)
    invalid = []
    for index, figure in enumerate(plan.get('figures', [])):
        positions = figure.pop('finding_indices', None)
        if positions is not None:
            valid = (isinstance(positions, list) and bool(positions)
                     and all(type(i) is int and 0 <= i < len(indices) for i in positions))
            refs = [indices[i] for i in positions] if valid else []
        else:
            names = figure.get('finding_ids', [])
            valid = (isinstance(names, list) and bool(names)
                     and all(isinstance(name, str) and len(aliases.get(name, ())) == 1 for name in names))
            refs = [next(iter(aliases[name])) for name in names] if valid else []
        if not valid:
            invalid.append(index)
        else:
            figure['finding_ids'] = list(dict.fromkeys(refs))
    if invalid:
        raise JobReferenceError(invalid)
    plan['jobs'] = list(jobs.values())
    return plan


def bind_with_repair(value, repair):
    """A reference typo repairs only figure selectors, never regenerates the analysis."""
    try:
        return bind_jobs(value)
    except JobReferenceError as error:
        original_error = error
    payload = {'jobs': value['jobs'], 'figures': [dict(index=i, figure=value['figures'][i])
                                               for i in original_error.figures]}
    response = repair(payload)
    updates = response.get('updates') if isinstance(response, dict) else None
    if not isinstance(updates, list) or len(updates) != len(original_error.figures):
        raise original_error
    result, seen = copy.deepcopy(value), set()
    for update in updates:
        if (not isinstance(update, dict) or set(update) != {'figure_index', 'finding_indices'}
                or type(update['figure_index']) is not int
                or update['figure_index'] not in original_error.figures or update['figure_index'] in seen):
            raise original_error
        index = update['figure_index']
        seen.add(index)
        result['figures'][index]['finding_indices'] = update['finding_indices']
    return bind_jobs(result)
