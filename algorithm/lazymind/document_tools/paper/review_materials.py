"""Prepare existing papers for F2 without external discovery."""
import copy
import json
import time
from typing import Annotated, Literal

from httpx import HTTPStatusError
from lazyllm import AutoModel
from lazyllm.tools.writer.tools.base import WriterToolBase
from pydantic import ConfigDict, Field, create_model

from .contracts import MissingMaterials
from .literature_review import _prompt
from .material_bindings import MaterialBindings
from .prompts.literature_review_prompt import MATERIAL_SELECTION_PROMPT
from .runtime_events import report_progress


def current_handoff() -> dict | None:
    from lazymind.chat.service.academic_handoff import academic_handoff_context

    try:
        context = academic_handoff_context()
    except HTTPStatusError as error:
        if error.response.status_code == 404:
            return None
        raise
    return context if context['kind'] == 'review' else None


def handoff_input(context: dict) -> dict:
    from lazymind.chat.service.academic_handoff import academic_read_paper

    value = context['input']
    papers = []
    for item in value['papers']:
        paper = {**copy.deepcopy(item['paper']),
                 **{key: copy.deepcopy(value) for key, value in item.items() if key != 'paper'}}
        result = academic_read_paper(paper_id=paper['paper_id'])
        while result['status'] == 'processing':
            report_progress(10, f'正在等待论文《{paper.get("title", paper["paper_id"])}》解析完成')
            time.sleep(2)
            result = academic_read_paper(paper_id=paper['paper_id'])
        parts, warnings = [], []
        while True:
            if result['status'] != 'ready' or result['content_level'] != 'fulltext':
                detail = '；'.join(result.get('warnings', [])) or result['status']
                raise MissingMaterials(f'论文《{paper.get("title", paper["paper_id"])}》正文尚不可读：{detail}')
            warnings.extend(result.get('warnings', []))
            parts.extend((f'<!-- page:{page["page"]} -->\n' if page['page'] > 0 else '') + page['text']
                         for page in result['pages'] if page['text'].strip())
            if not result.get('next_cursor'):
                break
            result = academic_read_paper(cursor=result['next_cursor'])
        if not parts:
            raise MissingMaterials(f'论文《{paper.get("title", paper["paper_id"])}》没有可用正文。')
        paper.update(result['paper'])
        paper.update(id=paper['paper_id'], text='\n\n'.join(parts), source_version=result['source_version'],
                     reading={'content_type': 'fulltext', 'content_hash': result['content_hash'],
                              'warnings': list(dict.fromkeys(warnings))})
        papers.append(paper)
    directions = {key: value[key] for key in ('candidate_directions', 'selected_directions')}
    request = value['user_instruction'] + '\n\nResearch directions (selected defines scope):\n'
    return {'topic': '；'.join(value['selected_directions']) or '论文综述',
            'request': request + json.dumps(directions, ensure_ascii=False), 'papers': papers}


def _select(papers: list[dict], inputs: dict, knowledge_bases: list[str]):
    bindings = MaterialBindings(papers)
    roles = create_model('ReviewMaterialRoles', __config__=ConfigDict(extra='forbid'),
                         **{key: (Literal['paper', 'background', 'exclude'], ...) for key in bindings.materials})
    schema = create_model('ReviewSelection', __config__=ConfigDict(extra='forbid'), material_roles=(roles, ...),
                          queries=(dict[str, Annotated[int, Field(ge=1)]], ...))
    selection = WriterToolBase(llm=AutoModel(model='llm'))._call_llm_structured(
        _prompt(MATERIAL_SELECTION_PROMPT, bindings.model_view({
            'research': {key: inputs.get(key) for key in ('topic', 'request', 'question')},
            'knowledge_bases': knowledge_bases,
            'papers': [{**{key: value for key, value in paper.items() if key not in ('text', 'abstract')},
                        'snippet': (paper.get('abstract') or paper.get('text') or '')[:1200]} for paper in papers],
        })), schema)
    selected = []
    for key, role in selection.material_roles.model_dump().items():
        if role != 'exclude':
            paper = bindings.materials[key]
            if role == 'background':
                paper['review_role'] = role
            else:
                paper.pop('review_role', None)
            selected.append(paper)
    return selected, selection.queries


def prepare(inputs: dict, knowledge_bases: list[str], *, fixed_scope: bool = False) -> list[dict]:
    papers = copy.deepcopy(inputs.get('papers') or [])
    if fixed_scope or not (papers or knowledge_bases):
        return papers
    report_progress(10, '正在选择本轮综述材料')
    selected, queries = _select(papers, inputs, knowledge_bases)
    if knowledge_bases and queries:
        from lazymind.chat.engine.tools.lazy_kb import KBToolkit

        report_progress(12, '正在检索指定知识库')
        toolkit = KBToolkit()
        candidates = {}
        existing = {paper['id'] for paper in selected}
        for query, count in queries.items():
            for item in toolkit.kb_search(query, k_max=count, kb_ids=knowledge_bases)['items']:
                identifier = f'kb-{item["kb_id"]}-{item["docid"]}'
                if identifier in existing or not item.get('text', '').strip():
                    continue
                if identifier in candidates:
                    candidates[identifier]['text'] += '\n\n' + item['text']
                else:
                    candidates[identifier] = {
                        'id': identifier, 'title': item.get('file_name') or item['docid'], 'text': item['text'],
                        'source': 'knowledge_base', 'kind': 'knowledge_base',
                        'metadata': {**item.get('metadata', {}), 'kb_id': item['kb_id'], 'doc_id': item['docid']},
                    }
        if candidates:
            selected, _ = _select([*selected, *candidates.values()], inputs, [])
            report_progress(15, '正在读取已选知识库文档')
            for paper in selected:
                if paper['id'] not in candidates:
                    continue
                metadata = paper['metadata']
                content = toolkit.read_document(metadata['kb_id'], metadata['doc_id'])['content']
                if not content['text'].strip():
                    raise MissingMaterials(f'知识库文档《{paper["title"]}》没有可用正文。')
                paper.update(text=content['text'], reading={
                    'content_type': 'knowledge_base_document', 'truncated': content['truncated'],
                })
    if not any(paper.get('review_role') != 'background' for paper in selected):
        raise MissingMaterials('指定范围内没有可综述的论文，请补充论文或调整知识库内的选文范围。')
    return selected
