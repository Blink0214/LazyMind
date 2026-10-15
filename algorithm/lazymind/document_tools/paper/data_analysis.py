"""Receives table values; returns prose and SVG bytes as text, never file paths."""


def run(inputs: dict) -> dict:
    """topic/request/data/experiment -> analysis, chart (SVG), is_demo."""
    return {
        'analysis': f'# 数据分析 · {inputs["topic"]}\n\n固定示例，未对用户数据执行统计计算。',
        'chart': '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="160"><text x="10" y="20">DEMO DATA</text><rect x="30" y="50" width="60" height="60" fill="#64748b"/><rect x="140" y="35" width="60" height="75" fill="#2563eb"/></svg>',
        'is_demo': True,
    }
