"""Scoped paper revision using the public Writer revision toolkit."""
from ..prompts.write_document_prompt import REVIEW_REVISION_INSTRUCTIONS, REVISION_SCOPE_INSTRUCTIONS
from lazyllm.tools.writer.data_models.revision import LocateResult, MarkdownModifyPlan, StringReplaceSet
from ...toolkits import WriterCreateToolkit, WriterRevisionToolkit
from ..publication import validate_chart_references as _validate_chart_references
from .. import contracts
import copy
from ..runtime_events import current_events, report_progress
import json
from lazyllm import AutoModel
import re
from lazyllm.tools.writer.utils import locate_markdown_paragraph
from lazyllm.tools.writer.utils.serialization import markdown_section_range, parse_markdown_sections

from .common import (
    _heading_signature,
    _json,
    _materialize_chart_insertions,
    _validate_manuscript,
    prepare_writer_context,
    validate_inputs,
    validate_result,
)


def resolve_review_issues(request, review):
    """Resolve review use, deriving internal issue IDs from the current report.

    Version compatibility is checked by the material contract; it is not consent
    to use a historical review. No saved or model-generated selection is reused.
    """
    if not request.strip():
        raise contracts.MissingMaterials('请明确本轮修改要求，或说明按当前审稿意见修订。')
    if review is None:
        return []
    raw = str(AutoModel(model='llm')(REVISION_SCOPE_INSTRUCTIONS + _json({'request': request}))).strip()
    raw = re.sub(r'^(?:<think\b[^>]*>.*?</think>\s*)+', '', raw, flags=re.I | re.S)
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != {'intent'}:
        raise ValueError('Invalid revision intent response')
    if result['intent'] == 'direct':
        return []
    if result['intent'] != 'review':
        raise contracts.MissingMaterials('请明确直接修改论文，还是按当前完整审稿意见修订；暂不支持选择部分意见。')
    issue_ids = [issue['id'] for issue in review['issues']]
    if not issue_ids:
        raise contracts.MissingMaterials('当前审稿没有待处理意见，无需按审稿修订；如需其他修改，请说明具体要求。')
    return issue_ids


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
            raise contracts.MissingMaterials('审稿意见需要补充材料或实验，未产生可应用的文本修订。') from exc
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

def _modify_manuscript(prepared: dict, constraints, toolkit, *, editor=None):
    """Apply request edits or review-bound edits with the same public toolkit."""
    source = prepared['manuscript']
    # Do not expand pre-existing insertion directives outside the approved edit.
    _validate_chart_references(source, prepared['charts'])
    _heading_signature(source)
    task = copy.deepcopy(prepared['writing_task'])
    reviewing = task['meta'].get('review_driven', False)
    editor = editor if editor is not None else WriterRevisionToolkit()
    context_json = _json(prepared['writing_context'])
    if reviewing:
        report_progress(20, '正在根据当前审稿意见规划修改')
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
        raise contracts.MissingMaterials('审稿意见需要补充材料或实验，未产生可应用的文本修订。')
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
    # Targeted edits preserve unrelated defects; full-paper review is a separate user choice.
    _validate_manuscript(body, None, prepared['charts'], constraints, toolkit, context_json,
                         candidates=prepared['reference_candidates'], original=source,
                         check_quality=False, check_length=False)
    events = current_events()
    if events:
        events.delta(body)  # Match public Writer targeted revision: show the completed full text once.
    return {'manuscript': body, 'is_demo': False}

def run(inputs):
    """Revise a manuscript from the current request or the complete current review."""
    report_progress(10, '正在准备修订材料')
    toolkit = WriterCreateToolkit()
    prepared = prepare_writer_context(inputs, toolkit=toolkit)
    prepared['writing_task']['constraints']['visual_policy'] = {'allow_visuals': False}
    return _modify_manuscript(prepared, inputs['constraints'], toolkit)
