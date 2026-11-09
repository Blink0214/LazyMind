"""Measure stage rules and chart-ID-only material selection without generating text."""
import argparse
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from measure_writer_prompt_inputs import read, capture, measure
from lazyllm.tools.writer.data_models import WritingTask, WritingContext
from lazyllm.tools.writer.data_models.planning import SectionInstructionList
from lazyllm.tools.writer.tools import drafting_tools, planning_tools
from lazymind.document_tools.paper import contracts, workflow
from lazymind.document_tools.paper.writing import common


def graph_candidates(prepared, outline, plan, context):
    owners = common._outline_chart_owners(outline, prepared['charts'])
    analysis = json.loads(next(f['value'] for f in context['facts'] if f['key'] == 'write_document:analysis'))
    all_ids = {f['id'] for f in analysis['findings']}
    linked = {ref for chart in prepared['charts'] for ref in chart['finding_ids']}
    if not linked <= all_ids:
        raise ValueError('Unresolved chart findings')
    rows = []
    covered = set()
    for instruction in plan.instructions:
        path = instruction.content_ref.heading_path
        charts = [chart for chart in prepared['charts']
                  if owners[chart['id']][0][:len(path)] == path]
        required = {ref for chart in charts for ref in chart['finding_ids']}
        keep = required | (all_ids - linked)
        candidate = copy.deepcopy(context)
        for fact in candidate['facts']:
            if fact['key'] == 'write_document:analysis':
                filtered = {**analysis, 'findings': [f for f in analysis['findings'] if f['id'] in keep]}
                fact['value'] = common._json(filtered)
        covered |= keep
        rows.append((instruction, candidate, {'heading_path': path, 'chart_ids': [c['id'] for c in charts],
                                              'kept_finding_ids': sorted(keep), 'omitted_finding_ids': sorted(all_ids - keep)}))
    if covered != all_ids:
        raise ValueError('The aggregate selection lost findings')
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pool', required=True)
    parser.add_argument('--outline', required=True)
    parser.add_argument('--outline-context', required=True)
    parser.add_argument('--baseline-rules', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    pool = read(args.pool)
    inputs = contracts.validate_input('write_document', workflow.business_input('write_document', pool, pool['request']))
    saved = read(args.outline_context)
    saved_analysis = json.loads(next(f['value'] for f in saved['facts'] if f['key'] == 'write_document:analysis'))
    if saved_analysis != inputs['analysis']:
        raise ValueError('The confirmed outline and pool do not share the same analysis')
    inputs['outline'] = Path(args.outline).read_text()
    prepared = common.prepare_writer_inputs(inputs)
    prepared['writing_task']['constraints']['visual_policy'] = {'allow_visuals': False}
    stages = ('outline', 'section_plan', 'draft', 'audit', 'revision_plan', 'revision_content')
    contexts = {stage: common.stage_context(prepared, stage) for stage in stages}
    legacy = json.loads(Path(args.baseline_rules).read_text())

    def baseline_task(bundle, stage):
        task = copy.deepcopy(bundle['writing_task'])
        task['query'] = '\n\n'.join(legacy[key] for key in (
            'WRITING_INSTRUCTIONS', 'MATERIAL_USAGE_INSTRUCTIONS', 'CHART_USAGE_INSTRUCTIONS',
            'REFERENCE_USAGE_INSTRUCTIONS')) + task['query']
        task['query'] += legacy['DRAFT_MODE_INSTRUCTIONS']
        task['query'] += ('\nUploaded documents are raw source material, including possible outlines and review advice. '
                          'Use them according to the current user request. Review suggestions are not research facts; '
                          'apply them only when the user asks, and never invent experimental results. '
                          'Follow a supplied outline when the current request asks to draft from it.')
        return task

    with TemporaryDirectory() as directory:
        new = measure(prepared['writing_task'], contexts, inputs['outline'], inputs['manuscript'],
                      directory, prepared, inputs['constraints'])
        with patch.object(common, 'stage_task', baseline_task):
            old = measure(prepared['writing_task'], contexts, inputs['outline'], inputs['manuscript'],
                          directory, prepared, inputs['constraints'])
        result = planning_tools.WriterPlanningTools(artifact_store=directory).generate_section_instructions(
            common._CHART_OWNERS.sub('', inputs['outline']), contexts['section_plan'],
            task=common.stage_task(prepared, 'section_plan'))
        plan = read(result['metadata']['artifact_paths']['section_instructions'])
        plan = common._prepare_sections(plan, inputs['outline'], inputs['constraints'])
        common._bind_chart_references(plan, prepared, inputs['outline'])
        plan = SectionInstructionList.model_validate(plan)
        tool = drafting_tools.WriterDraftingTools(artifact_store=directory)
        task = WritingTask.model_validate(common.stage_task(prepared, 'draft'))
        selection = []
        for instruction, candidate, ids in graph_candidates(prepared, inputs['outline'], plan, contexts['draft']):
            def render(context):
                return capture(drafting_tools, 'GENERATE_DRAFT_SECTION_MARKDOWN_PROMPT', lambda:
                               tool._markdown_draft_prompt(task, instruction, WritingContext.model_validate(context), None))
            selection.append({**ids, 'full': render(contexts['draft']), 'candidate': render(candidate)})
    output = {'sources': vars(args),
              'method': 'Offline Writer prompts including structured-output schema; characters, not tokens or latency. '
                        'Confirmed outline and pool analysis compared exactly; chart IDs validated. '
                        'Selection candidates are an UNSAFE placement-only upper bound, never used in production. '
                        'All unlinked findings and all other analysis fields/materials remain unchanged. '
                        'Figures may be cited across chapters, so ownership does not prove omission is safe.',
              'stage_rules_before': old, 'stage_rules_after': new, 'chart_selection_candidate': selection,
              'full_findings': len(inputs['analysis']['findings'])}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
