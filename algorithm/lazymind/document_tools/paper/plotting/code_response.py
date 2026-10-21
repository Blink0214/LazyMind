"""Apply model-authored local code edits without changing the execution validators."""
import copy
import json
import re


CODE_INSTRUCTION = '''Return Python source with actual physical newlines, preferably in ONE ```python``` block.
Do not JSON-encode the source or put literal \\n between statements. Do not wrap it in a Python string.
An optional figure caption may follow in ONE ```text``` block. No other explanation or reasoning text.
Existing JSON code/edits responses remain accepted for compatibility.
'''


def response_text(raw):
    """Separate completed leading reasoning envelopes from the final response.

    Never search for JSON/code inside reasoning or change tags inside the body.
    An unfinished reasoning envelope has no independently identifiable final response.
    """
    text, removed = str(raw or '').strip(), 0
    while True:
        opening = re.match(r'<(think|thinking)>', text, flags=re.IGNORECASE)
        if opening is None:
            return text, removed
        closing = re.search(r'</' + opening[1] + r'>', text[opening.end():], flags=re.IGNORECASE)
        if closing is None:
            raise ValueError('模型思考段未闭合，不能将其中的 JSON 或代码作为最终正文')
        text = text[opening.end() + closing.end():].lstrip()
        removed += 1


def parse_code_response(raw):
    """Read ordinary Python text without guessing how to repair escaping or source syntax."""
    if isinstance(raw, dict):
        return raw
    text, _ = response_text(raw)
    if not text:
        raise ValueError('模型未返回代码')
    if text.startswith('{'):
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError('代码响应必须为对象或 Python 文本')
        return result
    match = re.fullmatch(r'```(?:python|py)\s*\n([\s\S]*?)\n```'
                         r'(?:\s*```(?:text|caption)\s*\n([\s\S]*?)\n```)?', text)
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


REPAIR_INSTRUCTION = '''On repair, prefer JSON {"edits":[{"old":"exact unique source text","new":"replacement"}]}.
Change only the code responsible for the reported error. Edits are applied in order; each old string must occur
exactly once in the current source. Use valid JSON escaping for quotes, backslashes and newline characters;
the DECODED old/new values must match actual source text. Do not return a diff or line numbers.
If a local edit is insufficient, return complete Python source in a python block; the old JSON code response also works.
'''


def code_response(answer, previous='', metadata=()):
    """Return full code or a strictly anchored patch; ambiguous edits never modify the source."""
    if not isinstance(answer, dict) or set(answer) - {'code', 'edits', *metadata}:
        raise ValueError('代码响应只允许 code 或 edits 及声明的附加字段')
    if ('code' in answer) == ('edits' in answer):
        raise ValueError('代码响应必须且只能包含 code 或 edits 之一')
    if 'code' in answer:
        if not isinstance(answer['code'], str) or not answer['code'].strip():
            raise ValueError('code 必须为非空文本')
        return answer['code']
    edits = answer['edits']
    if not previous or not isinstance(edits, list) or not edits:
        raise ValueError('局部代码修复需要已有程序和非空 edits')
    result = previous
    for edit in copy.deepcopy(edits):
        if (not isinstance(edit, dict) or set(edit) != {'old', 'new'}
                or not isinstance(edit['old'], str) or not edit['old']
                or not isinstance(edit['new'], str)):
            raise ValueError('每项代码修复必须包含非空 old 和文本 new')
        if result.count(edit['old']) != 1:
            raise ValueError('代码修复 old 必须精确匹配当前程序中的唯一片段')
        result = result.replace(edit['old'], edit['new'], 1)
    return result
