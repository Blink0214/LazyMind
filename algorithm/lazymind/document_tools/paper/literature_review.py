import copy
import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from time import sleep
from typing import Annotated, Iterator, Literal
from urllib.parse import unquote, urlsplit

import lazyllm
import httpx
from filelock import FileLock
from lazyllm import AutoModel, ThreadPoolExecutor
from lazyllm.common import KeyAuthError
from lazyllm.tools.writer.tools.base import WriterToolBase
from pydantic import BaseModel, ConfigDict, Field, create_model

from lazymind.chat.engine.agent_runtime.budget import build_context_budget
from lazymind.common.token_estimation import estimate_tokens
from lazymind.config import config
from lazymind.model_config import _role_entry, load_model_config

from .contracts import MissingMaterials, validate_input
from .material_bindings import MaterialBindings
from .runtime_events import report_progress
from .prompts.literature_review_prompt import (
    CANDIDATE_SELECTION_PROMPT,
    COVERAGE_PROMPT,
    INPUT_PROMPT,
    OVERVIEW_PROMPT,
    READING_NOTE_PROMPT,
    SEARCH_PLAN_PROMPT,
    SYNTHESIS_PROMPT,
)


class _SearchPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    queries: dict[str, Annotated[int, Field(ge=1)]]
    allow_external_search: bool = True
    year_from: int | None = None
    year_to: int | None = None
    paper_limit: int | None = Field(
        ge=1, description='The user\'s reference count target, retained even when exceeding it is allowed; null if unspecified.',
    )


class _ReadingNote(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    note: str = Field(min_length=1)
    citation: str


def _review_plan_schema(bindings: MaterialBindings):
    # The host fixes one field per existing object. Models assign roles, not identities.
    roles = create_model(
        'MaterialRoles', __config__=ConfigDict(extra='forbid'),
        **{key: (Literal['paper', 'background', 'exclude'], ...)
           for key in bindings.materials},
    )
    return create_model('ReviewPlan', __base__=_SearchPlan,
                        material_roles=(roles, ...), supplied_only=(bool, ...))


def _select_materials(plan, bindings: MaterialBindings) -> tuple[list[dict], list[dict]]:
    roles = plan.material_roles.model_dump()
    selected = [copy.deepcopy(paper) for key, paper in bindings.materials.items() if roles[key] == 'paper']
    background = [copy.deepcopy(paper) for key, paper in bindings.materials.items() if roles[key] == 'background']
    if plan.supplied_only:
        plan.queries = {}
        if not selected:
            raise MissingMaterials('指定范围内没有可综述的论文，请提供对应论文。')
    return selected, background


def _input_budget() -> int:
    injected = lazyllm.globals['config'].get('dynamic_model_configs', {})
    role = injected.get('llm', {}).get('chat') or _role_entry(load_model_config().get('llm'))
    return build_context_budget(llm_config={'llm': role}).effective_input_budget


def _prompt(instruction: str, data: dict) -> str:
    data = copy.deepcopy(data)
    for candidate in data.get('candidates', []):
        candidate.pop('snippet', None)

    def render(value):
        return INPUT_PROMPT.format(instruction=instruction, data=json.dumps(value, ensure_ascii=False))

    limit = _input_budget()
    prompt = render(data)
    if estimate_tokens(prompt) <= limit:
        return prompt
    data['truncated'] = True
    passages = []
    for key in ('papers', 'sources', 'candidates', 'background'):
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
            return {**{key: source[key] for key in ('id', 'title', 'citation')}, 'note': notes[0]}
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


def _identity(source: dict) -> set[tuple[str, str]]:
    metadata = source.get('metadata') or {}
    if source.get('kind') == 'knowledge_base':
        return {('knowledge_base', json.dumps(
            [metadata['kb_id'], metadata['doc_id']],
        ))}
    keys = set()
    title = unicodedata.normalize('NFKC', source['title']).casefold()
    keys.add(('title', ''.join(char for char in title if char.isalnum())))
    url = source.get('url', '')
    parts = urlsplit(url)
    if parts.netloc:
        keys.add(('url', parts.netloc.casefold() + unquote(parts.path).rstrip('/')
                  + ('?' + parts.query if parts.query else '')))
    doi = str(metadata.get('doi') or '').removeprefix('https://doi.org/').casefold()
    if parts.netloc.casefold() in ('doi.org', 'dx.doi.org'):
        doi = unquote(parts.path).lstrip('/').casefold()
    if doi:
        keys.add(('doi', doi))
    if metadata.get('doc_id'):
        keys.add((source.get('source', 'input'), str(metadata['doc_id'])))
    return {key for key in keys if key[1]}


def _providers(request: str) -> list:
    from lazymind.chat.service.component.tool_policy import select_search_provider
    from lazymind.chat.service.component.tool_registry import DEFAULT_TOOLS

    config = next(item for item in DEFAULT_TOOLS if item.name == 'academic_search')
    _, definition = select_search_provider(config, request)
    providers = {provider.source_name: provider
                 for provider in [*definition['tools'], *config.tool['tools']] if provider.__key_source__()}
    return [type(provider)(timeout=httpx.Timeout(None, connect=15)) for provider in providers.values()]


def _arxiv_request(call, *args, **kwargs):
    path = Path(config['agentic_workspace']) / 'academic-search' / 'arxiv.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    # arXiv 要求请求串行，间隔至少三秒；同一工作目录的进程共用文件锁。
    with FileLock(path):
        sleep(3)
        return call(*args, **kwargs)


def _search(provider, query: str, count: int, plan: _SearchPlan) -> list[dict]:
    if provider.source_name == 'sciverse':
        if count > 10:
            items = provider.meta_search(
                query=query, page_size=count, include_content=False,
                year_from=plan.year_from, year_to=plan.year_to,
            )['items']
        else:
            items = provider.search(
                query, topk=count, include_content=False,
                search_type='meta' if plan.year_from or plan.year_to else 'agentic',
                year_from=plan.year_from, year_to=plan.year_to,
            )
    elif provider.source_name == 'arxiv':
        if plan.year_from or plan.year_to:
            start = plan.year_from or 1991
            end = plan.year_to or date.today().year
            query = f'({query}) AND submittedDate:[{start}01010000 TO {end}12312359]'
        items = _arxiv_request(provider.search, query, max_results=count)
    else:
        raise ValueError(f'文献综述尚未接入检索服务：{provider.source_name}')
    return [{**item, 'url': item.get('url', ''), 'text': item.get('snippet', ''),
             'source': provider.source_name, 'metadata': item.get('extra', {}),
             'kind': 'paper', 'reading': '检索预览'}
            for item in items if item.get('title', '').strip()]


def _search_results(providers: list, plan: _SearchPlan) -> Iterator[dict]:
    for query, count in plan.queries.items():
        while providers:
            provider = providers[0]
            try:
                batch = _search(provider, query, count, plan)
            except (httpx.HTTPError, KeyAuthError, json.JSONDecodeError) as error:
                providers.pop(0)
                next_source = providers[0].source_name if providers else '无可用服务'
                lazyllm.LOG.warning(f'{provider.source_name} 检索失败，后续使用 {next_source}：{type(error).__name__}')
            else:
                yield from batch
                break


def _content(provider, source: dict) -> dict:
    if source.get('kind') == 'knowledge_base':
        from lazymind.chat.engine.tools.lazy_kb import KBToolkit

        metadata = source['metadata']
        content = KBToolkit().read_document(metadata['kb_id'], metadata['doc_id'])['content']
        if not content['text'].strip():
            raise MissingMaterials(f'知识库文档《{source["title"]}》没有可用正文。')
        source.update(text=content['text'], reading={
            'content_type': 'knowledge_base_document', 'truncated': content['truncated'],
        })
        return source
    offset = 0
    parts = []
    readings = []
    while True:
        try:
            if provider.source_name == 'arxiv':
                result = _arxiv_request(provider.get_content, source, offset=offset)
            else:
                result = provider.get_content(source, offset=offset)
        except (httpx.HTTPError, KeyAuthError, json.JSONDecodeError) as error:
            lazyllm.LOG.warning(f'论文《{source["title"]}》取文失败，保留已取得内容：{type(error).__name__}')
            if readings:
                readings[-1]['truncated'] = True
            break
        reading = result['extra']['content_read']
        if reading.get('pagination_error') or (offset and reading.get('fallback')):
            lazyllm.LOG.warning(f'论文《{source["title"]}》在位置 {offset} 的续页读取失败，保留已取得内容。')
            if readings:
                readings[-1]['truncated'] = True
            break
        if not result['content'].strip():
            if readings:
                readings[-1]['truncated'] = True
            break
        parts.append(result['content'])
        readings.append(reading)
        if not reading.get('more'):
            break
        next_offset = reading['next_offset']
        if next_offset <= offset:
            raise ValueError('论文内容服务返回了无效的后续位置。')
        offset = next_offset
    if parts:
        source.update(text=''.join(parts), reading=readings)
    return source


def _retrieve(providers: list, plan: _SearchPlan, sources: list[dict],
              seen: dict, used_ids: set[str], context: dict, *, knowledge_base: bool = False) -> list[dict]:
    candidates = []
    readers = {provider.source_name: provider for provider in providers}

    def register(source):
        keys = _identity(source)
        previous = next((seen[key] for key in keys if key in seen), None)
        if previous is not None:
            if source['kind'] == 'knowledge_base' and source['text'] not in previous['text']:
                previous['text'] += '\n\n' + source['text']
            metadata = previous.setdefault('metadata', {})
            for key, value in source['metadata'].items():
                metadata.setdefault(key, value)
            seen.update((key, previous) for key in keys)
            return
        prefix = 'KB' if source['kind'] == 'knowledge_base' else 'SRC'
        index = 1
        while f'{prefix}-{index:03d}' in used_ids:
            index += 1
        source['id'] = f'{prefix}-{index:03d}'
        used_ids.add(source['id'])
        seen.update((key, source) for key in keys)
        candidates.append(source)

    if knowledge_base:
        from lazymind.chat.engine.tools.lazy_kb import KBToolkit

        toolkit = KBToolkit()
        for query, count in plan.queries.items():
            batch = toolkit.kb_search(query, k_max=count)['items']
            for item in batch:
                if item.get('text', '').strip():
                    register({**item, 'title': item.get('file_name') or item['docid'], 'url': '',
                              'text': item['text'], 'source': 'knowledge_base',
                              'metadata': {**item.get('metadata', {}),
                                           'kb_id': item['kb_id'], 'doc_id': item['docid'],
                                           'node_id': item['uid']},
                              'kind': 'knowledge_base', 'reading': '知识库节选'})
    else:
        for item in _search_results(providers, plan):
            register(item)
    if not candidates:
        return []
    by_id = {item['id']: item for item in candidates}
    selection_schema = create_model(
        'CandidateSelection', __base__=BaseModel,
        source_ids=(list[Literal[tuple(by_id)]], ...),
    )
    selection = WriterToolBase(llm=AutoModel(model='llm'))._call_llm_structured(
        _prompt(CANDIDATE_SELECTION_PROMPT,
            {'research': context,
             'paper_count': len(sources),
             'existing_titles': [item['title'] for item in sources],
             'candidates': candidates}),
        selection_schema,
    )
    selected = [by_id[source_id] for source_id in dict.fromkeys(selection.source_ids)]

    with ThreadPoolExecutor() as executor:
        content = executor.map(lambda source: _content(readers.get(source['source']), source), selected)
        return [source for source in content if source['text'].strip()]


def _render(inputs: dict, notes: list[dict], background: list[dict]) -> str:
    report_progress(85, '正在生成文献综述' if notes else '未获取到可读论文，正在生成主题概述')
    bindings = MaterialBindings(notes + background)
    from .runtime_events import generate_markdown
    return generate_markdown(AutoModel(model='llm'),
        _prompt(SYNTHESIS_PROMPT if notes else OVERVIEW_PROMPT,
            bindings.model_view({'research': {key: inputs.get(key) for key in ('topic', 'request', 'question')},
                                 'sources': notes, 'background': background})), bindings=bindings)


def run(inputs: dict) -> dict:
    inputs = validate_input('literature_review', inputs)
    sources = inputs['papers'] or []
    bindings = MaterialBindings(sources)
    context = {key: inputs.get(key) for key in ('topic', 'request', 'question')}
    context['knowledge_base_ids'] = ((lazyllm.globals.get('agentic_config') or {}).get('filters') or {}).get('kb_id', [])
    search_kb = bool(context['knowledge_base_ids'])
    context['search_source'] = 'knowledge_base' if search_kb else 'api'
    writer = WriterToolBase(llm=AutoModel(model='llm'))
    report_progress(10, '正在选择本轮材料并规划文献检索')
    plan = writer._call_llm_structured(
        _prompt(SEARCH_PLAN_PROMPT,
            bindings.model_view({'research': context,
             'papers': [{**{key: value for key, value in paper.items() if key not in ('text', 'abstract')},
                         'snippet': (paper.get('abstract') or paper.get('text') or '')[:1200]}
                        for paper in sources],
             'paper_count': len(sources), 'today': date.today().isoformat()})),
        _review_plan_schema(bindings),
    )
    sources, background = _select_materials(plan, bindings)
    report_progress(20, '正在整理已选材料的阅读笔记')
    notes = _notes(sources, context)
    background_notes = _notes(background, context) if background else []
    context['paper_limit'] = plan.paper_limit
    providers = None
    seen = {key: source for source in sources for key in _identity(source)}
    used_ids = {paper['id'] for paper in bindings.materials.values()}
    searched = set()
    for round_index in range(2):
        if not plan.queries or (not search_kb and not plan.allow_external_search):
            break
        if plan.year_from and plan.year_to and plan.year_from > plan.year_to:
            raise ValueError('模型生成的检索年份范围无效。')
        if not search_kb and providers is None:
            providers = _providers(inputs.get('request', ''))
        report_progress(25 + round_index * 30,
                        '正在检索并读取知识库文献' if search_kb else '正在检索并读取外部文献')
        added = _retrieve(providers if providers is not None else [], plan, sources, seen, used_ids,
                          context, knowledge_base=search_kb)
        sources.extend(added)
        report_progress(40 + round_index * 30, f'正在整理 {len(added)} 篇新增论文的阅读笔记')
        notes.extend(_notes(added, context))
        if not search_kb:
            searched.update(plan.queries)
        if round_index == 1 or providers == []:
            break
        search_kb = False
        context['search_source'] = 'api'
        report_progress(50, '正在评估是否需要补充检索')
        plan = writer._call_llm_structured(
            _prompt(COVERAGE_PROMPT,
                {'research': context, 'previous_evidence': inputs.get('evidence'), 'sources': notes,
                 'paper_count': len(sources), 'searched_queries': sorted(searched),
                 'today': date.today().isoformat()}),
            _SearchPlan,
        )
        plan.queries = {query: count for query, count in plan.queries.items() if query not in searched}
    if not sources and not plan.allow_external_search:
        raise MissingMaterials('用户指定的来源没有可用材料，请提供可阅读的论文或调整来源范围。')
    evidence = _render(inputs, notes, background_notes)
    return {'evidence': evidence, 'papers': sources, 'is_demo': False}
