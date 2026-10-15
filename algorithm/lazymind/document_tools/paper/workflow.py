"""Paper Workflow state and Runtime adapter; algorithms use the separate business API."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ..artifacts import markdown_filename, persist_artifact_json
from . import contracts
from .api import run_algorithm
from .contracts import MissingMaterials

SCHEMA = 'paper-material-pool.v1'
ROUTES = {
    'F1': ['F2'], 'F2': ['F1', 'F3', 'F5', 'F7'], 'F3': ['F4'],
    'F4': ['F5'], 'F5': ['F6'], 'F6': ['F5'], 'F7': [],
}
STEP_NODE = {
    'research_topic': 'F1', 'literature_review': 'F2', 'experiment': 'F3', 'data_analysis': 'F4', 'write_document': 'F5',
    'peer_review': 'F6', 'reading_reflection': 'F7', 'refine_topic': 'F1', 'revise_document': 'F5',
}
STEP_LABELS = {
    'research_topic': '研究选题', 'literature_review': '文献综述', 'experiment': '实验研究', 'data_analysis': '数据分析',
    'write_document': '论文写作', 'peer_review': '模拟审稿', 'reading_reflection': '阅读心得',
    'refine_topic': '调整选题', 'revise_document': '论文修订', 'prepare': '材料准备', 'finish': '结束说明',
}
KINDS = {'papers', 'data', 'question', 'evidence', 'experiment', 'analysis',
         'chart', 'outline', 'manuscript', 'review', 'reflection'}


def validate_pool(pool: Any) -> dict:
    if not isinstance(pool, dict) or pool.get('schema') != SCHEMA:
        raise ValueError(f'材料池 schema 必须为 {SCHEMA}')
    if not isinstance(pool.get('topic'), str) or not pool['topic'].strip():
        raise ValueError('缺少研究主题 topic')
    if type(pool.get('sequence')) is not int or pool['sequence'] < 0:
        raise ValueError('sequence 必须是非负整数')
    if not isinstance(pool.get('materials'), list) or not isinstance(pool.get('current'), dict):
        raise ValueError('materials 必须是列表，current 必须是材料引用表')
    if not isinstance(pool.get('history'), list):
        raise ValueError('history 必须是列表')
    indexed = {}
    for item in pool['materials']:
        if not isinstance(item, dict) or item.get('kind') not in KINDS:
            raise ValueError('未知材料类型')
        if not isinstance(item.get('id'), str) or not item['id']:
            raise ValueError('材料缺少 id')
        if type(item.get('version')) is not int or item['version'] < 1:
            raise ValueError('材料版本必须为正整数')
        key = (item['id'], item['version'])
        if key in indexed:
            raise ValueError('材料 id/version 重复')
        if not isinstance(item.get('content'), (str, dict, list)) or (not item['content'] and item['kind'] != 'papers'):
            raise ValueError('材料 content 不能为空')
        if not isinstance(item.get('source_refs'), list) or not all(
            isinstance(ref, str) and ref for ref in item['source_refs']
        ):
            raise ValueError('source_refs 必须是来源标识列表')
        if not isinstance(item.get('derived_from'), list) or type(item.get('mock')) is not bool:
            raise ValueError('材料必须声明布尔 mock 和 derived_from')
        indexed[key] = item

    def resolve(ref):
        if not isinstance(ref, dict) or not isinstance(ref.get('id'), str) or type(ref.get('version')) is not int:
            raise ValueError('材料引用必须包含 id 和整数 version')
        key = (ref['id'], ref['version'])
        if key not in indexed:
            raise ValueError(f'材料引用不存在: {key}')
        return indexed[key]

    for kind, ref in pool['current'].items():
        if resolve(ref)['kind'] != kind:
            raise ValueError('材料引用类型不匹配')
    for item in pool['materials']:
        for ref in item['derived_from']:
            if resolve(ref) is item:
                raise ValueError('材料不能派生自自身')
    for execution in pool['history']:
        if not isinstance(execution, dict) or execution.get('node') not in ROUTES:
            raise ValueError('执行记录节点无效')
        if not all(isinstance(execution.get(key), list) for key in ('inputs', 'outputs')):
            raise ValueError('执行记录 inputs/outputs 必须是引用列表')
        for ref in execution.get('inputs', []) + execution.get('outputs', []):
            resolve(ref)
    if pool['sequence'] != len(pool['history']):
        raise ValueError('sequence 与执行历史长度不一致')
    papers = material(pool, 'papers')
    if papers and (not isinstance(papers['content'], list) or not all(
        isinstance(paper, dict) and isinstance(paper.get('id'), str) and paper['id']
        and isinstance(paper.get('text'), str) and paper['text'] for paper in papers['content']
    )):
        raise ValueError('papers 必须是含 id/text 的论文列表')
    return pool


def material(pool: dict, kind: str) -> dict | None:
    ref = pool['current'].get(kind)
    if ref is None:
        return None
    return next(item for item in pool['materials']
                if item['id'] == ref['id'] and item['version'] == ref['version'])


def add_material(pool: dict, kind: str, content: Any, derived: list[dict], sources: list[str], *, is_demo: bool = False) -> dict:
    identifier = pool['current'].get(kind, {}).get('id', f'material-{kind}')
    version = 1 + max((item['version'] for item in pool['materials'] if item['id'] == identifier), default=0)
    ref = {'id': identifier, 'version': version}
    pool['materials'].append({
        **ref, 'kind': kind, 'title': kind, 'mock': is_demo,
        'content': content, 'source_refs': sorted(set(sources)), 'derived_from': copy.deepcopy(derived),
    })
    pool['current'][kind] = ref
    return ref


def seed_pool() -> dict:
    pool = {'schema': SCHEMA, 'topic': '检索增强生成的证据可追溯性', 'sequence': 0,
            'materials': [], 'current': {}, 'history': []}
    # shortcut: small mock contents are inline; real adapters should use the
    # platform's authorized material references instead of embedding full papers.
    add_material(pool, 'papers', [
        {'id': '示例P1', 'title': '[示例] 检索证据组织方法', 'text': '示例论文讨论检索与引用对齐，不是真实论文。'},
        {'id': '示例P2', 'title': '[示例] 生成答案的来源检查', 'text': '示例论文讨论主张与来源核验，不是真实论文。'},
    ], [], ['示例P1', '示例P2'], is_demo=True)
    add_material(pool, 'data', {'columns': ['method', 'score'],
                               'rows': [['baseline', 60], ['evidence', 75]]}, [], [], is_demo=True)
    add_material(pool, 'manuscript', '# [示例] 已有论文\n\n## 方法\n待完善证据定位。\n\n## 局限\n仅为流程测试。', [], ['示例P1'], is_demo=True)
    return validate_pool(pool)


def business_input(step: str, pool: dict, request: str = '') -> dict:
    module = contracts.ALIASES.get(step, step)
    value = {'topic': pool['topic'], 'request': request or pool.get('request', '')}
    for kind in contracts.FIELDS[module]:
        item = material(pool, kind)
        if item:
            value[kind] = copy.deepcopy(item['content'])
    return value


def pool_from_business(value: dict) -> dict:
    """The entry adapter accepts business values, not a user-authored pool."""
    if not isinstance(value, dict):
        raise ValueError('入口材料必须是 JSON 对象')
    if value.get('schema') == SCHEMA:
        return validate_pool(copy.deepcopy(value))  # Existing pinned integrations.
    if set(value) - {'topic', 'request', 'demo', *KINDS}:
        raise ValueError('入口材料含未知字段')
    if type(value.get('demo', False)) is not bool:
        raise ValueError('demo 必须是布尔值')
    if value.get('demo'):
        if set(value) - {'topic', 'demo'}:
            raise ValueError('演示材料不能与真实材料混用')
        pool = seed_pool()
        if 'topic' in value:
            contracts.text(value['topic'], 'topic')
            pool['topic'] = value['topic']
        return pool
    contracts.text(value.get('topic'), 'topic')
    if not isinstance(value.get('request', ''), str):
        raise ValueError('request 必须是字符串')
    pool = {'schema': SCHEMA, 'topic': value['topic'], 'request': value.get('request', ''),
            'sequence': 0, 'materials': [], 'current': {}, 'history': []}
    paper_ids = []
    if 'papers' in value:
        contracts.validate_value('papers', value['papers'])
        paper_ids = [p['id'] for p in value['papers']]
    for kind in sorted(KINDS):
        if kind in value and value[kind] is not None:
            contracts.validate_value(kind, value[kind])
            add_material(pool, kind, value[kind], [], paper_ids)
    return validate_pool(pool)


def preparation_document(pool: dict) -> str:
    names = {'papers': '论文', 'data': '数据表', 'question': '研究选题', 'evidence': '文献综述',
             'experiment': '实验结果', 'analysis': '分析结论', 'chart': '图表',
             'outline': '大纲', 'manuscript': '稿件', 'review': '审稿意见', 'reflection': '阅读心得'}
    lines = ['# 材料准备', '', '## 研究主题', pool['topic'], '', '## 已接收材料']
    for kind in pool['current']:
        item = material(pool, kind)
        detail = f"（{len(item['content'])} 篇）" if kind == 'papers' else ''
        lines.append(f'- {names[kind]}{detail}')
    if not pool['current']:
        lines.append('尚未提供论文、数据或稿件；进入后续功能时将检查必要材料。')
    if any(material(pool, kind)['mock'] for kind in pool['current']):
        lines.extend(['', '> 演示数据，仅用于流程测试，不是真实研究结果。'])
    lines.extend(['', '请按目标选择下一步；无需依次完成所有阶段。'])
    return '\n'.join(lines)


def execute_step(step: str, pool: dict, request: str = '', fail: bool = False) -> dict:
    """Infrastructure owns lineage and atomic validation; algorithms own content."""
    validate_pool(pool)
    node = STEP_NODE[step]
    previous = pool['history'][-1]['node'] if pool['history'] else None
    if previous and node != previous and node not in ROUTES[previous]:
        raise ValueError(f'不允许的流程路径: {previous}→{node}')
    if fail:
        raise RuntimeError('[示例] 模拟执行失败；可重试。')
    module = contracts.ALIASES.get(step, step)
    relevant = contracts.FIELDS[module]
    inputs = [copy.deepcopy(pool['current'][kind]) for kind in relevant if kind in pool['current']]
    output = run_algorithm(step, business_input(step, pool, request))
    is_demo = output['is_demo'] or any(material(pool, k)['mock'] for k in relevant if material(pool, k))
    generated = {key: copy.deepcopy(output[key]) for key in contracts.OUTPUTS[module]}
    main = {'F1': 'question', 'F2': 'evidence', 'F3': 'experiment', 'F4': 'analysis',
            'F5': 'manuscript', 'F6': 'review', 'F7': 'reflection'}[node]
    document = generated[main]['report'] if main == 'experiment' else generated[main]
    notice = '> 演示数据，仅用于流程测试，不是真实研究结果。'
    if is_demo and notice not in document:
        first, sep, rest = document.partition('\n')
        document = first + '\n\n' + notice + '\n' + rest
    if main == 'experiment':
        generated[main]['report'] = document
    else:
        generated[main] = document
    sources = {source for kind in relevant if material(pool, kind)
               for source in material(pool, kind)['source_refs']}
    sources.update(p['id'] for p in generated.get('papers', []))
    result = copy.deepcopy(pool)
    outputs = [add_material(result, kind, content, inputs, sorted(sources), is_demo=is_demo)
               for kind, content in generated.items()]
    result['sequence'] += 1
    result['history'].append({'node': node, 'request': request, 'inputs': inputs, 'outputs': outputs})
    validate_pool(result)
    return {'status': 'completed', 'pool': result, 'document': document,
            'next': ROUTES[node] + ['END'], 'mock': is_demo}


def execute_mock(node: str, pool: dict, request: str = '', fail: bool = False) -> dict:
    """Legacy scenario-test helper; production dispatches by semantic step ID."""
    if node not in ROUTES:
        raise ValueError(f'未知节点: {node}')
    step = next(step for step, kind in STEP_NODE.items() if kind == node)
    if node == 'F1' and pool['history'] and pool['history'][-1]['node'] == 'F2':
        step = 'refine_topic'
    if node == 'F5' and pool['history'] and pool['history'][-1]['node'] == 'F6':
        step = 'revise_document'
    return execute_step(step, pool, request, fail)


def _unwrap(value: Any) -> Any:
    # Host-bound JSON/text artifact envelopes, never LLM-invented file paths.
    for _ in range(4):
        if (isinstance(value, dict) and 'topic' not in value
                and ('schema' not in value or 'schema_version' in value)):
            if 'data' in value:
                value = value['data']
                continue
            if 'text' in value:
                value = value['text']
                continue
        break
    return value


def _read_bound(path: str) -> Any:
    text = Path(path).read_text(encoding='utf-8')
    try:
        return _unwrap(json.loads(text))
    except json.JSONDecodeError:
        return text


def pool_from_inputs(inputs: dict[str, Any]) -> dict:
    candidates = [(key, validate_pool(value)) for key, value in inputs.items() if key.endswith('_pool')]
    if not candidates:
        raise MissingMaterials('没有绑定材料池，请先执行入口准备或补充材料。')
    key, selected = max(candidates, key=lambda item: (item[1]['sequence'], item[0].startswith('recovery_')))
    pool = copy.deepcopy(selected)
    document = inputs.get(key.removesuffix('_pool') + '_document')
    if document is not None:
        if not isinstance(document, str) or not document.strip():
            raise ValueError('已绑定的产物正文为空或格式错误')
        node = pool['history'][-1]['node'] if pool['history'] else None
        kind = {'F1': 'question', 'F2': 'evidence', 'F3': 'experiment', 'F4': 'analysis', 'F5': 'manuscript',
                'F6': 'review', 'F7': 'reflection'}.get(node)
        current = material(pool, kind) if kind else None
        if current:
            content = current['content']
            previous_document = content.get('report') if kind == 'experiment' and isinstance(content, dict) else content
            if previous_document != document:
                updated = {**content, 'report': document} if kind == 'experiment' and isinstance(content, dict) else document
                add_material(pool, kind, updated, [pool['current'][kind]], current['source_refs'], is_demo=current['mock'])
    return validate_pool(pool)


def _runtime_request(params: dict) -> tuple[str, str]:
    instruction = str(params.get('retry_hint') or params.get('runtime_instruction') or '').strip()
    # Core restores launch intent in user_input on rewind; the new request is in retry_hint.
    recovery = instruction.partition('Recovery request for this rerun only:')[2].strip()
    request = str(params.get('current_user_input') or recovery or params.get('user_input')
                  or params.get('launch_user_input') or '').strip()
    return request, instruction


def run_paper_step() -> dict:
    """Execute the authoritative current step and save its artifacts in one batch.

    No arguments: step identity and immutable input bindings come from Runtime.
    """
    from lazymind.chat.engine.subagent.context import require_context
    from lazymind.chat.engine.subagent.tools import save_artifacts

    context = require_context()
    step = context.params.get('step_id')
    bound = context.params.get('remote_inputs') or {}
    if not isinstance(bound, dict):
        raise ValueError('Runtime remote_inputs 格式错误')
    inputs = {key: _read_bound(path) for key, path in bound.items()}
    request, instruction = _runtime_request(context.params)
    if step == 'prepare':
        if 'paper_input' in inputs:
            pool = pool_from_business(inputs['paper_input'])
        elif '[paper:demo]' in request or '默认演示材料' in request:
            pool = seed_pool()
        else:
            raise MissingMaterials('请提供研究主题及已有论文、数据或稿件；演示测试请明确使用默认演示材料。')
        artifacts = [
            {'key': 'seed_pool', 'content_type': 'json', 'value': pool},
            {'key': 'prepare_document', 'content_type': 'text', 'value': preparation_document(pool)},
        ]
    elif step == 'finish':
        artifacts = [{'key': 'finish_document', 'content_type': 'text', 'value': '# 本次流程已结束\n已有产物保留，可按需继续或重新执行。'}]
    else:
        if step not in STEP_NODE:
            raise ValueError(f'未知 Workflow 步骤: {step}')
        pool = pool_from_inputs(inputs)
        fail = '[mock:fail]' in request + instruction and '[mock:retry]' not in request + instruction
        result = execute_step(step, pool, request, fail=fail)
        artifacts = [
            {'key': f'{step}_pool', 'content_type': 'json', 'value': result['pool']},
            {'key': f'{step}_document', 'content_type': 'text', 'value': result['document']},
        ]
        if step == 'data_analysis':
            chart = Path(context.workspace_path) / '分析图表.svg'
            chart.write_text(material(result['pool'], 'chart')['content'], encoding='utf-8')
            artifacts.append({'key': 'analysis_chart', 'content_type': 'file', 'value': str(chart)})
    # Publish actual named files; the existing document projection supplies the editor.
    for artifact in artifacts:
        if artifact['key'].endswith('_pool'):
            path = Path(context.workspace_path) / f"{artifact['key']}.json"
            persist_artifact_json(artifact['value'], str(path), schema_name=SCHEMA,
                                  created_by='paper-writing')
            artifact.update(content_type='file', value=str(path))
        elif artifact['key'].endswith('_document'):
            path = Path(context.workspace_path) / markdown_filename(STEP_LABELS[step])
            path.write_text(artifact['value'], encoding='utf-8')
            artifact.update(content_type='file', value=str(path))
    saved = save_artifacts(artifacts)
    return {'status': 'completed', 'step': step, 'saved': saved}
