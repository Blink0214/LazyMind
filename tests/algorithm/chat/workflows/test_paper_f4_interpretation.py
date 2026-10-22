"""Numeric model names must not be confused with unverified statistical claims."""
import pytest

from lazymind.document_tools.paper.analysis.interpretation import identifiers, validate
from lazymind.document_tools.paper.analysis.schema import normalize_input


def test_digit_leading_model_names_are_allowed_but_estimates_are_not():
    inputs = normalize_input({'topic': '7B 模型实验，由 F4 分析；不把 1e3 当作模型名称',
                              'data': {'columns': ['score'], 'rows': [[0.5]],
                                       'column_types': ['number'], 'units': [None]}})
    names = identifiers(inputs)
    assert '7B' in names and 'F4' in names
    assert '1e3' not in names
    analysis = {'targets': [{'id': 'mean'}], 'findings': [{'id': 'm', 'target_id': 'mean'}]}
    value = {'summary': 'F4 分析的是 7B 模型的模拟示例。',
             'answers': [{'target_id': 'mean', 'conclusion': '仅展示模拟结果。', 'finding_ids': ['m']}],
             'mechanism_explanation': '无法确定真实机制。', 'limitations': ['完整 7B 实验尚未执行。']}
    assert validate(value, analysis, names, 'zh-CN')['summary'] == value['summary']
    for claim in ['准确率为 0.8。', '共有 1e3 个样本。', '未知 70B 模型。']:
        value['summary'] = claim
        with pytest.raises(ValueError, match='summary 含未允许的数值'):
            validate(value, analysis, names, 'zh-CN')
