"""Receives the selected manuscript text, including user edits."""


def run(inputs: dict) -> dict:
    """topic/request/manuscript/evidence/analysis -> review, is_demo."""
    return {'review': f'# 模拟审稿 · {inputs["topic"]}\n\n## 修改建议\n明确数据的适用范围；在方法部分补充证据定位。', 'is_demo': True}
