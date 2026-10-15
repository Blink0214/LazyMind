"""Replace run only. Input/output examples live in tests/fixtures/paper-writing."""


def run(inputs: dict) -> dict:
    """topic/request/papers/question/evidence/mode -> question, is_demo."""
    body = f'# 研究选题 · {inputs["topic"]}\n\n## 候选问题\n如何提高结论的证据可追溯性？\n\n## 创新点\n建立主张与证据对应关系。'
    if inputs.get('evidence'):
        body += '\n\n## 文献反馈\n结合传入综述调整研究范围。'
    return {'question': body, 'is_demo': True}
