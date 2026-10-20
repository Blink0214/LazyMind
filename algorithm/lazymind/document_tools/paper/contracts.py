"""Business-only contracts. Storage, versions and workflow routing stay outside."""
import copy
import math
import xml.etree.ElementTree as ET
from .review_schema import SavedReview, text_digest

FIELDS = {
    'research_topic': ('papers', 'question', 'evidence'),
    'literature_review': ('papers', 'question', 'evidence'),
    'experiment': ('question', 'evidence', 'data'),
    'data_analysis': ('data', 'experiment'),
    'write_document': ('papers', 'question', 'evidence', 'analysis', 'chart', 'outline', 'manuscript',
                       'review', 'review_data'),
    'peer_review': ('manuscript', 'evidence', 'analysis'),
    'reading_reflection': ('papers', 'evidence'),
}
OUTPUTS = {
    'research_topic': ('question',), 'literature_review': ('evidence', 'papers'),
    'experiment': ('experiment',), 'data_analysis': ('analysis', 'chart'),
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
    elif kind == 'papers':
        if not isinstance(value, list):
            raise ValueError('papers 必须是论文列表')
        ids = set()
        for paper in value:
            if not isinstance(paper, dict) or set(paper) - {'id', 'title', 'text', 'url'}:
                raise ValueError('论文字段必须是 id/title/text/url')
            for key in ('id', 'title', 'text'):
                text(paper.get(key), f'paper.{key}')
            if 'url' in paper and not isinstance(paper['url'], str):
                raise ValueError('paper.url 必须是字符串')
            if paper['id'] in ids:
                raise ValueError('论文 id 重复')
            ids.add(paper['id'])
    elif kind == 'data':
        if not isinstance(value, dict) or set(value) != {'columns', 'rows'}:
            raise ValueError('data 必须包含 columns/rows')
        columns, rows = value['columns'], value['rows']
        if not isinstance(columns, list) or not columns or any(not isinstance(c, str) or not c.strip() for c in columns):
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
    elif kind == 'experiment':
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
            try:
                root = ET.fromstring(value)
            except ET.ParseError as exc:
                raise ValueError('chart 必须是有效 SVG 文本') from exc
            if root.tag not in ('svg', '{http://www.w3.org/2000/svg}svg'):
                raise ValueError('chart 必须是 SVG')


def validate_input(step, value):
    module = ALIASES.get(step, step)
    fields = FIELDS[module]
    if not isinstance(value, dict) or set(value) - {'topic', 'request', 'mode', *fields}:
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
            validate_value(field, item)
        result[field] = copy.deepcopy(item)
    required = {'refine_topic': ('evidence',), 'peer_review': ('manuscript',),
                'revise_document': ('manuscript', 'review'), 'reading_reflection': ('papers',)}.get(step, ())
    missing = [name for name in required if not result[name]]
    if step == 'data_analysis' and not result['data'] and not (
        result['experiment'] and result['experiment']['results']
    ):
        missing.append('数据或实验结果')
    if step == 'write_document' and not any(
        result[k] for k in ('papers', 'question', 'evidence', 'analysis', 'outline', 'manuscript')
    ):
        missing.append('研究材料、大纲或稿件')
    if missing:
        raise MissingMaterials('缺少材料: ' + '、'.join(missing))
    if step == 'revise_document' and result['review_data'] is not None:
        data = result['review_data']
        if (data['manuscript_sha256'] != text_digest(result['manuscript'])
                or data['review_sha256'] != text_digest(result['review'])):
            raise ValueError('稿件或审稿意见已修改，结构化意见已过期，请重新审稿后再修订')
    return result


def validate_output(module, value):
    if not isinstance(value, dict) or set(value) != {'is_demo', *OUTPUTS[module]}:
        raise ValueError(f'{module} 算法输出字段不符合约定')
    if type(value['is_demo']) is not bool:
        raise ValueError('输出 is_demo 必须是布尔值')
    for key in OUTPUTS[module]:
        validate_value(key, value[key])
    if module == 'peer_review' and value['review_data']['review_sha256'] != text_digest(value['review']):
        raise ValueError('结构化审稿意见与 Markdown 报告不匹配')
    return value
