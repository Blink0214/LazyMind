import copy
import json
from typing import Iterator

import lazyllm
from lazyllm import AutoModel, ThreadPoolExecutor
from lazyllm.tools.writer.tools.base import WriterToolBase
from pydantic import BaseModel, ConfigDict, Field

from lazymind.chat.engine.agent_runtime.budget import build_context_budget
from lazymind.common.token_estimation import estimate_tokens
from lazymind.model_config import _role_entry, load_model_config

from .contracts import MissingMaterials, validate_input
from .material_bindings import MaterialBindings
from .runtime_events import report_progress
from .prompts.literature_review_prompt import (
    INPUT_PROMPT,
    READING_NOTE_PROMPT,
    SYNTHESIS_PROMPT,
)


class _ReadingNote(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    note: str = Field(min_length=1)
    citation: str


def _input_budget() -> int:
    injected = lazyllm.globals['config'].get('dynamic_model_configs', {})
    role = injected.get('llm', {}).get('chat') or _role_entry(load_model_config().get('llm'))
    return build_context_budget(llm_config={'llm': role}).effective_input_budget


def _prompt(instruction: str, data: dict) -> str:
    data = copy.deepcopy(data)

    def render(value):
        return INPUT_PROMPT.format(instruction=instruction, data=json.dumps(value, ensure_ascii=False))

    limit = _input_budget()
    prompt = render(data)
    if estimate_tokens(prompt) <= limit:
        return prompt
    data['truncated'] = True
    passages = []
    for key in ('papers', 'sources', 'background'):
        for paper in data.get(key, []):
            field = max((name for name in ('note', 'text', 'abstract', 'snippet') if paper.get(name)),
                        key=lambda name: estimate_tokens(paper[name]), default=None)
            if field:
                passages.append((paper, field, paper[field]))
                paper[field] = ''
    available = limit - estimate_tokens(render(data))
    if not passages or available < len(passages):
        raise ValueError('研究要求和文献信息超出模型输入预算，请缩短要求或减少材料。')
    for paper, field, text in passages:
        paper[field] = next(_chunks(text, available // len(passages)))
    prompt = render(data)
    if estimate_tokens(prompt) > limit:
        raise ValueError('文献材料截断后仍超出模型输入预算。')
    return prompt


def _chunks(text: str, max_tokens: int) -> Iterator[str]:
    while text:
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens(json.dumps(text[:middle], ensure_ascii=False)) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        if low == 0:
            raise ValueError('研究要求和资料信息已占满模型的可用输入空间。')
        yield text[:low]
        text = text[low:]


def _read_note(source: dict, context: dict, input_tokens: int) -> dict:
    writer = WriterToolBase(llm=AutoModel(model='llm'))
    text = source.get('text') or ''
    if not text.strip():
        text = source['abstract']
        source['reading'] = {'content_type': 'abstract'}
    data = {**{key: value for key, value in source.items() if key not in ('text', 'abstract')},
            'research': context, 'source_id': source['id'],
            'citation': source.get('citation', ''), 'text': ''}
    while True:
        overhead = estimate_tokens(INPUT_PROMPT.format(
            instruction=READING_NOTE_PROMPT, data=json.dumps(data, ensure_ascii=False),
        ))
        notes = []
        for chunk in _chunks(text, input_tokens - overhead):
            reading = writer._call_llm_structured(
                INPUT_PROMPT.format(instruction=READING_NOTE_PROMPT,
                                    data=json.dumps({**data, 'text': chunk}, ensure_ascii=False)),
                _ReadingNote,
            )
            notes.append(reading.note)
            if not data['citation']:
                data['citation'] = reading.citation
        if len(notes) == 1:
            source['citation'] = data['citation']
            return {**{key: source[key] for key in (
                'id', 'title', 'citation', 'reading', 'is_seed', 'is_important',
                'seed_relation', 'direction_relation', 'key_contribution',
            ) if key in source}, 'note': notes[0]}
        condensed = '\n\n'.join(notes)
        if estimate_tokens(condensed) >= estimate_tokens(text):
            raise ValueError('阅读笔记未能缩短输入内容，无法继续合并。')
        text = condensed


def _notes(sources: list[dict], context: dict) -> list[dict]:
    if not sources:
        return []
    input_tokens = _input_budget()
    with ThreadPoolExecutor() as executor:
        return list(executor.map(lambda source: _read_note(source, context, input_tokens), sources))


def _render(inputs: dict, notes: list[dict], background: list[dict]) -> str:
    report_progress(85, '正在生成文献综述')
    bindings = MaterialBindings(notes + background)
    from .runtime_events import generate_markdown
    prompt = _prompt(SYNTHESIS_PROMPT, bindings.model_view({
        'research': {key: inputs.get(key) for key in ('topic', 'request', 'question')},
        'sources': notes, 'background': background,
    }))
    return generate_markdown(AutoModel(model='llm'), prompt, bindings=bindings)


def run(inputs: dict) -> dict:
    inputs = validate_input('literature_review', inputs)
    sources = [paper for paper in inputs['papers'] if paper.get('review_role') != 'background']
    background = [paper for paper in inputs['papers'] if paper.get('review_role') == 'background']
    if not sources:
        raise MissingMaterials('没有可综述的论文，请选择知识库材料、上传论文或提供已选论文。')
    context = {key: inputs.get(key) for key in ('topic', 'request', 'question')}
    report_progress(20, '正在整理已选论文的阅读笔记')
    notes = _notes(sources, context)
    background_notes = _notes(background, context)
    return {'evidence': _render(inputs, notes, background_notes), 'papers': sources, 'is_demo': False}
