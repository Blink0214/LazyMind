"""Generate one research question from the available F1 materials."""

import json
import re

from lazyllm import AutoModel, ThreadPoolExecutor

from lazymind.common.token_estimation import estimate_tokens

from .contracts import MissingMaterials
from .prompts.research_topic_prompt import (
    CONDENSED_PAPER_GUIDANCE,
    CONTENT_TOPICS,
    FULL_PAPER_GUIDANCE,
    NO_PAPER_GUIDANCE,
    PAPER_NOTE_PROMPT,
    QUESTION_PROMPT,
    TOPIC_EXTRACTION_PROMPT,
)


_SECTION_RE = re.compile(r'^##[ \t]+([^\n]+)$', re.MULTILINE)
_PAGE_RE = re.compile(r'<!-- page:(\d+) -->')
_PROMPT_TOKENS = 6000
_CHUNK_TOKENS = 2500
_NOTE_TOKENS = 400
_MAX_WORKERS = 4


def _topic_excerpt(text: str, token_budget: int) -> str:
    """Use parsed opening pages, preferring an abstract when one is present."""
    pages = re.split(r'(?m)^<!-- page:\d+ -->[ \t]*$', text, maxsplit=4)
    opening = '\n'.join([page.strip() for page in pages if page.strip()][:3])[:20000]
    abstract = re.search(r'(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:摘[ \t]*要|abstract)'
                         r'[ \t]*(?:[:：—-][ \t]*)?', opening)

    def bounded(value: str, budget: int) -> str:
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens(value[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        return value[:low].strip()

    if abstract:
        lead = bounded(opening[:abstract.start()], min(120, token_budget // 4))
        opening = f'{lead}\nAbstract:\n{opening[abstract.end():].strip()}'
    return bounded(opening, token_budget)


def infer_topic(request: str, papers: list[dict]) -> str:
    """Infer the F1 topic from the request and parsed paper openings."""
    excerpt_budget = max(1, 3000 // len(papers)) if papers else 0
    paper_clues = [{'id': paper['id'], 'opening': _topic_excerpt(paper['text'], excerpt_budget)}
                   for paper in papers]
    prompt = TOPIC_EXTRACTION_PROMPT + '\n' + json.dumps(
        {'request': request, 'paper_clues': paper_clues}, ensure_ascii=False,
    )
    if estimate_tokens(prompt) > _PROMPT_TOKENS:
        raise MissingMaterials('研究主题提取输入过长，请缩短要求或减少论文。')
    response = str(AutoModel(model='llm')(prompt) or '').strip()
    if not response:
        raise RuntimeError('研究主题提取模型返回空内容。')
    response = re.sub(r'^(?:<think\b[^>]*>.*?</think>\s*)+', '', response,
                      flags=re.IGNORECASE | re.DOTALL)
    try:
        result = json.loads(response)
    except json.JSONDecodeError as exc:
        raise RuntimeError('研究主题提取模型输出不是有效 JSON。') from exc
    if not isinstance(result, dict) or set(result) != {'topic'}:
        raise RuntimeError('研究主题提取模型输出格式错误。')
    topic = result['topic']
    if topic is None or (isinstance(topic, str) and not topic.strip()):
        raise MissingMaterials('无法确定研究主题，请明确说明研究对象或核心问题。')
    if not isinstance(topic, str):
        raise RuntimeError('研究主题提取模型输出格式错误。')
    return topic.strip()


def _paper_chunks(text: str) -> list[str]:
    """Split parsed text on page boundaries, then bound oversized pages."""
    markers = list(_PAGE_RE.finditer(text))
    pages = []
    if not markers:
        pages.append(('unknown', text))
    else:
        if text[:markers[0].start()].strip():
            pages.append(('unknown', text[:markers[0].start()]))
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            pages.append((marker.group(1), text[marker.end():end]))

    chunks = []
    current = ''
    for page, body in pages:
        body = body.strip()
        prefix = f'<!-- page:{page} -->\n'
        while body:
            low, high = 1, len(body)
            while low < high:
                middle = (low + high + 1) // 2
                if estimate_tokens(prefix + body[:middle]) <= _CHUNK_TOKENS:
                    low = middle
                else:
                    high = middle - 1
            length = low
            if length < len(body):
                line_end = body.rfind('\n', 0, length)
                if line_end >= length // 2:
                    length = line_end + 1
            piece, body = body[:length], body[length:]
            segment = prefix + piece.strip()
            if current and estimate_tokens(current + '\n\n' + segment) > _CHUNK_TOKENS:
                chunks.append(current)
                current = ''
            current = current + '\n\n' + segment if current else segment
            body = body.lstrip()
    if current:
        chunks.append(current)
    return chunks


def _condense(source: str, topic: str, request: str, paper: dict) -> str:
    prompt = PAPER_NOTE_PROMPT + '\n' + json.dumps(
        {'topic': topic, 'request': request, 'paper_id': paper['id'],
         'title': paper['title'], 'source': source}, ensure_ascii=False,
    )
    if estimate_tokens(prompt) > _PROMPT_TOKENS:
        raise ValueError('研究选题的论文片段或要求超出模型输入预算。')
    note = str(AutoModel(model='llm')(prompt) or '').strip()
    if not note or estimate_tokens(note) > _NOTE_TOKENS:
        raise ValueError(f'论文 {paper["id"]} 的分段提炼为空或超出长度预算。')
    return note


def _condensed_papers(papers: list[dict], topic: str, request: str) -> list[dict]:
    paper_chunks = [(paper, _paper_chunks(paper['text'])) for paper in papers]
    tasks = [(chunk, topic, request, paper) for paper, chunks in paper_chunks for chunk in chunks]
    if not tasks:
        raise ValueError('论文解析正文为空，无法提炼研究材料。')
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as executor:
        notes = list(executor.map(lambda task: _condense(*task), tasks))

    result = []
    offset = 0
    for paper, chunks in paper_chunks:
        count = len(chunks)
        paper_notes = notes[offset:offset + count]
        offset += count
        while len(paper_notes) > 1:
            groups = [paper_notes[index:index + 4] for index in range(0, len(paper_notes), 4)]
            with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(groups))) as executor:
                paper_notes = list(executor.map(
                    lambda group, current_paper=paper: _condense('\n\n'.join(group), topic, request, current_paper)
                    if len(group) > 1 else group[0], groups,
                ))
        result.append({**paper, 'text': 'Page-by-page notes covering the full paper, not the original text:\n'
                                          + paper_notes[0]})
    return result


def _validate_question_markdown(markdown: str, mode: str) -> None:
    if not markdown:
        raise ValueError('研究选题输出不能为空。')
    lines = markdown.splitlines()
    if any(lines[0].startswith(fence) and lines[-1].strip() == fence
           for fence in ('```', '~~~')):
        raise ValueError('研究选题输出不能是外层代码围栏。')
    headings = list(_SECTION_RE.finditer(markdown))
    required = ['研究问题']
    if mode == 'refine':
        required.append('文献反馈与调整依据')
    for section in required:
        matches = [(index, match) for index, match in enumerate(headings)
                   if match.group(1).strip() == section]
        if len(matches) != 1:
            raise ValueError(f'研究选题输出必须有且只有一个“{section}”二级标题。')
        index, match = matches[0]
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        if not markdown[match.end():end].strip():
            raise ValueError(f'研究选题输出的“{section}”章节不能为空。')


def run(inputs: dict) -> dict:
    """topic/request/papers/question/evidence/mode -> question, is_demo."""
    papers = inputs.get('papers', []) or []
    material = {key: inputs.get(key) for key in ('topic', 'request', 'papers', 'question', 'evidence', 'mode')}

    def make_prompt(condensed: bool = False) -> str:
        guidance = (CONDENSED_PAPER_GUIDANCE if condensed else FULL_PAPER_GUIDANCE) if papers else NO_PAPER_GUIDANCE
        return QUESTION_PROMPT.format(paper_guidance=guidance, topics='; '.join(CONTENT_TOPICS)) + (
            'Input materials (JSON):\n' + json.dumps(material, ensure_ascii=False)
        )

    prompt = make_prompt()
    if estimate_tokens(prompt) > _PROMPT_TOKENS:
        if not papers:
            raise ValueError('研究选题输入超出模型预算，请缩短用户要求或已有材料。')
        material['papers'] = _condensed_papers(papers, inputs['topic'], inputs.get('request', ''))
        prompt = make_prompt(condensed=True)
        if estimate_tokens(prompt) > _PROMPT_TOKENS:
            raise ValueError('论文提炼后仍超出研究选题输入预算，请减少论文或缩短其他材料。')
    markdown = str(AutoModel(model='llm')(prompt) or '').strip()
    markdown = re.sub(r'^(?:<think\b[^>]*>.*?</think>\s*)+', '', markdown,
                      flags=re.IGNORECASE | re.DOTALL)
    if re.match(r'^<think\b', markdown, flags=re.IGNORECASE):
        raise ValueError('研究选题输出格式错误：存在未闭合的思考内容。')
    _validate_question_markdown(markdown, inputs['mode'])
    return {'question': markdown, 'is_demo': False}
