"""Owns both drafting and revision; no pool, runtime or artifact APIs."""


def run(inputs: dict) -> dict:
    """Business research materials + mode draft/revise -> outline, manuscript, is_demo."""
    if inputs['mode'] == 'revise':
        body = inputs['manuscript'] + '\n\n## 本轮修订\n仅演示接收审稿意见，尚未执行真实修改。'
    else:
        body = f'# 论文写作 · {inputs["topic"]}\n\n## 摘要\n讨论证据可追溯性。\n\n## 方法\n比较证据组织方式。\n\n## 局限\n无真实实验结论。'
    return {'outline': '# 大纲\n## 摘要\n## 方法\n## 局限', 'manuscript': body, 'is_demo': True}
