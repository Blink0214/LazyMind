"""Business-only contracts. Storage, versions and workflow routing stay outside."""
import copy
import math

from .analysis.validation import validate_charts, validate_svg

from .review_schema import SavedReview, text_digest

FIELDS = {
    'research_topic': ('papers', 'question', 'evidence'),
    'literature_review': ('papers', 'question', 'evidence'),
    'experiment': ('question', 'evidence', 'data'),
    'data_analysis': ('data', 'experiment', 'data_context', 'study_design'),
    'write_document': ('papers', 'question', 'evidence', 'analysis', 'chart', 'outline', 'manuscript',
                       'review', 'review_data'),
    'peer_review': ('manuscript', 'evidence', 'analysis'),
    'reading_reflection': ('papers', 'evidence'),
}
OUTPUTS = {
    'research_topic': ('question',), 'literature_review': ('evidence', 'papers'),
    'experiment': ('experiment',), 'data_analysis': ('analysis', 'charts'),
    'write_document': ('outline', 'manuscript'), 'peer_review': ('review', 'review_data'),
    'reading_reflection': ('reflection',),
}
ALIASES = {'refine_topic': 'research_topic', 'revise_document': 'write_document'}
MODES = {'research_topic': 'create', 'refine_topic': 'refine',
         'write_document': 'draft', 'revise_document': 'revise'}
KINDS = set().union(*FIELDS.values(), *OUTPUTS.values())


class MissingMaterials(ValueError):
    """The caller should ask for business materials, never a pool file."""


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} 必须是非空文本')


def validate_value(kind, value):
    if kind == 'review_data':
        SavedReview.model_validate(value)
        return
    if kind == 'analysis' and isinstance(value, dict):
        from .analysis.schema import validate_analysis
        validate_analysis(value)
        return
    if kind in ('data_context', 'study_design', 'constraints'):
        from .analysis.schema import DataContext, StudyDesign, Constraints, parse
        parse({'data_context': DataContext, 'study_design': StudyDesign, 'constraints': Constraints}[kind], value)
        return
    if kind == 'papers':
        if not isinstance(value, list):
            raise ValueError('papers 必须是论文列表')
        ids = set()
        for paper in value:
            if not isinstance(paper, dict) or set(paper) - {'id', 'title', 'abstract', 'text', 'url'}:
                raise ValueError('论文字段必须是 id/title/abstract/text/url')
            for key in ('id', 'title'):
                text(paper.get(key), f'paper.{key}')
            for key in ('abstract', 'text'):
                if paper.get(key) is not None and not isinstance(paper[key], str):
                    raise ValueError(f'paper.{key} 必须是字符串或 null')
            if not any((paper.get(key) or '').strip() for key in ('abstract', 'text')):
                raise MissingMaterials(f'论文 {paper["id"]} 缺少材料：需要摘要或全文')
            if 'url' in paper and not isinstance(paper['url'], str):
                raise ValueError('paper.url 必须是字符串')
            if paper['id'] in ids:
                raise ValueError('论文 id 重复')
            ids.add(paper['id'])
    elif kind == 'data':
        if isinstance(value, dict) and ('column_types' in value or 'tables' in value):
            from .analysis.schema import DataTable, DataBundle, parse
            parse(DataBundle if 'tables' in value else DataTable, value)
        else:
            # Other paper stages keep their original table contract; F4 validates its own spec.
            if not isinstance(value, dict) or set(value) != {'columns', 'rows'}:
                raise ValueError('data 必须包含 columns/rows')
            columns, rows = value['columns'], value['rows']
            if (not isinstance(columns, list) or not columns
                    or any(not isinstance(c, str) or not c.strip() for c in columns)):
                raise ValueError('columns 必须是非空列名列表')
            if len(set(columns)) != len(columns) or not isinstance(rows, list) or not rows:
                raise ValueError('列名不能重复，rows 必须是非空列表')
            for row in rows:
                if not isinstance(row, list) or len(row) != len(columns):
                    raise ValueError('数据行长度必须与列数相同')
                for cell in row:
                    if cell is not None and type(cell) not in (str, int, float, bool):
                        raise ValueError('单元格必须是 JSON 标量')
                    if isinstance(cell, float) and not math.isfinite(cell):
                        raise ValueError('数据不允许 NaN/Infinity')
    elif kind == 'charts':
        if value == []:
            return
        if isinstance(value, list) and value and 'finding_ids' in value[0]:
            from .analysis.schema import Chart, parse
            for chart in value:
                parse(Chart, chart)
                validate_svg(chart['svg'])
        else:
            validate_charts(value)
    elif kind == 'experiment':
        if isinstance(value, dict) and {'code', 'logs'} <= set(value):
            from .analysis.schema import Experiment, parse
            parse(Experiment, value)
            return
        if not isinstance(value, dict) or set(value) != {'executed', 'plan', 'results', 'report'}:
            raise ValueError('experiment 必须包含 executed/plan/results/report')
        if type(value['executed']) is not bool:
            raise ValueError('executed 必须是布尔值')
        text(value['plan'], 'plan')
        text(value['report'], 'report')
        if value['results'] is not None:
            validate_value('data', value['results'])
        if value['executed'] and value['results'] is None:
            raise ValueError('已执行实验必须提供结果')
    else:
        text(value, kind)
        if kind == 'chart':
            validate_svg(value)


def validate_input(step, value):
    if step == 'data_analysis':
        from .analysis.schema import normalize_input, ContractError
        try:
            return normalize_input(value)
        except ContractError as exc:
            if exc.code == 'MISSING_MATERIAL':
                raise MissingMaterials(str(exc)) from exc
            raise
    module = ALIASES.get(step, step)
    fields = FIELDS[module]
    allowed = {'topic', 'request', 'mode', *fields}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f'{step} 输入含未知字段')
    text(value.get('topic'), 'topic')
    if not isinstance(value.get('request', ''), str):
        raise ValueError('request 必须是字符串')
    result = {'topic': value['topic'], 'request': value.get('request', '')}
    if step in MODES:
        if value.get('mode', MODES[step]) != MODES[step]:
            raise ValueError(f'{step} mode 必须是 {MODES[step]}')
        result['mode'] = MODES[step]
    elif 'mode' in value:
        raise ValueError(f'{step} 不接受 mode')
    for field in fields:
        item = value.get(field, [] if field == 'papers' else None)
        if item is not None:
            if field == 'analysis' and module != 'data_analysis':
                text(item, field)  # Preserve the existing downstream input contract.
            else:
                validate_value(field, item)
        result[field] = copy.deepcopy(item)
    required = {'refine_topic': ('evidence',), 'peer_review': ('manuscript',),
                'revise_document': ('manuscript', 'review'), 'reading_reflection': ('papers',)}.get(step, ())
    missing = [name for name in required if not result[name]]
    if step == 'write_document':
        writing_materials = ('papers', 'question', 'evidence', 'analysis', 'outline', 'manuscript')
        if not any(result[k] for k in writing_materials):
            missing.append('研究材料、大纲或稿件')
    if missing:
        raise MissingMaterials('缺少材料: ' + '、'.join(missing))
    if step == 'revise_document' and result['review_data'] is not None:
        data = result['review_data']
        if (data['manuscript_sha256'] != text_digest(result['manuscript'])
                or data['review_sha256'] != text_digest(result['review'])):
            raise ValueError('稿件或审稿意见已修改，结构化意见已过期，请重新审稿后再修订')
    return result


def validate_output(module, value, inputs=None):
    if not isinstance(value, dict) or set(value) != {'is_demo', *OUTPUTS[module]}:
        raise ValueError(f'{module} 算法输出字段不符合约定')
    if type(value['is_demo']) is not bool:
        raise ValueError('输出 is_demo 必须是布尔值')
    if module == 'data_analysis':
        from .analysis.schema import AnalysisOutput, parse, validate_analysis
        from .analysis.schema import validate_charts as validate_structured_charts
        parse(AnalysisOutput, value, 'output')
        validate_analysis(value['analysis'], inputs)
        validate_structured_charts(value['charts'], value['analysis'])
        return value
    for key in OUTPUTS[module]:
        validate_value(key, value[key])
    if module == 'peer_review' and value['review_data']['review_sha256'] != text_digest(value['review']):
        raise ValueError('结构化审稿意见与 Markdown 报告不匹配')
    return value
