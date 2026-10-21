"""Read-only academic review; storage and workflow routing belong to the caller."""
import json
import logging
import re
import time
from copy import deepcopy

from lazyllm import AutoModel
from lazyllm.tools.writer.tools.base import WriterToolBase
from pydantic import ValidationError

from .contracts import validate_input
from .prompts.peer_review_prompt import REVIEW_PROMPT, REPAIR_PROMPT
from .review_schema import RepairedIssue, ReviewIssue, ReviewRepair, ReviewResult, SavedReview, text_digest

SEVERITY_LABELS = {'major': '重要问题', 'minor': '一般问题', 'suggestion': '建议'}
CATEGORY_LABELS = {'method': '方法', 'evidence': '证据', 'analysis': '分析',
                   'structure': '结构', 'language': '语言', 'citation': '引用'}
ACTION_LABELS = {'text_revision': '可直接修改文字', 'needs_material': '需要作者补充材料',
                 'needs_experiment': '需要补充实验，不能用生成文字代替'}
LOG = logging.getLogger(__name__)
MAX_REPAIR_ISSUES = 5
MAX_REPAIR_CHARS = 24000


def review_scope(result: ReviewResult, inputs: dict) -> tuple[list[str], list[str]]:
    supplied = ['稿件']
    limitations = list(result.limitations)
    for key, label, missing in (
        ('evidence', '文献证据材料', '未提供文献证据材料，未核验外部引用的真实性及其对论断的支持。'),
        ('analysis', '分析材料', '未提供分析材料，未独立核验实验数值和统计结果。'),
    ):
        if inputs.get(key):
            supplied.append(label)
        else:
            limitations.append(missing)
    limitations.append('仅审查已提供的文本；未联网检索、查重或执行实验，不构成期刊录用判断。')
    return supplied, list(dict.fromkeys(limitations))


def render_review(result: ReviewResult, inputs: dict) -> str:
    """Render the structured result for the existing Markdown editor."""
    lines = [f'# 模拟审稿 · {inputs["topic"]}', '', '## 总体评价', result.summary, '', '## 逐条意见']
    priority = {severity: index for index, severity in enumerate(SEVERITY_LABELS)}
    issues = sorted(result.issues, key=lambda issue: priority[issue.severity])
    for index, issue in enumerate(issues, 1):
        lines.extend([
            '', f'### R{index}｜{SEVERITY_LABELS[issue.severity]}｜{CATEGORY_LABELS[issue.category]}',
            f'位置：{issue.location}', '',
        ])
        if issue.quote is not None:
            lines.extend(['原文：', *['> ' + line for line in issue.quote.splitlines()], ''])
        else:
            lines.extend(['原文：不适用（全文或缺失内容建议）', ''])
        lines.extend([f'问题与依据：{issue.problem}', '', f'修改建议：{issue.suggestion}', '',
                      f'处理方式：{ACTION_LABELS[issue.action_type]}'])
    if not issues:
        lines.extend(['', '本次检查未识别出明确问题；这不代表论文已达到发表标准。'])

    supplied, limitations = review_scope(result, inputs)
    lines.extend(['', '## 检查范围与限制', '本次使用：' + '、'.join(supplied) + '。'])
    if inputs.get('request'):
        lines.extend(['', '用户审查要求：', *['> ' + line for line in inputs['request'].splitlines()]])
    lines.extend(['', *['- ' + item for item in dict.fromkeys(limitations)]])
    return '\n'.join(lines) + '\n'


class ReviewQuoteError(ValueError):
    """The model's excerpt could not be located, not a missing upload."""


def locate_quotes(result: ReviewResult, manuscript: str) -> ReviewResult:
    result = result.model_copy(deep=True)
    invalid = []
    for index, issue in enumerate(result.issues, 1):
        if issue.quote is None or issue.quote in manuscript:
            continue
        # Only whitespace runs may differ. Never fuzzy-match changed words or numbers.
        pattern = r'\s+'.join(re.escape(part) for part in issue.quote.split())
        match = re.search(pattern, manuscript)
        if match:
            issue.quote = match.group(0)
        else:
            invalid.append(f'第 {index} 条（{issue.location}）：{issue.quote[:160]}')
    if invalid:
        raise ReviewQuoteError('论文已收到，但模型生成的以下摘录在正文中找不到：\n'
                               + '\n'.join(invalid) + '\n这是审稿摘录校验失败，不是缺少论文文件。')
    return result


def build_output(result: ReviewResult, inputs: dict) -> dict:
    """One model result supplies both formats, with matching issue IDs and source digests."""
    result = locate_quotes(result, inputs['manuscript'])
    review = render_review(result, inputs)
    priority = {severity: index for index, severity in enumerate(SEVERITY_LABELS)}
    issues = sorted(result.issues, key=lambda issue: priority[issue.severity])
    data = SavedReview.model_validate({
        **result.model_dump(),
        'limitations': review_scope(result, inputs)[1],
        'issues': [dict(issue.model_dump(), id=f'R{index}') for index, issue in enumerate(issues, 1)],
        'manuscript_sha256': text_digest(inputs['manuscript']), 'review_sha256': text_digest(review),
    })
    return {'review': review, 'review_data': data.model_dump(), 'is_demo': False}


def repair_targets(candidate: dict, manuscript: str) -> dict:
    """Collect invalid fields/issues without losing the valid part of the review."""
    if not isinstance(candidate, dict) or not isinstance(candidate.get('issues'), list):
        raise ValueError('审稿结果不是完整 JSON 对象或缺少意见列表，已停止，未自动重新审稿。')
    targets = {}
    try:
        ReviewResult.model_validate(candidate)
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False):
            loc = item['loc']
            path = f'issues.{loc[1]}' if loc[0] == 'issues' else loc[0]
            if path not in ('summary', 'limitations') and not path.startswith('issues.'):
                raise ValueError('审稿结果含未知顶层字段，已停止，未自动重新审稿。') from error
            value = candidate['issues'][loc[1]] if loc[0] == 'issues' else candidate.get(path)
            target = targets.setdefault(path, {'value': value, 'errors': []})
            target['errors'].append({'field': list(loc), 'message': item['msg']})
    for index, issue in enumerate(candidate['issues']):
        path = f'issues.{index}'
        if path in targets:
            continue
        try:
            locate_quotes(ReviewResult(summary='检查摘录', issues=[ReviewIssue.model_validate(issue)],
                                       limitations=[]), manuscript)
        except ReviewQuoteError as error:
            targets[path] = {'value': issue, 'errors': [str(error)]}
    if sum(path.startswith('issues.') for path in targets) > MAX_REPAIR_ISSUES:
        raise ValueError('审稿结果损坏范围过大，已停止修复；未扩写意见或自动重新审稿。')
    return targets


def apply_repair(candidate: dict, targets: dict, repair: ReviewRepair, manuscript: str) -> ReviewResult:
    if set(repair.replacements) != set(targets):
        raise ValueError('审稿局部修复必须且只能返回指定字段，不能改动有效意见。')
    merged = deepcopy(candidate)
    lines = manuscript.splitlines(keepends=True)
    for path, value in repair.replacements.items():
        if path.startswith('issues.'):
            issue = RepairedIssue.model_validate(value)
            value = issue.model_dump()
            if issue.quote is not None:
                start, end = issue.quote.start_line, issue.quote.end_line
                if not 1 <= start <= end <= len(lines):
                    raise ValueError('审稿修复的原文行号越界或倒置，不能发布意见。')
                value['quote'] = ''.join(lines[start - 1:end]).rstrip('\r\n')
            merged['issues'][int(path.split('.')[1])] = value
        else:
            merged[path] = value
    return ReviewResult.model_validate(merged)


def repair_prompt(inputs: dict, targets: dict) -> str:
    context = {key: value for key, value in inputs.items() if key != 'manuscript'}
    context['manuscript_lines'] = [{'line': index, 'text': line}
                                   for index, line in enumerate(inputs['manuscript'].splitlines(), 1)]
    prompt = REPAIR_PROMPT + '\n\nInput JSON:\n' + json.dumps({
        'inputs': context, 'targets': targets, 'issue_schema': RepairedIssue.model_json_schema(),
    }, ensure_ascii=False)
    if len(prompt) > MAX_REPAIR_CHARS:
        raise ValueError('审稿局部修复内容超过限制，已停止；请缩小审查范围后重试。')
    return prompt


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('审稿 JSON 含重复字段，不能确定应保留的内容。')
        result[key] = value
    return result


def parse_response(response):
    """Never use structural JSON guessing: one damaged issue must not become two."""
    if not isinstance(response, str):
        return response
    text = response.strip()
    if text.startswith('```') and text.endswith('```'):
        text = re.sub(r'^```(?:json)?\s*\n', '', text, count=1).removesuffix('```').strip()
    try:
        return json.loads(text, object_pairs_hook=_unique_fields)
    except json.JSONDecodeError:
        # Repair only a repeated field tag: "category":"analysis","category":"location":...
        # Tokenizing complete strings keeps quoted manuscript content untouched.
        tokens = list(re.finditer(r'"(?:\\.|[^"\\])*"|[{}\[\]:,]|[^\s{}\[\]:,"]+', text))
        values = [token.group() for token in tokens]
        fields = {json.dumps(key) for key in ReviewIssue.model_fields}
        removals = []
        for i in range(4, len(tokens) - 3):
            if (values[i] in fields and values[i] == values[i - 4]
                    and values[i - 3] == ':' and values[i - 2].startswith('"') and values[i - 1] == ','
                    and values[i + 1] == ':' and values[i + 2] in fields
                    and values[i + 2] != values[i] and values[i + 3] == ':'):
                removals.append((tokens[i].start(), tokens[i + 2].start()))
        for start, end in reversed(removals):
            text = text[:start] + text[end:]
        return json.loads(text, object_pairs_hook=_unique_fields)


def call_once(reviewer: WriterToolBase, prompt: str, schema):
    # Writer's convenience call retries the whole task; F6 owns one targeted repair instead.
    model = reviewer._build_structured_llm(reviewer._structured_output_prompt(schema), apply_formatter=False)
    started = time.monotonic()
    response = model(prompt)
    LOG.info('paper_review model_call schema=%s elapsed=%.3fs response_chars=%d',
             schema.__name__, time.monotonic() - started, len(response) if isinstance(response, str) else 0)
    try:
        return parse_response(response)
    except ValueError as error:
        raise ValueError('审稿返回无法解析的 JSON，已停止，未自动重新审稿。') from error


def run(inputs: dict) -> dict:
    """Business inputs -> Markdown review, structured review_data, is_demo."""
    inputs = validate_input('peer_review', inputs)
    reviewer = WriterToolBase(llm=AutoModel(model='llm'))
    prompt = REVIEW_PROMPT + '\n\nInput JSON:\n' + json.dumps(inputs, ensure_ascii=False)
    candidate = call_once(reviewer, prompt, ReviewResult)
    targets = repair_targets(candidate, inputs['manuscript'])
    LOG.info('paper_review issues=%d repair_targets=%d', len(candidate['issues']), len(targets))
    if targets:
        repair = ReviewRepair.model_validate(call_once(reviewer, repair_prompt(inputs, targets), ReviewRepair))
        result = apply_repair(candidate, targets, repair, inputs['manuscript'])
    else:
        result = ReviewResult.model_validate(candidate)
    return build_output(result, inputs)
