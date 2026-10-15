"""P1 experiment adapter. Never label simulated measurements as executed."""


def run(inputs: dict) -> dict:
    """topic/request/question/evidence/data -> experiment, is_demo."""
    return {'experiment': {
        'executed': False, 'plan': '比较两种证据组织方式',
        'results': {'columns': ['method', 'score'], 'rows': [['baseline', 60], ['evidence', 75]]},
        'report': f'# 实验研究 · {inputs["topic"]}\n\n未执行实验；60、75 仅用于下游接线。',
    }, 'is_demo': True}
