"""Single/multiple paper reflection; no cross-workflow handoff."""


def run(inputs: dict) -> dict:
    """topic/request/papers/evidence -> reflection, is_demo."""
    return {'reflection': f'# 阅读心得 · {inputs["topic"]}\n\n## 阅读范围\n{len(inputs["papers"])} 篇论文。\n\n## 比较与启发\n写作时保留主张与来源对应关系。', 'is_demo': True}
