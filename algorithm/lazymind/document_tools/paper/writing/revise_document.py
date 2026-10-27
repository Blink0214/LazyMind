"""Scoped paper revision using the public Writer revision toolkit."""
from ..prompts.write_document_prompt import REVIEW_REVISION_INSTRUCTIONS, REVISION_SCOPE_INSTRUCTIONS
from lazyllm.tools.writer.data_models.revision import MarkdownModifyPlan, StringReplaceSet
from ...toolkits import WriterCreateToolkit, WriterRevisionToolkit
from ..publication import validate_chart_references as _validate_chart_references
from .. import contracts
import copy
from ..runtime_events import current_events, report_progress
import json
from lazyllm import AutoModel
from lazyllm.tools.writer.tools.base import _WriterJsonFormatter
from lazyllm.tools.writer.utils import locate_markdown_paragraph
from lazyllm.tools.writer.utils.serialization import markdown_section_range

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
    raw = str(AutoModel(model='llm')(REVISION_SCOPE_INSTRUCTIONS + _json({
        'request': request, 'issues': review['issues'],
    }))).strip()
    result = _WriterJsonFormatter()(raw)
    if not isinstance(result, dict):
        raise ValueError('Invalid revision intent response')
    intent = result.get('intent')
    if intent in {'direct', 'unclear'} and set(result) == {'intent'}:
        if intent == 'direct':
            return []
        raise contracts.MissingMaterials('请明确本轮修改要求或要采用的审稿意见编号。')
    if intent != 'review' or result.get('selection') not in {'all', 'subset'}:
        raise ValueError('Invalid revision intent response')
    issue_ids = [issue['id'] for issue in review['issues']]
    if result['selection'] == 'all':
        if set(result) != {'intent', 'selection'}:
            raise ValueError('Invalid revision intent response')
        if not issue_ids:
            raise contracts.MissingMaterials('当前审稿没有待处理意见，无需按审稿修订；如需其他修改，请说明具体要求。')
        return issue_ids
    if set(result) != {'intent', 'selection', 'issue_ids'}:
        raise ValueError('Invalid revision intent response')
    selected = result['issue_ids']
    if (not isinstance(selected, list) or not selected
            or any(not isinstance(key, str) or key not in issue_ids for key in selected)):
        raise contracts.MissingMaterials('请选择当前审稿报告中有效的意见编号，选择不能为空。')
    return [key for key in issue_ids if key in selected]


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
    matched_ranges = []
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
        matched_ranges.append((start, end))
        # Writer may include unchanged surrounding text to make a replacement
        # unique. Check what actually changes, not that matching context.
        old, new = replacement.old_string, replacement.new_string
        prefix = 0
        while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
            prefix += 1
        old_end, new_end = len(old), len(new)
        while old_end > prefix and new_end > prefix and old[old_end - 1] == new[new_end - 1]:
            old_end -= 1
            new_end -= 1
        changed_start, changed_end = start + prefix, start + old_end
        if not _within_range(changed_start, changed_end, allowed):
            raise ValueError('Writer replacement exceeds the planned modification scope.')
        located.append((changed_start, changed_end, replacement))
    located.sort(key=lambda item: item[0])
    # The shared applier still replaces the full strings, so overlapping matching
    # contexts remain unsafe even when their changed characters are disjoint.
    matched_ranges.sort()
    if any(left[1] > right[0] for left, right in zip(matched_ranges, matched_ranges[1:])):
        raise ValueError('Writer returned overlapping manuscript edits.')
    replacements.replacements = [item[2] for item in reversed(located)]
    return replacements.model_dump(), located

def _validate_review_plan(plan, issues):
    """Check selected-issue coverage; public Writer owns manuscript locating."""
    selected = {issue['id'] for issue in issues}
    covered = set()
    for item in plan.instructions:
        ids = item.meta.get('issue_ids')
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(key, str) or key not in selected for key in ids)):
            raise ValueError('Review plan operations must reference selected issue_ids.')
        covered.update(ids)
    required = {issue['id'] for issue in issues if issue['action_type'] == 'text_revision'}
    if not required <= covered:
        raise ValueError('Review plan does not cover all selected issues.')

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
    issues = prepared['selected_issues'] if reviewing else []
    if reviewing:
        if not issues:
            raise ValueError('Review revision requires selected issues; empty is not all.')
        task['query'] += REVIEW_REVISION_INSTRUCTIONS + _json(issues)
    try:
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
    except ValueError as exc:
        if (reviewing and not any(i['action_type'] == 'text_revision' for i in issues)
                and str(exc) in {'modify_plan contains no operations for the located revision scope.',
                                 'locate_result contains no revision targets.'}):
            raise contracts.MissingMaterials(
                '所选意见仍需补充材料或实验，已保留为待补事项；本轮没有可执行的文本修改，稿件未修改。') from exc
        raise
    if reviewing:
        _validate_review_plan(plan, issues)
        if not plan.instructions and not plan.title_instruction:
            raise contracts.MissingMaterials('所选意见仍需补充材料或实验，稿件未修改。')
    if plan.title_instruction or not plan.instructions:
        raise ValueError('Writer must return explicit manuscript modification operations.')
    if any(item.visual_instruction for item in plan.instructions):
        raise ValueError('Paper manuscript editing does not generate visual assets.')
    operation_ranges = _operation_ranges(source, plan)
    allowed = [bounds for ranges in operation_ranges for bounds in ranges]
    report_progress(50, '正在生成修改内容')
    changes, located_changes = _bounded_replacements(source, allowed, json.loads(editor.generate_string_replace_set(
        markdown_document=source, modify_plan_json=plan.model_dump_json(), writing_context_json=context_json,
    )))
    if reviewing:
        # Every selected text revision needs an edit in its associated operations. This is
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
        events.completed_preview(body)
    return {'manuscript': body, 'is_demo': False}

def run(inputs):
    """Revise a manuscript from the current request or selected current review issues."""
    report_progress(10, '正在准备修订材料')
    toolkit = WriterCreateToolkit()
    prepared = prepare_writer_context(inputs, toolkit=toolkit)
    prepared['writing_task']['constraints']['visual_policy'] = {'allow_visuals': False}
    return _modify_manuscript(prepared, inputs['constraints'], toolkit)
