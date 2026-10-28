"""Compact checks for the current native-response and line-repair contracts."""
import pytest

from lazymind.document_tools.paper.plotting.code_response import (
    normalize_response, parse_code_response, repair_response,
)


def test_local_repair_applies_ranges_against_the_same_source():
    source = 'def analyze():\n    score = 1\n    cost = 2\n    return score, cost\n'
    patched = repair_response(dict(replacements=[
        dict(start_line=3, end_line=3, code='    cost = 4'),
        dict(start_line=2, end_line=2, code='    score = 3'),
    ]), source)
    namespace = {}
    exec(patched, namespace)
    assert namespace['analyze']() == (3, 4)
    assert patched.splitlines()[0] == source.splitlines()[0]
    assert patched.splitlines()[-1] == source.splitlines()[-1]


def test_overlapping_ranges_are_not_applied():
    with pytest.raises(ValueError, match='重叠'):
        repair_response(dict(replacements=[dict(start_line=1, end_line=2, code='pass'),
                                           dict(start_line=2, end_line=2, code='pass')]), 'a\nb\n')


def test_code_is_read_from_native_body_without_merging_reasoning():
    code = 'def draw(data, statistics, spec):\n    return figure'
    response = normalize_response(dict(content=f'```python\n{code}\n```',
                                       reasoning_content='Unfinished draft code'))
    assert parse_code_response(response.body)['code'] == code
    assert response.reasoning == 'Unfinished draft code'
