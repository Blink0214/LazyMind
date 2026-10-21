"""Lossless execution plans and smaller model views of verified figure evidence."""
import copy
import ast
import json
import re

from .plan import Job


def runtime_plan(plan):
    """Translate spelling only; scientific directions/scopes and the locked plan stay unchanged."""
    result = copy.deepcopy(plan)
    for job in result['jobs']:
        job['alternative'] = job.get('alternative', 'two_sided').replace('_', '-')
    return result


def execution_plan(plan):
    """Runtime still gets the complete locked plan; generation sees only executable choices."""
    jobs = [Job.model_validate(job).model_dump(exclude_defaults=True) for job in plan['jobs']]
    defaults = {name: field.get_default(call_default_factory=True)
                for name, field in Job.model_fields.items() if not field.is_required()}
    for job in jobs:
        if 'alternative' in job:
            job['alternative'] = job['alternative'].replace('_', '-')
    defaults['alternative'] = defaults['alternative'].replace('_', '-')
    return dict(jobs=jobs, models=copy.deepcopy(plan.get('models', [])),
                joins=copy.deepcopy(plan.get('joins', []))), defaults


def figure_statistics(analysis):
    """Keep every estimate, interval, sample count, test and assumption; omit repeated prose."""
    result = copy.deepcopy(analysis)
    result.pop('summary', None)
    result.pop('diagnostics', None)
    result['findings'] = [{key: value for key, value in finding.items()
                           if key not in ('statement', 'columns', 'calculation')}
                          for finding in result.get('findings', [])]
    model_ids = {f.get('model_id') for f in result['findings']} - {None}
    result['models'] = [m for m in result.get('models', []) if m['id'] in model_ids]
    return result


def figure_spec(spec, table):
    result = copy.deepcopy(spec)
    contract = result.get('style_contract', {})
    labels = {label for group in table.get('metadata', {}).get('source_groups', [])
              for label in group.get('artist_labels', [])}
    if labels:
        for field in ('series_colors', 'bar_series_colors', 'series_edges',
                      'series_markers', 'series_line_styles', 'series_sizes'):
            if field in contract:
                contract[field] = {k: v for k, v in contract[field].items() if k in labels}
    return result


def drawing_reference(reference, spec):
    """The complete reviewed helper source stays visible; omit duplicated style prose/contracts."""
    contract = spec.get('style_contract', {})
    marker = 'APPROVED DRAWING HELPERS (no example data or file I/O):'
    if contract.get('version') == 'paper-v1' and spec.get('style') != 'custom' and marker in reference:
        return reference[reference.index(marker):]
    return reference


def native_interface(reference, spec):
    """Document exact call signatures; execution still loads the complete unchanged source."""
    blocks = re.findall(r'```python\s*\n([\s\S]*?)```', reference)
    if not blocks or spec.get('style') == 'custom':
        return drawing_reference(reference, spec)
    contract = spec.get('style_contract', {})
    public = {contract.get('native_template'), *contract.get('helpers', [])}
    interface = []
    for node in ast.parse(blocks[-1]).body:
        if isinstance(node, ast.FunctionDef) and node.name in public:
            interface.append(dict(name=node.name, arguments=ast.unparse(node.args),
                                  description=ast.get_docstring(node) or ''))
    return interface


def interpretation_evidence(analysis, plan):
    """One authoritative finding list with deduplicated design/scope, instead of prose + full jobs."""
    result = figure_statistics(analysis)
    result.pop('data_sha256', None)
    result.pop('input_sha256', None)
    result.pop('data_sources', None)
    diagnostics = copy.deepcopy(analysis.get('diagnostics', []))
    passed = [item for item in diagnostics if item['name'] == '独立数值与样本量复核' and item['outcome'] == 'passed']
    result['diagnostics'] = [item for item in diagnostics if item not in passed]
    if passed:
        result['numeric_verification'] = dict(outcome='passed', finding_ids=[
            item['id'].removeprefix('verified_') for item in passed],
            criteria=list(dict.fromkeys(item.get('threshold') for item in passed)),
            details=list(dict.fromkeys(item['details'] for item in passed)))
    jobs = {job['id']: job for job in plan['jobs']}
    scopes, definitions, designs = {}, {}, {}

    def intern(catalog, value, prefix):
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if key not in catalog:
            catalog[key] = (prefix + str(len(catalog) + 1), copy.deepcopy(value))
        return catalog[key][0]

    for finding in result['findings']:
        job = jobs[finding['id']]
        scope = {key: copy.deepcopy(job[key]) for key in ('table_id', 'filters', 'baseline_filters', 'pair_by')
                 if key in job}
        finding['scope_id'] = intern(scopes, scope, 'scope_')
        definition = {key: value for key, value in job.items()
                      if key not in ('id', 'target_id', 'unit', 'table_id', 'filters', 'baseline_filters', 'pair_by',
                                     'design_evidence')
                      and value not in (None, [], '')}
        finding['definition_id'] = intern(definitions, definition, 'definition_')
        if job.get('design_evidence'):
            finding['design_id'] = intern(designs, job['design_evidence'], 'design_')
    for name, catalog in [('scopes', scopes), ('definitions', definitions), ('designs', designs)]:
        result[name] = {identifier: value for identifier, value in catalog.values()}
    return result
