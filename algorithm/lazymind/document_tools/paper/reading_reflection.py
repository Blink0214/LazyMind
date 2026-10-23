"""Generate a Markdown reading reflection from one or more papers."""

import json
import re

from lazyllm import AutoModel, ThreadPoolExecutor

from lazymind.common.token_estimation import estimate_tokens

from .research_topic import _prompt_token_limit, group_texts_by_prompt_budget, split_text_by_prompt_budget
from .prompts.reading_reflection_prompt import (
    CONDENSED_PAPER_GUIDANCE,
    FULL_PAPER_GUIDANCE,
    MULTIPLE_PAPER_GUIDANCE,
    PAPER_NOTE_PROMPT,
    REFLECTION_PROMPT,
    SINGLE_PAPER_GUIDANCE,
)


_MAX_WORKERS = 4


def _paper_chunks(text: str, topic: str, request: str, paper: dict, prompt_limit: int) -> list[str]:
    return split_text_by_prompt_budget(
        text, lambda source: _note_prompt(source, topic, request, paper), prompt_limit,
        overflow_message='阅读心得的论文片段提示超出模型输入预算。',
    )


def _note_prompt(source: str, topic: str, request: str, paper: dict) -> str:
    return PAPER_NOTE_PROMPT + '\n' + json.dumps(
        {
            'topic': topic,
            'request': request,
            'paper_id': paper['id'],
            'title': paper['title'],
            'source_kind': 'full_text' if 'text' in paper else 'abstract',
            'source': source,
        },
        ensure_ascii=False,
    )


def _condense(source: str, topic: str, request: str, paper: dict, prompt_limit: int) -> str:
    prompt = _note_prompt(source, topic, request, paper)
    if estimate_tokens(prompt) > prompt_limit:
        raise ValueError('阅读心得的论文片段或要求超出模型输入预算。')
    note = str(AutoModel(model='llm')(prompt) or '').strip()
    note = re.sub(r'^(?:<think\b[^>]*>.*?</think>\s*)+', '', note,
                  flags=re.IGNORECASE | re.DOTALL)
    if not note or re.match(r'^<think\b', note, flags=re.IGNORECASE):
        raise ValueError(f'论文 {paper["id"]} 的阅读笔记为空或思考内容未闭合。')
    return note


def _condensed_papers(papers: list[dict], topic: str, request: str, prompt_limit: int) -> list[dict]:
    paper_chunks = [
        (paper, _paper_chunks(paper['text'] if 'text' in paper else paper['abstract'],
                              topic, request, paper, prompt_limit))
        for paper in papers
    ]
    tasks = [(chunk, topic, request, paper) for paper, chunks in paper_chunks for chunk in chunks]
    if not tasks:
        raise ValueError('论文解析正文为空，无法生成阅读心得。')
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as executor:
        notes = list(executor.map(lambda task: _condense(*task, prompt_limit), tasks))

    result = []
    offset = 0
    for paper, chunks in paper_chunks:
        count = len(chunks)
        paper_notes = notes[offset:offset + count]
        offset += count
        while len(paper_notes) > 1:
            groups = group_texts_by_prompt_budget(
                paper_notes,
                lambda source, current_paper=paper: _note_prompt(source, topic, request, current_paper), prompt_limit,
                overflow_message=f'论文 {paper["id"]} 的阅读笔记超出合并输入预算。',
            )
            if len(groups) == len(paper_notes):
                raise ValueError(f'论文 {paper["id"]} 的阅读笔记无法在输入预算内继续合并。')
            with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(groups))) as executor:
                paper_notes = list(executor.map(
                    lambda group, current_paper=paper: _condense(
                        '\n\n'.join(group), topic, request, current_paper, prompt_limit,
                    ) if len(group) > 1 else group[0],
                    groups,
                ))
        source_field = 'text' if 'text' in paper else 'abstract'
        source_kind = 'full-text' if source_field == 'text' else 'abstract-only'
        result.append({
            **paper,
            source_field: f'Reading notes derived from {source_kind} material, not the original text:\n'
                          + paper_notes[0],
        })
    return result


def _validate_reflection_markdown(markdown: str) -> None:
    if not markdown:
        raise ValueError('阅读心得输出不能为空。')
    lines = markdown.splitlines()
    if any(lines[0].startswith(fence) and lines[-1].strip() == fence for fence in ('```', '~~~')):
        raise ValueError('阅读心得输出不能是外层代码围栏。')
    if not markdown.lstrip().startswith('# '):
        raise ValueError('阅读心得输出必须以 Markdown 一级标题开始。')


def run(inputs: dict) -> dict:
    """topic/request/papers/evidence -> reflection, is_demo."""
    papers = []
    for paper in inputs['papers']:
        source_field = 'text' if (paper.get('text') or '').strip() else 'abstract'
        papers.append({'id': paper['id'], 'title': paper['title'], source_field: paper[source_field]})
    prompt_limit = _prompt_token_limit()
    material = {
        'topic': inputs['topic'],
        'request': inputs.get('request', ''),
        'papers': papers,
        'evidence': inputs.get('evidence'),
    }

    def make_prompt(condensed: bool = False) -> str:
        paper_guidance = CONDENSED_PAPER_GUIDANCE if condensed else FULL_PAPER_GUIDANCE
        reading_guidance = SINGLE_PAPER_GUIDANCE if len(papers) == 1 else MULTIPLE_PAPER_GUIDANCE
        return REFLECTION_PROMPT.format(
            paper_guidance=paper_guidance,
            reading_guidance=reading_guidance,
        ) + 'Input materials (JSON):\n' + json.dumps(material, ensure_ascii=False)

    prompt = make_prompt()
    if estimate_tokens(prompt) > prompt_limit:
        material['papers'] = _condensed_papers(papers, inputs['topic'], inputs.get('request', ''), prompt_limit)
        prompt = make_prompt(condensed=True)
        if estimate_tokens(prompt) > prompt_limit:
            raise ValueError(
                '论文提炼后仍超出阅读心得输入预算，请减少论文或缩短其他材料。',
            )
    markdown = str(AutoModel(model='llm')(prompt) or '').strip()
    markdown = re.sub(r'^(?:<think\b[^>]*>.*?</think>\s*)+', '', markdown,
                      flags=re.IGNORECASE | re.DOTALL)
    if re.match(r'^<think\b', markdown, flags=re.IGNORECASE):
        raise ValueError('阅读心得输出格式错误：存在未闭合的思考内容。')
    _validate_reflection_markdown(markdown)
    return {'reflection': markdown, 'is_demo': False}
