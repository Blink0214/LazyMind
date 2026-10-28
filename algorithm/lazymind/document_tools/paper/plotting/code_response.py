"""Normalize initial Python answers and apply repairs to numbered source lines."""
import ast
from dataclasses import dataclass
import io
import json
import re
import tokenize


PYTHON_RESPONSE = re.compile(
    r'```(?:python|py)\s*\n([\s\S]*?)\n```'
    r'(?:\s*```(?:text|caption)\s*\n([\s\S]*?)\n```)?')


CODE_INSTRUCTION = '''Return Python source with actual physical newlines, preferably in ONE ```python``` block.
Do not JSON-encode the source or put literal \\n between statements. Do not wrap it in a Python string.
An optional figure caption may follow in ONE ```text``` block. No other explanation or reasoning text.
Code may also be returned as a JSON code field. Return the complete initial script.
'''


@dataclass(frozen=True)
class ModelResponse:
    body: str | dict
    reasoning: str = ''
    removed_thinking_blocks: int = 0
    recovered_prefix_chars: int = 0
    inserted_boundary_spaces: int = 0


def json_body(text):
    text = text.strip()
    match = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    return match[1] if match else text


def _cross_boundary_json(reasoning, body):
    """Recover only a unique unfinished object spanning the channel boundary.

    Completed examples in reasoning, thought-only responses and incomplete answers
    never become final output. No characters, quotes or values are manufactured.
    """
    if not body.strip():
        return None
    candidates = []
    for opening in re.finditer(r'\{', reasoning):
        start = opening.start()
        fence = re.search(r'```(?:json)?\s*$', reasoning[:start])
        if fence:
            start = fence.start()
        prefix = reasoning[start:]
        try:
            json.loads(json_body(prefix))
            continue
        except ValueError:
            pass
        joined = prefix + body
        try:
            value = json.loads(json_body(joined))
        except ValueError:
            continue
        if isinstance(value, dict):
            candidates.append((start, joined, 0))
    return candidates[0] if len(candidates) == 1 else None


def _complete_code_body(text):
    """An existing formal answer takes precedence over any reasoning prefix."""
    text = text.strip()
    if not text:
        return False
    if PYTHON_RESPONSE.fullmatch(text):
        return True
    try:
        if isinstance(json.loads(json_body(text)), dict):
            return True
    except ValueError:
        pass
    try:
        return bool(ast.parse(text).body)
    except (SyntaxError, ValueError):
        return False


def _code_boundary_space(prefix, body):
    """Recover a lost token separator, without editing strings or indentation."""
    if not prefix or not body or not re.match(r'\w\Z', prefix[-1]) or not re.match(r'\w', body):
        return None
    joined = prefix + ' ' + body
    block = PYTHON_RESPONSE.fullmatch(joined.strip())
    if block is None:
        return None
    code = block[1]
    boundary = len(prefix) - block.start(1)
    offsets, offset = [], 0
    for line in code.splitlines(keepends=True):
        offsets.append(offset)
        offset += len(line)
    try:
        ast.parse(code)
        for token in tokenize.generate_tokens(io.StringIO(code).readline):
            if token.type not in (tokenize.STRING, tokenize.COMMENT):
                continue
            start = offsets[token.start[0] - 1] + token.start[1]
            end = offsets[token.end[0] - 1] + token.end[1]
            if start <= boundary < end:
                return None
    except (SyntaxError, ValueError, tokenize.TokenError):
        return None
    return joined


def _cross_boundary_code(reasoning, body):
    """Recover an unfinished answer block only when its exact continuation is valid.

    Never promote closed examples or replace a complete formal answer. If exact
    joining fails, one missing token-separator space may be recovered outside literals.
    """
    if not body.strip() or body.lstrip().startswith('```') or _complete_code_body(body):
        return None
    candidates = []
    for opening in re.finditer(r'```(?:python|py)[ \t]*\r?\n', reasoning):
        prefix = reasoning[opening.start():]
        if '```' in reasoning[opening.end():]:
            continue
        joined = prefix + body
        block = PYTHON_RESPONSE.fullmatch(joined.strip())
        if block is None or not block[1].strip():
            continue
        spaces = 0
        try:
            ast.parse(block[1])
        except (SyntaxError, ValueError):
            joined = _code_boundary_space(prefix, body)
            if joined is None:
                continue
            spaces = 1
        candidates.append((opening.start(), joined, spaces))
    return candidates[0] if len(candidates) == 1 else None


def normalize_response(raw, *, json_output=False):
    """Keep provider channels separate and normalize only leading envelopes."""
    if isinstance(raw, dict) and 'content' not in raw:
        return ModelResponse(raw)
    if isinstance(raw, dict):
        text = str(raw.get('content') or '')
        thoughts = [raw['reasoning_content']] if raw.get('reasoning_content') else []
    else:
        text, thoughts = str(raw or ''), []
    removed, recovered, spaces = 0, 0, 0
    # A provider's separate reasoning/content fields give an explicit boundary;
    # quoted tags within either field must not replace that boundary.
    recover = _cross_boundary_json if json_output else _cross_boundary_code
    if thoughts:
        boundary = recover(thoughts[-1], text)
        if boundary is not None:
            start, body, spaces = boundary
            recovered = len(thoughts[-1]) - start
            thoughts[-1] = thoughts[-1][:start]
            return ModelResponse(body.strip(), '\n'.join(filter(None, thoughts)), 0, recovered, spaces)
    while True:
        opening = re.match(r'\s*<(think|thinking)>', text, flags=re.IGNORECASE)
        if opening is None:
            break
        closings = list(re.finditer(r'</' + opening[1] + r'>', text[opening.end():], flags=re.IGNORECASE))
        if not closings:
            raise ValueError('模型思考段未闭合，不能将其中的 JSON 或代码作为最终正文')
        closing = closings[0]
        body = text[opening.end() + closing.end():]
        if json_output:
            try:
                json.loads(json_body(body))
                valid_body = True
            except ValueError:
                valid_body = False
        else:
            valid_body = _complete_code_body(body)
        candidates = []
        if not valid_body:
            for end in closings:
                thought = text[opening.end():opening.end() + end.start()]
                tail = text[opening.end() + end.end():]
                boundary = recover(thought, tail)
                if boundary is not None:
                    start, joined, boundary_spaces = boundary
                    candidates.append((joined, thought[:start], len(thought)-start, boundary_spaces))
        # Several closing tags can be literal string data. Ambiguous responses
        # retain their ordinary parse error rather than silently changing values.
        if len(candidates) == 1:
            text, thought, recovered, spaces = candidates[0]
            thoughts.append(thought)
            removed += 1
            break
        thoughts.append(text[opening.end():opening.end() + closing.start()])
        text = body
        removed += 1
    return ModelResponse(text.strip(), '\n'.join(filter(None, thoughts)), removed, recovered, spaces)


def parse_code_response(raw):
    """Read ordinary Python text without guessing how to repair escaping or source syntax."""
    if isinstance(raw, dict):
        return raw
    text = normalize_response(raw).body
    if not text:
        raise ValueError('模型未返回代码')
    if text.startswith('{'):
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError('代码响应必须为对象或 Python 文本')
        return result
    match = PYTHON_RESPONSE.fullmatch(text)
    if match:
        result = {'code': match[1]}
        if match[2] is not None:
            result['caption'] = match[2]
        return result
    if '```' in text:
        match = re.fullmatch(r'```json\s*\n([\s\S]*?)\n```', text)
        if match:
            return parse_code_response(match[1])
        raise ValueError('代码响应需要单个完整 Python 块和可选图注块')
    return {'code': text}


REPAIR_INSTRUCTION = '''Repair the supplied previous_code using the actual error and full numbered source.
Return ONE JSON object: {"replacements":[{"start_line":12,"end_line":13,"code":"    corrected_statement\\n"}]}.
Each inclusive, 1-based range refers to the SAME supplied source version. The "N | " prefixes are line numbers,
not source text. Return raw replacement code as JSON strings, preserving indentation; do not include line prefixes.
Choose the smallest complete ranges needed to fix the root cause, including related imports or helper calls when
necessary. A traceback may point inside an injected helper: fix the incorrect arguments in previous_code, not the
read-only helper. Do not blindly replace only the throwing line. Multiple ranges must not overlap; the host applies
them from bottom to top. To insert code, replace an adjacent source line with that line plus the insertion; use an
empty code string to delete a range. Preserve all other lines, scientific methods, input scope and calculations.
Do not return a complete script, an old/new text patch, a diff or explanation. A figure caption, only if changed,
may be included as a "caption" string in the same JSON object. Do not wrap Python or captions in separate blocks.
'''


def numbered_source(source):
    return '\n'.join(f'{number} | {line}' for number, line in enumerate(source.splitlines(), 1))


def repair_response(answer, previous):
    """Apply nonoverlapping inclusive ranges against one immutable source version."""
    replacements = answer.get('replacements') if isinstance(answer, dict) else None
    if not isinstance(replacements, list) or not replacements:
        raise ValueError('修复请返回非空 replacements 列表，每项含 start_line、end_line、code；不要返回完整脚本')
    lines = previous.splitlines(keepends=True)
    ranges = []
    for item in replacements:
        if not isinstance(item, dict):
            raise ValueError('每项 replacements 必须为对象，含 start_line、end_line、code')
        start, end, code = item.get('start_line'), item.get('end_line'), item.get('code')
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError(f'替换行号无效：{start!r}–{end!r}；当前源码共 {len(lines)} 行，使用 1 起始的闭区间')
        if not isinstance(code, str):
            raise ValueError(f'替换 {start}–{end} 行的 code 必须是字符串（删除时为空字符串）')
        ranges.append((start, end, code))
    ranges.sort(key=lambda item: item[0])
    for left, right in zip(ranges, ranges[1:]):
        if left[1] >= right[0]:
            raise ValueError(f'替换范围重叠：{left[0]}–{left[1]} 与 {right[0]}–{right[1]}；行号必须基于同一份源码')
    for start, end, code in reversed(ranges):
        # Keep adjacent untouched lines separate, including when a replacement omits its final newline.
        ending = '\r\n' if lines[end - 1].endswith('\r\n') else '\n'
        if code and not code.endswith(('\n', '\r')) and (end < len(lines) or lines[end - 1].endswith(('\n', '\r'))):
            code += ending
        lines[start - 1:end] = [code] if code else []
    return ''.join(lines)


def code_response(answer):
    """Read the complete initial source."""
    if not isinstance(answer, dict) or not isinstance(answer.get('code'), str) or not answer['code'].strip():
        raise ValueError('请返回完整 Python 脚本：单个 python 代码块或非空 code 字段')
    return answer['code']
