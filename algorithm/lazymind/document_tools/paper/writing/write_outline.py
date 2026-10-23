"""Generate an editable outline with public Writer streaming."""
from ..prompts.write_document_prompt import CHART_OUTLINE_INSTRUCTIONS
from ...toolkits import WriterCreateToolkit
import copy
from ..runtime_events import current_events, report_progress

from .common import (
    _CHART_OWNERS,
    _heading_signature,
    _json,
    _outline_chart_owners,
    _trace_call,
    prepare_writer_context,
    validate_inputs as validate_writing_inputs,
)


def prepare_outline(prepared, toolkit, *, on_delta=None):
    """Reuse Writer outline planning for both the approval step and legacy drafts."""
    task = prepared['writing_task']
    context_json = _json(prepared['writing_context'])
    outline = prepared['outline']
    original_signature = _heading_signature(outline) if outline else None
    previous = _outline_chart_owners(outline or '', prepared['charts'], require_complete=False)
    needs_ownership = len(previous) != len(prepared['charts'])
    if not outline or needs_ownership:
        report_progress(15, '正在生成大纲和图表归属' if prepared['charts'] else '正在生成大纲')
        outline_task = copy.deepcopy(task)
        if prepared['charts']:
            outline_task['query'] += '\n' + CHART_OUTLINE_INSTRUCTIONS + _json(prepared['chart_directory'])
        if outline:
            outline_task['query'] += '\nAdd only missing chart ownership markers. Preserve every existing image assignment. Preserve the following outline headings, hierarchy and order verbatim:\n' + outline
        generate = toolkit.stream_outline if on_delta is not None else toolkit.generate_outline
        kwargs = {'on_delta': on_delta} if on_delta is not None else {}
        outline = _trace_call('outline', generate,
            writing_task_json=_json(outline_task), writing_context_json=context_json, **kwargs,
        )
        if original_signature and _heading_signature(outline) != original_signature:
            raise ValueError('Chart ownership planning changed the supplied outline structure.')
    if prepared['charts']:
        owners = _outline_chart_owners(outline, prepared['charts'])
        if any(owners.get(key) != value for key, value in previous.items()):
            raise ValueError('Image planning moved an existing assignment.')
    _heading_signature(outline)
    return outline

def validate_inputs(inputs):
    validate_writing_inputs(inputs)
    if inputs['manuscript']:
        raise ValueError('Use revise_document to edit an existing manuscript.')


def run(inputs):
    toolkit = WriterCreateToolkit()
    prepared = prepare_writer_context(inputs, toolkit=toolkit)
    prepared['writing_task']['constraints']['visual_policy'] = {'allow_visuals': False}
    events = current_events()

    def on_delta(text):
        if text.strip():
            report_progress(35, '正在输出大纲内容')
        if events is not None:
            events.delta(text)

    outline = prepare_outline(prepared, toolkit, on_delta=on_delta)
    report_progress(85, '大纲已生成，正在校验并保存')
    return {'outline': outline, 'is_demo': False}
