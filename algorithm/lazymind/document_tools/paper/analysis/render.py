"""Deterministic rendering preserves probability, interval and sample-unit semantics."""
from .schema import validate_analysis
from .presentation import complete_results, provenance_markdown


def render(analysis, title='数据分析', plan=None):
    analysis = validate_analysis(analysis)
    summary = analysis['summary'].replace('](#complete-results)', '](#block-complete-results)')
    lines = [f'# {title}', '', summary, '', '## 方法',
             *['- ' + m for m in analysis['methods']], '', complete_results(analysis, plan)]
    if analysis['models']:
        lines += ['', '## 模型与先验', '']
        for model in analysis['models']:
            lines += [f'- {model["id"]}：{model["method"]}；{model["formula"] or ""}',
                      f'  似然：{model["likelihood"] or "不适用"}']
            for prior in model['priors']:
                lines.append(f'  先验：{prior["parameter"]} ~ {prior["distribution"]} {prior["parameters"]}；'
                             f'来源={prior["source"]}；{prior["rationale"]}')
    lines += ['', '## 局限', *['- ' + text for text in analysis['limitations']], '',
              f'原始表共 {analysis["row_count"]} 行；此数不等于独立样本量。',
              '', provenance_markdown(analysis, plan)]
    return '\n'.join(lines)
