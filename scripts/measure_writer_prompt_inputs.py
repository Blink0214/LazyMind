"""Offline replay of real Writer inputs; never invokes a model or modifies source artifacts."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from lazyllm.tools.writer.data_models import WritingContext, WritingTask
from lazyllm.tools.writer.data_models.planning import SectionInstructionList
from lazyllm.tools.writer.tools import drafting_tools, planning_tools, quality_tools, revision_tools
from lazyllm.tools.writer.utils import to_prompt_json
from lazymind.document_tools.paper import contracts, workflow
from lazymind.document_tools.paper.prompts.write_document_prompt import CHART_OUTLINE_INSTRUCTIONS
from lazymind.document_tools.paper.writing import common


class Captured(BaseException):
    pass


def read(path):
    value = json.loads(Path(path).read_text())
    return value['data'] if isinstance(value, dict) and 'data' in value else value


def capture(module, name, operation):
    template = getattr(module, name)
    result = {}

    class Template:
        def format(self, **values):
            prompt = template.format(**values)
            parts = dict(task=0, context=0, instructions=0, source=0, other=0)
            for key, value in values.items():
                group = ('task' if key == 'task_json' else 'context' if key == 'context_json'
                         else 'instructions' if key in {'section_instruction_json', 'instructions_json',
                                                       'operations_json', 'instruction_json', 'modify_plan_json'}
                         else 'source' if key in {'document_content', 'draft_document_content',
                                                 'previous_markdown', 'markdown_block'} else 'other')
                if key == 'context_json':
                    data = json.loads(value)
                    if isinstance(data, dict) and 'task' in data and 'context' in data:
                        for item in ('task', 'context'):
                            parts[item] += len(to_prompt_json(data[item]))
                        parts['other'] += len(value) - len(to_prompt_json(data['task'])) - len(to_prompt_json(data['context']))
                        continue
                parts[group] += len(value) if isinstance(value, str) else len(str(value))
            parts['template'] = len(prompt) - sum(parts.values())
            schema_name = {
                'VALIDATE_DRAFT_DOCUMENT_PROMPT': 'AuditResult',
                'LOCATE_REVISION_TARGET_MARKDOWN_PROMPT': 'LocateResult',
                'GENERATE_MODIFY_PLAN_MARKDOWN_PROMPT': 'MarkdownModifyPlan',
                'GENERATE_MARKDOWN_REVISION_CONTENT_PROMPT': 'MarkdownRevisionContent',
                'GENERATE_MARKDOWN_INSERTION_CONTENT_PROMPT': 'MarkdownInsertionContent',
                'GENERATE_MARKDOWN_REVISION_BATCH_PROMPT': 'MarkdownRevisionBatch',
                'GENERATE_STRING_REPLACE_SET_PROMPT': 'StringReplaceSet',
            }.get(name)
            parts['system_schema'] = (len(module.WriterToolBase()._structured_output_prompt(getattr(module, schema_name)))
                                      if schema_name else 0)
            total = sum(parts.values())
            result.update(total=total, chars=parts,
                          percent={key: round(100 * count / total, 2) for key, count in parts.items()})
            raise Captured()

    with patch.object(module, name, Template()):
        try:
            operation()
        except Captured:
            pass
    if not result:
        raise RuntimeError(f'No prompt captured: {name}')
    return result


def measure(task, contexts, outline, manuscript, directory, prepared=None, constraints=None, unbound_historical=False):
    task = WritingTask.model_validate(task)
    ctx = {key: WritingContext.model_validate(value) for key, value in contexts.items()}
    tasks = {stage: WritingTask.model_validate(common.stage_task(prepared, stage)) if prepared else task
             for stage in ('outline', 'section_plan', 'draft', 'audit', 'revision_locate')}
    task = tasks['draft']
    planning = planning_tools.WriterPlanningTools(artifact_store=directory)
    drafting = drafting_tools.WriterDraftingTools(artifact_store=directory)
    quality = quality_tools.WriterQualityTools(artifact_store=directory)
    revision = revision_tools.WriterRevisionTools(artifact_store=directory)
    outline_task = tasks['outline'].model_copy(deep=True)
    if prepared is not None:
        if prepared['charts']:
            outline_task.query += '\n' + CHART_OUTLINE_INSTRUCTIONS
        if outline:
            outline_task.query += ('\nAdd only missing chart ownership markers. Preserve every existing image assignment. '
                                   'Preserve the supplied outline headings, hierarchy and order in context verbatim.')
    rows = {'outline': capture(planning_tools, 'GENERATE_OUTLINE_MARKDOWN_PROMPT', lambda:
                              planning.generate_outline(outline_task, ctx['outline'], representation='markdown'))}
    planning_outline = common._CHART_OWNERS.sub('', outline) if prepared is not None else outline
    result = planning.generate_section_instructions(planning_outline, ctx['section_plan'], task=tasks['section_plan'])
    paths = result['metadata']['artifact_paths']
    plan = SectionInstructionList.model_validate(read(paths['section_instructions']))
    if prepared is not None:
        data = common._prepare_sections(plan.model_dump(), outline, constraints)
        if not unbound_historical:
            common._bind_chart_references(data, prepared, outline)
        plan = SectionInstructionList.model_validate(data)
    rows['section_plan'] = {'model_calls': 0, 'instructions': len(plan.instructions)}
    rows['sections'] = [capture(drafting_tools, 'GENERATE_DRAFT_SECTION_MARKDOWN_PROMPT', lambda item=item:
                              drafting._markdown_draft_prompt(task, item, ctx['draft'], None))
                        for item in plan.instructions]
    rows['whole_document'] = capture(drafting_tools, 'GENERATE_WHOLE_DOCUMENT_MARKDOWN_PROMPT', lambda:
                                    drafting.stream_whole_document(task, ctx['draft'], plan))
    if manuscript:
        rows['quality'] = capture(quality_tools, 'VALIDATE_DRAFT_DOCUMENT_PROMPT', lambda:
                                  quality.validate_draft_document(manuscript, ctx['audit'], task=tasks['audit']))
        revised = tasks['revision_locate'].model_copy(update={'task_type': 'revise'})
        rows['revision_locate'] = capture(revision_tools, 'LOCATE_REVISION_TARGET_MARKDOWN_PROMPT', lambda:
                                         revision.locate_revision_target(revised, manuscript, ctx['revision_plan']))
    rows['material_chars_per_section'] = [dict(key=f.key, chars=len(f.value), locked=f.locked)
                                         for f in ctx['draft'].facts]
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--f5-pool', required=True)
    parser.add_argument('--basic-task', required=True)
    parser.add_argument('--basic-context', required=True)
    parser.add_argument('--basic-outline', required=True)
    parser.add_argument('--basic-manuscript', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--revision-dir', help='Real saved revision planning directory with task, document and locate result.')
    parser.add_argument('--revision-plan', help='Real saved Markdown modify plan.')
    parser.add_argument('--duplicate-chart-findings-baseline', action='store_true')
    parser.add_argument('--unbound-historical-outline', action='store_true',
                        help='Measure historical materials before chart ownership existed; excludes placement instructions.')
    args = parser.parse_args()
    pool = read(args.f5_pool)
    inputs = contracts.validate_input('write_document', workflow.business_input('write_document', pool, pool['request']))
    if args.duplicate_chart_findings_baseline:
        original = common._chart_directory
        def duplicated(chart, findings):
            value = original(chart, findings)
            if 'finding_ids' in value:
                value['findings'] = [findings[key] for key in value['finding_ids']]
            return value
        with patch.object(common, '_chart_directory', duplicated):
            prepared = common.prepare_writer_inputs(inputs)
    else:
        prepared = common.prepare_writer_inputs(inputs)
    prepared['writing_task']['constraints']['visual_policy'] = {'allow_visuals': False}
    stages = ('outline', 'section_plan', 'draft', 'revision_plan', 'revision_content', 'audit')
    contexts = {stage: common.stage_context(prepared, stage) for stage in stages}
    with TemporaryDirectory() as directory:
        f5 = measure(prepared['writing_task'], contexts, inputs['outline'], inputs['manuscript'], directory, prepared, inputs['constraints'], args.unbound_historical_outline)
        basic_context = read(args.basic_context)
        basic = measure(read(args.basic_task), {stage: basic_context for stage in stages},
                        Path(args.basic_outline).read_text(), Path(args.basic_manuscript).read_text(), directory)
    revision_rows = {}
    if args.revision_dir:
        root = Path(args.revision_dir)
        revision = revision_tools.WriterRevisionTools()
        task = WritingTask.model_validate(read(root / 'writing_task.json'))
        source = (root / 'writer_document.md').read_text()
        context = WritingContext.model_validate(read(root / 'writing_context.json'))
        revision_rows['locate'] = capture(revision_tools, 'LOCATE_REVISION_TARGET_MARKDOWN_PROMPT', lambda:
            revision.locate_revision_target(task, source, context))
        revision_rows['plan'] = capture(revision_tools, 'GENERATE_MODIFY_PLAN_MARKDOWN_PROMPT', lambda:
            revision.generate_modify_plan(task, source, read(root / 'locate_result.json'), context))
        plan = read(args.revision_plan)
        # Probe each possible first-call template without allowing model execution.
        for name in ('GENERATE_MARKDOWN_REVISION_CONTENT_PROMPT', 'GENERATE_MARKDOWN_INSERTION_CONTENT_PROMPT',
                     'GENERATE_MARKDOWN_REVISION_BATCH_PROMPT', 'GENERATE_STRING_REPLACE_SET_PROMPT'):
            try:
                with patch.object(revision, '_call_llm_structured', side_effect=Captured):
                    revision_rows['content'] = capture(revision_tools, name, lambda:
                        revision.generate_string_replace_set(source, plan, context, task=task))
                break
            except RuntimeError:
                continue
        if 'content' not in revision_rows:
            raise RuntimeError('No revision content prompt captured')
    output = {'method': 'Current-code offline prompt reconstruction from real local artifacts; characters, not tokens. '
                        'Includes Writer structured-output system/schema; excludes provider-injected messages and transport wrappers. No model requests. Sections and whole-document are alternative generation paths. '
                        'Revision locate reuses the task text to measure input size, not revision quality. '
                        'No historical latency claim. Section plans regenerated deterministically.',
              'sources': vars(args), 'f5': f5, 'basic': basic, 'saved_revision': revision_rows}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
