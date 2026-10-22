"""Paper writing and v2-to-Writer preparation, without workflow or pool access.

The entry expects normalized v2 input from the shared contract owner. Draft and
review-driven revision use the shared LazyMind Writer toolkits.
"""
import copy
from collections import Counter
import json
import re
import logging
import time
from threading import Lock

from lazyllm import tracing

from lazyllm.tools.writer.data_models.planning import SectionInstructionList
from lazyllm.tools.writer.data_models.quality import ReviewReport
from lazyllm.tools.writer.data_models.revision import LocateResult, MarkdownModifyPlan, StringReplaceSet
from lazyllm.tools.writer.utils import locate_markdown_paragraph
from lazyllm.tools.writer.utils.serialization import (
    get_markdown_outline_targets, markdown_section_range, parse_markdown_sections,
    parse_markdown_outline_instructions, strip_heading_numbering,
)

from . import contracts
from .publication import (
    normalize_source_citations, protect_reference_region, split_references,
    validate_citations, validate_unmanaged_additions,
    markdown_prose as _markdown_prose, validate_chart_references as _validate_chart_references,
)
from .runtime_events import current_events, report_progress

from ..toolkits import WriterCreateToolkit, WriterRevisionToolkit


from .prompts.write_document_prompt import (
    WRITING_INSTRUCTIONS,
    MATERIAL_USAGE_INSTRUCTIONS,
    CHART_USAGE_INSTRUCTIONS,
    CHART_OUTLINE_INSTRUCTIONS,
    REFERENCE_USAGE_INSTRUCTIONS,
    SECTION_CONTENT_INSTRUCTIONS,
    WRITING_TASK_HEADER,
    REVISE_MODE_INSTRUCTIONS,
    DRAFT_MODE_INSTRUCTIONS,
    SECTION_NO_TARGET_INSTRUCTIONS,
    SECTION_BUDGET_INSTRUCTIONS,
    REVIEW_REVISION_INSTRUCTIONS,
    DRAFT_MODIFICATION_INSTRUCTIONS,
    CHART_REFERENCE_GUIDANCE,
)


# ======= 写作阶段埋点与章节回调 =======

LOG = logging.getLogger(__name__)


def _safe(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        LOG.warning('F5 trace recording failed', exc_info=True)


def _record(record):
    LOG.info('[PaperWritingTiming] %s', json.dumps(record, ensure_ascii=False))


def _trace_call(phase, function, *args, **kwargs):
    """Measure business stages, leaving nested Writer LLM spans intact."""
    def stage():
        pass
    stage.__name__ = 'paper.write_document.' + phase
    context = _safe(tracing.get_trace_context)
    record = {'phase': phase, 'trace_id': getattr(context, 'trace_id', None)}
    # These are adapter inputs, not claimed to be final model prompt/token sizes.
    for key, value in kwargs.items():
        if key.endswith('_json') and isinstance(value, str):
            record[key + '_chars'] = len(value)
    if phase == 'prepare' and args:
        for key in ('papers', 'evidence', 'analysis', 'charts', 'question'):
            value = args[0].get(key)
            record[key + '_json_chars'] = len(json.dumps(value, ensure_ascii=False))
            if isinstance(value, list):
                record[key + '_count'] = len(value)
            if key == 'analysis' and isinstance(value, dict):
                record['finding_count'] = len(value.get('findings', []))
    span = _safe(tracing.start_span, span_kind='callable', target=stage, args=(), kwargs={})
    started = time.monotonic()
    _safe(_record, {**record, 'status': 'started'})
    try:
        result = function(*args, **kwargs)
        record['status'] = 'completed'
        if isinstance(result, str):
            record['output_chars'] = len(result)
            if phase == 'consistency':
                record['quality_passed'] = _safe(
                    lambda: json.loads(result)['review_report']['result']['is_passed'])
        return result
    except BaseException as error:
        record.update(status='error', error_type=type(error).__name__)
        _safe(tracing.set_span_error, span, error)
        raise
    finally:
        record['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        _safe(tracing.set_span_attributes, span, {
            'paper.writing.' + key: value for key, value in record.items() if value is not None
        })
        _safe(tracing.finish_span, span)
        _safe(_record, record)


def _section_callbacks(events):
    """Capture correlation before workers run; forward original callbacks unchanged."""
    context = _safe(tracing.get_trace_context)
    trace_id = getattr(context, 'trace_id', None)
    started, seen, lock = time.monotonic(), set(), Lock()
    output_chars = {}

    def progress(event):
        state = {key: event.get(key) for key in (
            'section_index', 'section_total', 'section_state', 'section_attempt')}
        signature = tuple(state.values())
        with lock:
            fresh = signature not in seen
            seen.add(signature)
            index = state['section_index']
            if 'section_output_chars' in event:
                output_chars[index] = event['section_output_chars']
            state['last_reported_output_chars'] = output_chars.get(index)
        if fresh:
            _safe(_record, dict(phase='section_progress', trace_id=trace_id, **state,
                               elapsed_ms=round((time.monotonic() - started) * 1000)))
        if events:
            events.section_progress(event)

    def restart(prefix):
        _safe(_record, dict(phase='preview_restart', trace_id=trace_id, prefix_chars=len(prefix)))
        if events:
            events.restart(prefix)

    callbacks = {'on_progress': progress}
    # Presence of this callback permits Writer retries after body output. Preserve that policy.
    if events:
        callbacks['on_preview_restart'] = restart
    return callbacks


# ======= JSON 序列化 =======

def _json(value):
    return json.dumps(value, ensure_ascii=False)


# ======= 图表与文献：候选目录、公共协议适配及标记检查 =======

def _chart_caption(title):
    return re.sub(r'([\\\[\]])', r'\\\1', title).replace('\n', ' ').replace('\r', ' ')


def _chart_directory(chart, findings):
    """Project contract-validated chart associations from a shared finding index."""
    if any(ref not in findings for ref in chart['finding_ids']):
        raise ValueError('chart.finding_ids 包含未知的分析结论')
    return {**{key: copy.deepcopy(chart[key]) for key in ('id', 'title', 'columns', 'caption', 'finding_ids')},
            'findings': [copy.deepcopy(findings[ref]) for ref in chart['finding_ids']]}


def _reference_candidates(papers):
    candidates = []
    for paper in papers:
        entry = paper.get('citation')
        if entry is None:
            continue
        candidates.append({'id': paper['id'], 'citation': entry})
    return candidates


def _validate_reference_markers(body, candidates, original=None):
    """Check reserved citation markers; preserve existing unsupported citations."""
    def markers(text):
        return Counter(re.findall(r'\[文献[^\]\n]*(?:\]|(?=\n|$))', _markdown_prose(text)))
    if normalize_source_citations(body, candidates) != body:
        raise ValueError('成稿来源引用必须使用 [文献:ID]，不能遗留综述的来源标记或列表')
    validate_unmanaged_additions(original, body)
    if original is not None:
        protect_reference_region(original, body)
    if '#paper-ref-' in body or 'paper-references-' in body:
        validate_citations(body, candidates)
    existing = markers(original or '')
    allowed = {item['id'] for item in candidates}
    for marker, count in markers(body).items():
        match = re.fullmatch(r'\[文献:([\w.-]+)\]', marker)
        if match and match[1] in allowed:
            continue
        if count > existing[marker]:
            raise ValueError('Invalid or unavailable reference marker: ' + marker)


_CHART_OWNERS = re.compile(r'<!-- paper-charts:\s*(\[[^\n]*?\])\s*-->')


def _outline_chart_owners(outline, charts):
    catalog = {chart['id'] for chart in charts}
    owners = {}
    for level, path, occurrence, body in parse_markdown_sections(_markdown_prose(outline)):
        for match in _CHART_OWNERS.finditer(body):
            ids = json.loads(match[1])
            if level == 1 or not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
                raise ValueError('Chart ownership requires a body heading and a list of chart IDs.')
            for key in ids:
                if key not in catalog:
                    raise ValueError('Outline assigns an unknown chart.')
                if key in owners:
                    raise ValueError('Outline assigns a chart more than once.')
                owners[key] = (path, occurrence)
    if owners.keys() != catalog:
        raise ValueError('Outline must assign every supplied chart exactly once.')
    return owners


def _bind_chart_references(plan, prepared: dict, outline):
    """Translate outline ownership into Writer's existing cross-reference protocol."""
    if not prepared['charts']:
        return
    instructions = plan['instructions']
    placements = _outline_chart_owners(outline, prepared['charts'])
    owners = {}
    for chart in prepared['charts']:
        path, occurrence = placements[chart['id']]
        target_start, target_end = markdown_section_range(outline, path, occurrence)
        matches = []
        for i, item in enumerate(instructions):
            section = item['content_ref']
            start, end = markdown_section_range(
                outline, section['heading_path'], section.get('occurrence', 1),
            )
            if start <= target_start and target_end <= end:
                matches.append(i)
        if len(matches) != 1:
            raise ValueError('Chart placement does not match a planned section.')
        owners[chart['id']] = (matches[0], chart, path)
    for index, item in enumerate(instructions):
        meta = item['meta']
        meta['cross_reference_targets'] = list(dict.fromkeys(
            [*meta.get('cross_reference_targets', []), *owners]))
        references = meta.setdefault('cross_references', [])
        for target, (owner, chart, heading_path) in owners.items():
            references.append({
                'target': target, 'kind': 'image', 'required': owner == index,
                'must_create': owner == index, 'caption': _chart_caption(chart['title']),
                'guidance': (CHART_REFERENCE_GUIDANCE.format(title=chart['title'])
                             + ' Figure caption (preserve supplied meaning): ' + chart['caption']
                             + ' Place the chart in section: ' + ' / '.join(heading_path)),
            })


def _materialize_chart_insertions(body, charts):
    """Expand bounded revision insertions without invoking media generation."""
    catalog = {chart['id']: chart for chart in charts}
    prose = _markdown_prose(body).split('\n')
    lines = body.split('\n')
    for index, line in enumerate(prose):
        if '[图表:' not in line:
            continue
        match = re.fullmatch(r'\s*\[图表:([^\]]+)\]\s*', line)
        if match is None:
            raise ValueError('Chart insertion must be a standalone chart ID.')
        if match[1] not in catalog:
            raise ValueError('Writer manuscript references an unknown chart.')
        chart = catalog[match[1]]
        target = chart['id']
        caption = _chart_caption(chart['title'])
        lines[index] = f'<a id="block-{target}"></a>\n![{caption}](media-placeholder://{target})'
    return '\n'.join(lines)


# ======= 上下文准备：任务、材料与 Writer context =======

def prepare_writer_inputs(inputs: dict, *, toolkit=None) -> dict:
    """Adapt normalized v2 materials using the shared, model-free Writer builders.

    This internal bundle is not the public run output or a new business contract.
    Resource roles are already known, so no model-based resource classification is
    needed. Full material text is kept as key facts for the context builder.
    """
    toolkit = toolkit if toolkit is not None else WriterCreateToolkit()
    source = copy.deepcopy(inputs)
    constraints = source['constraints']
    selected_issues = []
    review = None
    if source['mode'] == 'revise':
        review_data = source['review_data']
        issues = {item['id']: item for item in review_data['issues']}
        selected_issues = [{**issues[key], 'target': resolve_review_target(issues[key], source['manuscript'])}
                           for key in source['selected_issue_ids']]
        review = {**review_data, 'issues': selected_issues}
    # Build the material rules before context creation: replacement generation
    # receives context and plan, but does not receive the writing task directly.
    query = '\n\n'.join((
        WRITING_INSTRUCTIONS, MATERIAL_USAGE_INSTRUCTIONS,
        CHART_USAGE_INSTRUCTIONS, REFERENCE_USAGE_INSTRUCTIONS,
    )) + WRITING_TASK_HEADER + _json({
        'topic': source['topic'], 'request': source['request'],
        'constraints': constraints, 'mode': source['mode'],
        'selected_issue_ids': source['selected_issue_ids'],
    })
    if source['mode'] == 'revise':
        query += REVISE_MODE_INSTRUCTIONS
    else:
        query += DRAFT_MODE_INSTRUCTIONS
    task = json.loads(toolkit.build_writing_task(query=query))
    # Explicit structured constraints take precedence over incidental numbers in
    # user prose. English words must not be passed to Writer as Chinese chars.
    task['constraints'] = copy.deepcopy(constraints)
    target = constraints['word_target']
    if target is not None and constraints['paper_language'] == 'zh-CN':
        task['constraints'].update(target_chars=target, max_chars=int(target * 1.1))
    # Business mode stays upstream-owned; Writer distinguishes creation from editing.
    task['task_type'] = {
        'draft': 'revise' if source['manuscript'] is not None else 'write',
        'revise': 'revise',
    }[source['mode']]
    task['scope'] = 'document'
    task['output'] = {'representation': 'markdown'}
    task['meta'] = {'source': 'write_document', 'mode': source['mode']}
    resources = []

    def material(key, title, value):
        if value is None or value == []:
            return
        resources.append({
            'resource_id': 'write_document:' + key, 'resource_type': 'text',
            'title': title, 'inline_text': _json(value),
            'meta': {'role': 'background', 'material_kind': key.split(':', 1)[0]},
        })

    for paper in source['papers']:
        material('paper:' + paper['id'], paper['title'], paper)
    candidates = _reference_candidates(source['papers'])
    material('reference_candidates', 'Citation candidate catalog (only these sources may receive new citations)', candidates)
    material('question', 'Research question, hypotheses and validation plan', source['question'])
    material('evidence', 'Literature evidence and limitations', source['evidence'])
    material('analysis', 'Upstream analysis results (not recalculated in this step)', source['analysis'])
    findings = {item['id']: item for item in (source['analysis'] or {}).get('findings', [])}
    chart_directory = [_chart_directory(chart, findings) for chart in source['charts']]
    material('charts', 'Chart reference catalog', chart_directory)
    material('outline', 'Existing outline', source['outline'])
    material('manuscript', 'Current complete manuscript', source['manuscript'])
    material('review', 'Review issues selected for this turn', review)
    resources = json.loads(toolkit.build_resources(input_resources_json=_json(resources)))
    profiles = [{
        'resource_id': resource['resource_id'], 'resource_role': 'background',
        'raw_content': resource['inline_text'], 'key_facts': [resource['inline_text']],
        'meta': copy.deepcopy(resource['meta']),
    } for resource in resources]
    return {
        'writing_task': task, 'resources': resources, 'resource_profiles': profiles,
        'outline': source['outline'], 'manuscript': source['manuscript'],
        'charts': source['charts'], 'chart_directory': chart_directory, 'selected_issues': selected_issues,
        'reference_candidates': candidates,
    }


def prepare_writer_context(inputs: dict, *, toolkit=None) -> dict:
    """Prepare a Writer-consumable task/context, without invoking a model.

    The shared context builder owns its internal temporary artifacts. This helper
    does not save public artifacts, access a material pool or resolve input paths.
    """
    toolkit = toolkit if toolkit is not None else WriterCreateToolkit()
    prepared = prepare_writer_inputs(inputs, toolkit=toolkit)
    context = json.loads(toolkit.create_writing_context(
        writing_task_json=_json(prepared['writing_task']),
        resource_profiles_json=_json(prepared['resource_profiles']),
    ))
    locked = {resource['resource_id'] for resource in prepared['resources']
              if resource['meta']['material_kind'] in {'paper', 'evidence', 'analysis'}}
    for fact in context['facts']:
        if fact['key'] in locked:
            fact['locked'] = True
    prepared['writing_context'] = context
    return prepared


# ======= 标题与大纲处理：复用公共 Markdown 解析 =======

def _heading_signature(markdown):
    """Use Writer's fence-aware parser; add only the paper hierarchy constraint."""
    get_markdown_outline_targets(markdown)
    signature = [(level, path[-1]) for level, path, _, _ in parse_markdown_sections(markdown)]
    previous = 0
    for level, title in signature:
        if not title or level > previous + 1:
            raise ValueError('Paper headings must not skip levels or have empty titles.')
        previous = level
    return signature


def _outline_from_markdown(markdown):
    return '\n\n'.join('#' * level + ' ' + title for level, title in _heading_signature(markdown))


# ======= 章节规划：篇幅分配与论文约束 =======

def _outline_budget_descriptions(outline):
    """Remove Writer budget suffixes only from their matching outline descriptions."""
    replacements = {}
    for payload in parse_markdown_outline_instructions(outline).values():
        description = payload.get('outline_description')
        budget = payload.get('target_chars')
        if not isinstance(description, str) or type(budget) is not int or budget <= 0:
            continue
        suffix = rf'(?:（约{budget}字）| \(about {budget} characters\))(?=[。.!?！？]?$)'
        cleaned = re.sub(suffix, '', description).strip()
        if cleaned != description:
            replacements[description] = cleaned
    return replacements


def _prepare_sections(plan, outline, constraints):
    """Retain Writer planning and apply language-aware paper budgets."""
    plan = SectionInstructionList.model_validate(plan).model_dump(exclude_none=True)
    instructions = plan['instructions']
    _, targets = get_markdown_outline_targets(outline)
    expected = [(path, occurrence) for _, path, occurrence, _ in targets]
    actual = [(item['content_ref']['heading_path'], item['content_ref']['occurrence'])
              for item in instructions]
    if actual != expected:
        raise ValueError('Writer section plan does not match the supplied outline.')
    target = constraints['word_target']
    unit = 'words' if constraints['paper_language'] == 'en' else 'chinese_characters'
    # Weight by the existing planner's relative budgets, never reinterpret English
    # word targets as characters. The final audit uses the legacy academic counter.
    weights = [max(1, int(item['meta'].get('target_chars') or 1)) for item in instructions]
    descriptions = _outline_budget_descriptions(outline)
    assigned = 0
    for index, item in enumerate(instructions):
        # Do not regex-strip numbers from research facts or user instructions.
        # Only replace complete descriptions tied to Writer's outline metadata.
        item['section_goal'] = descriptions.get(item['section_goal'], item['section_goal'])
        for field in ('required_points', 'expected_blocks'):
            item[field] = [descriptions.get(value, value) for value in item[field]]
        meta = item['meta']
        meta.pop('target_chars', None)
        meta.pop('max_chars', None)
        meta.pop('word_target', None)
        meta.pop('count_unit', None)
        for heading in item.get('heading_structure') or []:
            heading.pop('target_chars', None)
        if target is not None:
            cumulative = target * sum(weights[:index + 1]) // sum(weights)
            budget = cumulative - assigned
            assigned = cumulative
            meta.update(word_target=budget, count_unit=unit)
            item['required_points'].append(
                SECTION_BUDGET_INSTRUCTIONS.format(budget=budget, unit=unit, target=target)
            )
            if unit == 'chinese_characters' and budget > 0:
                meta.update(target_chars=budget, max_chars=int(budget * 1.1))
        else:
            item['required_points'].append(SECTION_NO_TARGET_INSTRUCTIONS)
        item['fact_constraints'].append(SECTION_CONTENT_INSTRUCTIONS)
    return plan


# ======= 文稿校验：计数、结构、图表引用与公共质量检查 =======

# 保留旧论文流程的计数口径；只迁移纯逻辑，不依赖旧 Workflow。
HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def count_academic_units(text: str, count_unit: str) -> int:
    """Count Chinese characters or English words using the declared paper language."""
    value = str(text or '')
    if count_unit == 'words':
        return len(WORD.findall(value))
    return len(HAN.findall(value)) + len(re.findall(r'\b[A-Za-z][A-Za-z0-9_-]*\b', value))


def _validate_manuscript_structure(body, *, signature=None, charts=None,
                                   candidates=None, original=None):
    """Shared mechanical checks for generation and the API result boundary."""
    actual = _heading_signature(body)
    if signature is not None and actual != signature:
        raise ValueError('Writer manuscript headings do not match the outline.')
    if charts is not None:
        _validate_chart_references(body, charts)
    if candidates is not None:
        _validate_reference_markers(body, candidates, original)
    return actual


def _validate_manuscript(body, signature, charts, constraints, toolkit, context_json, *,
                         candidates, original=None, check_quality=True):
    """Validate generated Markdown before the optional model quality check."""
    report_progress(87, '正在校验稿件结构与引用')
    _trace_call('validate_structure', _validate_manuscript_structure, body,
                 signature=signature, charts=charts, candidates=candidates, original=original)
    target = constraints['word_target']
    if target is not None:
        unit = 'words' if constraints['paper_language'] == 'en' else 'chinese_characters'
        actual = count_academic_units(split_references(body)[0], unit)
        if not target * 0.9 <= actual <= target * 1.1:
            raise ValueError(f'Manuscript length {actual} {unit} is outside target {target} ±10%.')
    if not check_quality:
        return
    report_progress(90, '正在检查全文一致性')
    quality = json.loads(_trace_call('consistency', toolkit.check_consistency,
        draft_document_json=body, writing_context_json=context_json,
    ))
    report = ReviewReport.model_validate(quality['review_report'])
    if not report.result.is_passed:
        raise ValueError('Writer consistency check failed: ' + report.result.summary)
    report_progress(95, '全文一致性检查通过')


# ======= 修改范围：原稿坐标、替换边界及意见覆盖 =======

def _operation_ranges(source, plan):
    """Resolve source-coordinate ranges per operation, including move destinations."""
    operations = []
    for item in plan.instructions:
        ranges = []
        scopes = [(item.content_ref, item.target_scope, item.locator_text)]
        if item.modify_type == 'move':
            scopes.append((item.destination_ref, item.destination_scope, item.destination_locator_text))
        for ref, scope, locator in scopes:
            if ref is None or ref.node_id or ref.placeholder_id:
                raise ValueError('Paper edits require Markdown heading references.')
            start, end = ((0, len(source)) if ref.document_root else markdown_section_range(
                source, ref.heading_path, ref.occurrence,
            ))
            if scope == 'section':
                ranges.append((start, end))
                continue
            text = (locate_markdown_paragraph(
                source, locator or '', heading_path=ref.heading_path, occurrence=ref.occurrence,
            ) if scope == 'paragraph' else locator)
            if not text or source[start:end].count(text) != 1:
                raise ValueError('Paper edit locator must match exactly once in its scope.')
            offset = start + source[start:end].index(text)
            ranges.append((offset, offset + len(text)))
        operations.append(ranges)
    return operations


def _within_range(start, end, allowed):
    return any(left <= start and end <= right for left, right in allowed)


def _bounded_replacements(source, allowed, generated):
    """Reject ambiguous/out-of-plan edits before the shared atomic application.

    Bounds refer to the original manuscript, so sort independent replacements from
    bottom to top: changing a heading cannot invalidate a later child reference.
    """
    replacements = StringReplaceSet.model_validate(generated)
    if not replacements.replacements:
        raise ValueError('Writer returned no manuscript edits.')
    located = []
    for replacement in replacements.replacements:
        ref = replacement.content_ref
        if ref is None or ref.document_root:
            start, end = 0, len(source)
        else:
            if ref.node_id or ref.placeholder_id:
                raise ValueError('Paper replacements require Markdown heading references.')
            start, end = markdown_section_range(source, ref.heading_path, ref.occurrence)
        if source[start:end].count(replacement.old_string) != 1:
            raise ValueError('Replacement text must match exactly once in its original scope.')
        start += source[start:end].index(replacement.old_string)
        end = start + len(replacement.old_string)
        if not _within_range(start, end, allowed):
            raise ValueError('Writer replacement exceeds the planned modification scope.')
        located.append((start, end, replacement))
    located.sort(key=lambda item: item[0])
    if any(left[1] > right[0] for left, right in zip(located, located[1:])):
        raise ValueError('Writer returned overlapping manuscript edits.')
    replacements.replacements = [item[2] for item in reversed(located)]
    return replacements.model_dump(), located


# ======= 审稿修订规划：绑定选中意见及允许范围 =======

def _review_plan(source, task, prepared: dict, editor, context_json):
    """Use reviewed anchors directly; bind every operation to selected issues.

    This checks generated operations, not the upstream review/hash contract.
    Quote is evidence for the issue, while target defines its editable extent.
    """
    issues = prepared['selected_issues']
    if not issues:
        raise ValueError('Review revision requires selected issues; empty is not all.')
    task['query'] += REVIEW_REVISION_INSTRUCTIONS + _json(issues)
    # Markdown Writer candidates are headings, not document_root. Expand a
    # document-level issue into real headings while retaining its original scope.
    targets = []
    for issue in issues:
        refs = ([{'heading_path': path, 'occurrence': occurrence}
                 for _, path, occurrence, _ in parse_markdown_sections(source)]
                if issue['target']['document_root'] else [issue['target']])
        targets.extend({'content_ref': ref, 'reason': _json(issue)} for ref in refs)
    located = LocateResult(task_id=task.get('task_id'), target_title=False, targets=targets)
    try:
        plan = MarkdownModifyPlan.model_validate(json.loads(editor.generate_modify_plan(
            writing_task_json=_json(task), writer_document_json=source,
            locate_result_json=located.model_dump_json(), writing_context_json=context_json,
        )))
    except ValueError as exc:
        # Public Writer rejects an empty plan. Translate only this known outcome;
        # genuine planner errors must still propagate unchanged.
        if (not any(i['action_type'] == 'text_revision' for i in issues)
                and str(exc) == 'modify_plan contains no operations for the located revision scope.'):
            raise contracts.MissingMaterials('选中意见需要补充材料或实验，未产生可应用的文本修订。') from exc
        raise
    bounds = {}
    for issue in issues:
        ref = issue['target']
        bounds[issue['id']] = ((0, len(source)) if ref['document_root'] else markdown_section_range(
            source, ref['heading_path'], ref['occurrence'],
        ))
    covered = set()
    operation_ranges = _operation_ranges(source, plan)
    for item, ranges in zip(plan.instructions, operation_ranges):
        ids = item.meta.get('issue_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or key not in bounds for key in ids):
            raise ValueError('Review plan operations must reference selected issue_ids.')
        if any(not any(bounds[key][0] <= start and end <= bounds[key][1] for key in ids)
               for start, end in ranges):
            raise ValueError('Review plan exceeds selected issue target scope.')
        if any(not any(bounds[key][0] <= start and end <= bounds[key][1] for start, end in ranges)
               for key in ids):
            raise ValueError('Review plan associates an issue with an unrelated operation.')
        covered.update(ids)
    required = {i['id'] for i in issues if i['action_type'] == 'text_revision'}
    if not required <= covered:
        raise ValueError('Review plan does not cover all selected issues.')
    return plan, operation_ranges, bounds


# ======= 稿件修改：公共工具执行及最终成稿 =======

def _modify_manuscript(prepared: dict, constraints, toolkit, *, editor=None):
    """Apply request edits or review-bound edits with the same public toolkit."""
    source = prepared['manuscript']
    # Do not expand pre-existing insertion directives outside the approved edit.
    _validate_chart_references(source, prepared['charts'])
    _heading_signature(source)
    task = copy.deepcopy(prepared['writing_task'])
    reviewing = task['meta']['mode'] == 'revise'
    if not reviewing:
        task['query'] += DRAFT_MODIFICATION_INSTRUCTIONS
    editor = editor if editor is not None else WriterRevisionToolkit()
    context_json = _json(prepared['writing_context'])
    if reviewing:
        report_progress(20, '正在根据选中审稿意见规划修改')
        plan, operation_ranges, issue_bounds = _review_plan(source, task, prepared, editor, context_json)
    else:
        report_progress(20, '正在定位修改范围')
        located = editor.locate_revision_target(
            writing_task_json=_json(task), writer_document_json=source,
            writing_context_json=context_json,
        )
        report_progress(35, '正在规划稿件修改')
        plan = MarkdownModifyPlan.model_validate(json.loads(editor.generate_modify_plan(
            writing_task_json=_json(task), writer_document_json=source,
            locate_result_json=located, writing_context_json=context_json,
        )))
    if reviewing and not plan.instructions and not plan.title_instruction:
        raise contracts.MissingMaterials('选中意见需要补充材料或实验，未产生可应用的文本修订。')
    if plan.title_instruction or not plan.instructions:
        raise ValueError('Writer must return explicit manuscript modification operations.')
    if any(item.visual_instruction for item in plan.instructions):
        raise ValueError('Paper manuscript editing does not generate visual assets.')
    if not reviewing:
        operation_ranges = _operation_ranges(source, plan)
    allowed = [bounds for ranges in operation_ranges for bounds in ranges]
    report_progress(50, '正在生成修改内容')
    changes, located_changes = _bounded_replacements(source, allowed, json.loads(editor.generate_string_replace_set(
        markdown_document=source, modify_plan_json=plan.model_dump_json(), writing_context_json=context_json,
    )))
    if reviewing:
        # Every issue needs an actual edit in its associated operations. This is
        # mechanical coverage, not proof that the semantic problem was solved.
        for issue in prepared['selected_issues']:
            if issue['action_type'] != 'text_revision':
                continue
            allowed = [bounds for item, ranges in zip(plan.instructions, operation_ranges)
                       if issue['id'] in item.meta['issue_ids'] for bounds in ranges]
            effective = any(
                replacement.old_string.replace('\r\n', '\n').replace('\r', '\n') !=
                replacement.new_string.replace('\r\n', '\n').replace('\r', '\n')
                and _within_range(start, end, allowed)
                and _within_range(start, end, [issue_bounds[issue['id']]])
                for start, end, replacement in located_changes
            )
            if not effective:
                raise ValueError('Writer produced no effective edit for selected issue: ' + issue['id'])
    report_progress(80, '正在应用稿件修改')
    applied = json.loads(editor.apply_string_replace(
        markdown_document=source, string_replace_set_json=_json(changes),
        writing_context_json=context_json,
    ))
    if not applied['string_replace_result']['success']:
        raise ValueError('Writer failed to apply manuscript edits.')
    body = _materialize_chart_insertions(applied['revised_document'], prepared['charts'])
    # Toolkit reads Markdown with universal newlines; normalization alone is not an edit.
    if body == source.replace('\r\n', '\n').replace('\r', '\n'):
        raise ValueError('Writer produced no manuscript change.')
    _validate_manuscript(body, None, prepared['charts'], constraints, toolkit, context_json,
                         candidates=prepared['reference_candidates'], original=source,
                         check_quality=not reviewing)
    events = current_events()
    if events:
        events.delta(body)  # Match public Writer targeted revision: show the completed full text once.
    return {'outline': _outline_from_markdown(body), 'manuscript': body, 'is_demo': False}


# ======= 新稿写作：大纲、章节规划、生成与组装 =======

def _write_draft(inputs, *, toolkit=None, on_delta=None):
    """Internal orchestration; callbacks are not part of the business input."""
    toolkit = toolkit if toolkit is not None else WriterCreateToolkit()
    report_progress(10, '正在准备写作材料')
    prepared = _trace_call('prepare', prepare_writer_context, inputs, toolkit=toolkit)
    task = prepared['writing_task']
    # Charts already supply captions and explicit finding associations. Do not ask Writer to
    # search for or generate media; the publisher resolves media placeholders to saved SVG resources.
    task['constraints']['visual_policy'] = {'allow_visuals': False}
    if prepared['manuscript'] is not None:
        return _modify_manuscript(prepared, inputs['constraints'], toolkit)
    context_json = _json(prepared['writing_context'])
    outline = prepared['outline']
    original_signature = _heading_signature(outline) if outline else None
    needs_ownership = bool(prepared['charts']) and not _CHART_OWNERS.search(outline or '')
    if not outline or needs_ownership:
        report_progress(15, '正在生成大纲和图表归属' if prepared['charts'] else '正在生成大纲')
        outline_task = copy.deepcopy(task)
        if prepared['charts']:
            outline_task['query'] += '\n' + CHART_OUTLINE_INSTRUCTIONS + _json(prepared['chart_directory'])
        if outline:
            outline_task['query'] += '\nAdd only chart ownership markers. Preserve the following outline headings, hierarchy and order verbatim:\n' + outline
        outline = _trace_call('outline', toolkit.generate_outline,
            writing_task_json=_json(outline_task), writing_context_json=context_json,
        )
        if original_signature and _heading_signature(outline) != original_signature:
            raise ValueError('Chart ownership planning changed the supplied outline structure.')
    if prepared['charts']:
        _outline_chart_owners(outline, prepared['charts'])
    task_json = _json(task)
    signature = _heading_signature(outline)
    # 章节规划与正文生成沿用公共 Writer，补充论文约束。
    report_progress(25, '正在规划章节')
    planned = json.loads(_trace_call('section_plan', toolkit.generate_section_instructions,
        writing_task_json=task_json, outline_json=_CHART_OWNERS.sub('', outline), writing_context_json=context_json,
    ))
    plan = _prepare_sections(planned['section_instructions'], outline, inputs['constraints'])
    _bind_chart_references(plan, prepared, outline)
    events = current_events()
    # Capture bound methods explicitly: parallel workers need not inherit ContextVars.
    callbacks = _section_callbacks(events)
    report_progress(30, '正在生成正文')
    sections = json.loads(_trace_call('draft_sections', toolkit.stream_draft_blocks_markdown,
        writing_task_json=task_json, section_instructions_json=_json(plan),
        writing_context_json=context_json,
        on_delta=on_delta or (events.delta if events else lambda delta: None), **callbacks,
    ))
    if not isinstance(sections, list) or not sections or any(not isinstance(s, str) or not s.strip() for s in sections):
        raise ValueError('Writer returned no complete draft sections.')
    # 组装完整稿件。
    report_progress(83, '正在组装全文')
    result = json.loads(_trace_call('assemble', toolkit.generate_draft_document_markdown,
        draft_sections_json=_json(sections), writing_context_json=context_json,
        outline_json=_CHART_OWNERS.sub('', outline), title=signature[0][1],
    ))
    body = result['draft_document']
    objects = _validate_chart_references(body, prepared['charts'])
    if {match[1] for match in objects} != {chart['id'] for chart in prepared['charts']}:
        raise ValueError('Draft must insert every planned chart exactly once.')
    if 'paper-charts:' in body:
        raise ValueError('Internal chart ownership leaked into manuscript.')
    _validate_manuscript(body, signature, prepared['charts'], inputs['constraints'], toolkit, context_json,
                         candidates=prepared['reference_candidates'])
    return {'outline': _outline_from_markdown(body), 'manuscript': body, 'is_demo': False}


# ======= 写作前置条件与输出检查 =======

def resolve_review_target(issue, manuscript):
    """Resolve a displayed heading and quote uniquely; never widen failed matches."""
    location = issue['location'].strip()
    quote = issue['quote']
    if location.casefold() in {'全文', '全文级', 'document', 'document_root'}:
        if quote is not None and quote not in manuscript:
            raise ValueError('Review quote is not in document.')
        return {'node_id': None, 'placeholder_id': None, 'heading_path': [],
                'document_root': True, 'occurrence': 1}

    def normalize(title):
        title = re.sub(r'^#{1,6}\s+', '', title.strip())
        return strip_heading_numbering(title)

    wanted = [normalize(x) for x in re.split(r'\s*>\s*|\s*/\s*', location)]
    matches = []
    for _, path, occurrence, _ in parse_markdown_sections(manuscript):
        normalized = [normalize(x) for x in path]
        if normalized[-len(wanted):] != wanted:
            continue
        left, right = markdown_section_range(manuscript, path, occurrence)
        if quote is not None and quote not in manuscript[left:right]:
            continue
        matches.append({'node_id': None, 'placeholder_id': None, 'heading_path': path,
                        'document_root': False, 'occurrence': occurrence})
    if len(matches) != 1:
        raise ValueError('Review location + quote must match exactly one section; missing or ambiguous target.')
    return matches[0]


def validate_inputs(inputs):
    """Writing-specific prerequisites after the shared material contract."""
    if inputs['outline'] and not inputs['manuscript']:
        _heading_signature(inputs['outline'])
    if inputs['manuscript']:
        _heading_signature(inputs['manuscript'])
    selected = set(inputs['selected_issue_ids'])
    if inputs['mode'] == 'draft':
        if inputs['review'] is not None or inputs.get('review_data') is not None or selected:
            raise ValueError('draft 不接受审稿意见或选中 ID')
    else:
        review = inputs['review_data']
        if not selected or not selected <= {i['id'] for i in review['issues']}:
            raise ValueError('selected_issue_ids 必须非空且命中当前审稿意见')


def validate_result(value, inputs):
    """Check writing output independently of storage and public field validation."""
    for key in ('outline', 'manuscript'):
        if value[key] is not None and len(value[key]) > 2_000_000:
            raise ValueError(f'{key} 超过长度限制')
    signature = _validate_manuscript_structure(value['manuscript'],
        charts=inputs['charts'] if inputs else None,
        candidates=_reference_candidates(inputs['papers']) if inputs else None,
        original=inputs['manuscript'] if inputs else None)
    if value['outline'] is None or _heading_signature(value['outline']) != signature:
        raise ValueError('输出大纲与稿件不一致')


# ======= 业务入口：按 mode 分流 =======

def run(inputs: dict) -> dict:
    """Normalized v2 business input -> complete Markdown, with no publication."""
    if inputs['mode'] == 'revise':
        report_progress(10, '正在准备修订材料')
        toolkit = WriterCreateToolkit()
        prepared = prepare_writer_context(inputs, toolkit=toolkit)
        prepared['writing_task']['constraints']['visual_policy'] = {'allow_visuals': False}
        return _modify_manuscript(prepared, inputs['constraints'], toolkit)
    if inputs['mode'] != 'draft':
        raise ValueError('Unsupported writing mode.')
    return _write_draft(inputs)
