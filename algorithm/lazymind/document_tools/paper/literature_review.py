"""Owns literature synthesis/retrieval, not workflow files or material storage."""
import copy


def run(inputs: dict) -> dict:
    """topic/request/papers/question/evidence -> evidence, papers, is_demo."""
    citations = ' '.join(f'[{paper["id"]}]' for paper in inputs['papers'])
    body = f'# 文献综述 · {inputs["topic"]}\n\n## 文献综合\n比较证据组织和来源核验。\n\n## 分歧与缺口\n进一步比较证据覆盖率。\n\n## 引用\n{citations or "未提供论文；当前仅为结构演示。"}'
    return {'evidence': body, 'papers': copy.deepcopy(inputs['papers']), 'is_demo': True}
