"""Paper Workflow state and Runtime adapter; algorithms use the separate business API."""
from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import markdown_filename, persist_artifact_json
from . import contracts, research_topic
from .api import run_algorithm
from .contracts import MissingMaterials
from .entry import MAX_MANUSCRIPT_CHARS, read_manuscript
from .review_schema import text_digest
from .analysis import trace as diagnostics

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
KINDS = contracts.KINDS


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
        if (not isinstance(item.get('content'), (str, dict, list))
                or (not item['content'] and item['kind'] not in ('papers', 'charts'))):
            raise ValueError('材料 content 不能为空')
        if item['kind'] == 'analysis' and isinstance(item['content'], dict):
            from .analysis.schema import VERSION, validate_analysis
            if item.get('contract') != VERSION:
                raise ValueError('结构化分析缺少匹配的契约版本，旧结果需要原始材料重新分析')
            validate_analysis(item['content'])
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
    if papers:
        contracts.validate_value('papers', papers['content'])
    return pool


def material(pool: dict, kind: str) -> dict | None:
    ref = pool['current'].get(kind)
    if ref is None:
        return None
    return next(item for item in pool['materials']
                if item['id'] == ref['id'] and item['version'] == ref['version'])


def add_material(pool: dict, kind: str, content: Any, derived: list[dict], sources: list[str],
                 *, is_demo: bool = False) -> dict:
    identifier = pool['current'].get(kind, {}).get('id', f'material-{kind}')
    version = 1 + max((item['version'] for item in pool['materials'] if item['id'] == identifier), default=0)
    ref = {'id': identifier, 'version': version}
    pool['materials'].append({
        **ref, 'kind': kind, 'title': kind, 'mock': is_demo,
        'content': content, 'source_refs': sorted(set(sources)), 'derived_from': copy.deepcopy(derived),
    })
    if kind == 'analysis' and isinstance(content, dict):
        from .analysis.schema import VERSION
        pool['materials'][-1]['contract'] = VERSION
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
    if module == 'data_analysis' and 'constraints' in pool:
        value['constraints'] = copy.deepcopy(pool['constraints'])
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
    if set(value) - {'topic', 'request', 'demo', 'constraints', *KINDS}:
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
    if 'constraints' in value:
        from .analysis.schema import Constraints, parse
        pool['constraints'] = parse(Constraints, value['constraints'])
    paper_ids = []
    if 'papers' in value:
        contracts.validate_value('papers', value['papers'])
        paper_ids = [p['id'] for p in value['papers']]
    for kind in sorted(KINDS):
        if kind in value and value[kind] is not None:
            contracts.validate_value(kind, value[kind])
            add_material(pool, kind, value[kind], [], paper_ids)
    return validate_pool(pool)


def _conversation_papers(files_by_turn: dict | None) -> list[dict]:
    """Resolve paper attachments through the existing conversation text resolver."""
    paths = []
    for turn in sorted(files_by_turn or {}, key=int):
        for raw_path in files_by_turn[turn] or []:
            path = str(raw_path).split('?', 1)[0]
            if Path(path).suffix.lower() in {
                '.pdf', '.doc', '.docx', '.pptx', '.txt', '.md', '.markdown', '.tex', '.rst', '.html', '.htm',
            }:
                paths.append((int(turn), path))
    if not paths:
        return []

    from lazymind.chat.engine.tools.file_resources.ingest import ingest_pdf_file
    from lazymind.chat.engine.tools.file_resources.resolver import resolve_text_target
    from lazymind.chat.engine.tools.file_resources.store import FileResourceStore, workspace_for_request

    store = FileResourceStore(workspace_for_request())
    papers = []
    known_ids = set()
    for turn, path in paths:
        try:
            if Path(path).suffix.lower() == '.pdf':
                manifest = ingest_pdf_file(path, turn_seq=turn, store=store)
                if manifest.get('parse_status') != 'ready':
                    raise ValueError('PDF is not ready')
                file_id = manifest['file_id']
                if file_id in known_ids:
                    continue
                resolved = resolve_text_target(file_id, resources_only=True)
                if resolved.kind != 'file_resource' or resolved.file_id != file_id:
                    raise ValueError('not a parsed PDF')
            else:
                resolved = resolve_text_target(path, turn=turn, resources_only=True)
            text = Path(resolved.path).read_text(encoding='utf-8').strip()
        except (OSError, ValueError):
            raise MissingMaterials(f'论文附件 {Path(path).name} 无法读取或尚未解析完成，请检查后重试。') from None
        if not text:
            raise MissingMaterials(f'论文附件 {Path(path).name} 没有可用的解析正文，请重新上传。')
        if re.search(r'\[Attachment truncated after \d+ characters\.\]', text):
            raise MissingMaterials(f'论文附件 {Path(path).name} 的解析正文被截断，请提供完整材料。')
        file_id = resolved.file_id or f'upload-{text_digest(text)}'
        if file_id in known_ids:
            continue
        known_ids.add(file_id)
        papers.append({'id': file_id, 'title': Path(resolved.display_name).stem or file_id, 'text': text})
    return papers


def preparation_document(pool: dict) -> str:
    # Compatibility for already pinned legacy sessions; new workflows have no prepare node.
    names = {'papers': '论文', 'data': '数据表', 'question': '研究选题', 'evidence': '文献综述',
             'experiment': '实验结果', 'analysis': '分析结论', 'chart': '图表', 'charts': '图表列表',
             'outline': '大纲', 'manuscript': '稿件', 'review': '审稿意见', 'review_data': '结构化审稿意见',
             'reflection': '阅读心得'}
    names.update(data_context='数据说明', study_design='观测设计事实')
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
    if module == 'research_topic':
        contracts.validate_input(step, business_input(step, pool, request))
        pool = copy.deepcopy(pool)
        papers = material(pool, 'papers')
        if papers:
            prepared = research_topic.prepare_papers(papers['content'])
            if prepared != papers['content']:
                add_material(pool, 'papers', prepared, [pool['current']['papers']], papers['source_refs'],
                             is_demo=papers['mock'])
    relevant = contracts.FIELDS[module]
    inputs = [copy.deepcopy(pool['current'][kind]) for kind in relevant if kind in pool['current']]
    execution_records = []
    if module == 'data_analysis':
        from .analysis.pipeline import capture_execution
        with capture_execution() as execution_records:
            output = run_algorithm(step, business_input(step, pool, request))
    else:
        output = run_algorithm(step, business_input(step, pool, request))
    is_demo = output['is_demo'] or any(material(pool, k)['mock'] for k in relevant if material(pool, k))
    generated = {key: copy.deepcopy(output[key]) for key in contracts.OUTPUTS[module]}
    main = {'F1': 'question', 'F2': 'evidence', 'F3': 'experiment', 'F4': 'analysis',
            'F5': 'manuscript', 'F6': 'review', 'F7': 'reflection'}[node]
    structured_analysis = main == 'analysis' and isinstance(generated[main], dict)
    if structured_analysis:
        from .analysis.render import render
        document = render(generated[main], '数据分析 · ' + pool['topic'])
    else:
        document = generated[main]['report'] if main == 'experiment' else generated[main]
    notice = '> 演示数据，仅用于流程测试，不是真实研究结果。'
    if is_demo and notice not in document:
        first, sep, rest = document.partition('\n')
        document = first + '\n\n' + notice + '\n' + rest
    if main == 'experiment':
        generated[main]['report'] = document
    elif not structured_analysis:
        generated[main] = document
    if main == 'review':
        generated['review_data']['review_sha256'] = text_digest(document)
    sources = {source for kind in relevant if material(pool, kind)
               for source in material(pool, kind)['source_refs']}
    sources.update(p['id'] for p in generated.get('papers', []))
    result = copy.deepcopy(pool)
    outputs = [add_material(result, kind, content, inputs, sorted(sources), is_demo=is_demo)
               for kind, content in generated.items()]
    result['sequence'] += 1
    result['history'].append({'node': node, 'request': request, 'inputs': inputs, 'outputs': outputs})
    if execution_records:
        result['history'][-1]['analysis_execution'] = execution_records
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
            if kind == 'analysis' and isinstance(content, dict):
                from .analysis.render import render
                from .analysis.schema import ContractError
                previous_document = render(content, '数据分析 · ' + pool['topic'])
                if current['mock']:
                    first, _, rest = previous_document.partition('\n')
                    previous_document = first + '\n\n> 演示数据，仅用于流程测试，不是真实研究结果。\n' + rest
                if previous_document != document:
                    raise ContractError('STALE_MATERIAL', 'analysis',
                                        '分析报告已编辑；编辑稿保留，请将改动作为本轮要求重新分析，不能将修改后的数字视为已复算结果')
                return validate_pool(pool)
            previous_document = content.get('report') if kind == 'experiment' and isinstance(content, dict) else content
            if previous_document != document:
                updated = ({**content, 'report': document}
                           if kind == 'experiment' and isinstance(content, dict) else document)
                add_material(pool, kind, updated, [pool['current'][kind]], current['source_refs'],
                             is_demo=current['mock'])
    return validate_pool(pool)


def _business_request(value: Any) -> str:
    """UI retry commands control execution; they are not algorithm requirements."""
    text = str(value or '').strip()
    if text.rstrip('。.!！').lower() in {'重试', '重新执行', 'retry', 're-run', 'rerun', 'try again'}:
        return ''
    if re.fullmatch(r'(?:请重新执行步骤|Please re-run step)\s+[A-Za-z0-9_.:-]+', text, re.IGNORECASE):
        return ''
    return text


def _runtime_request(params: dict) -> tuple[str, str]:
    instruction = str(params.get('retry_hint') or params.get('runtime_instruction') or '').strip()
    # Core restores launch intent in user_input on rewind; the new request is in retry_hint.
    recovery = instruction.partition('Recovery request for this rerun only:')[2].strip()
    values = (params.get('current_user_input'), recovery, params.get('user_input'), params.get('launch_user_input'))
    request = next((text for value in values if (text := _business_request(value))), '')
    return request, instruction


def _step_request(step: str, pool: dict, params: dict) -> str:
    request, instruction = _runtime_request(params)
    recovery = instruction.partition('Recovery request for this rerun only:')[2].strip()
    if _business_request(params.get('current_user_input')) or _business_request(recovery):
        return request
    # Reuse the last saved requirements for this module before falling back to launch intent.
    previous = ''
    if params.get('current_user_input') or recovery:
        previous = next((_business_request(item.get('request')) for item in reversed(pool['history'])
                         if item['node'] == STEP_NODE[step] and _business_request(item.get('request'))), '')
    return previous or _business_request(pool.get('request')) or request


def initialize_pool(bound: dict[str, str], request: str = '') -> dict:
    """Resolve saved materials, or initialize business inputs before the first algorithm."""
    # A later step must use the latest confirmed artifacts, not re-read the original upload.
    saved = {key: _read_bound(path) for key, path in bound.items()
             if key.endswith(('_pool', '_document'))}
    if any(key.endswith('_pool') for key in saved):
        return pool_from_inputs(saved)
    value = _read_bound(bound['paper_input']) if 'paper_input' in bound else {}
    if not isinstance(value, dict):
        raise ValueError('程序化 paper_input 必须是业务对象；上传论文请绑定 manuscript_file')
    value = copy.deepcopy(value)
    for slot, field in (('manuscript_text', 'manuscript'), ('paper_topic', 'topic'), ('review_request', 'request')):
        if slot in bound:
            incoming = Path(bound[slot]).read_text(encoding='utf-8-sig')
            contracts.text(incoming, field)
            if field in value and value[field] != incoming:
                raise ValueError(f'{field} 存在冲突输入，请只提供一个版本')
            value[field] = incoming
    if 'manuscript_file' in bound:
        if 'manuscript' in value:
            raise ValueError('同时提供了论文文件和正文，请选择一个待审稿版本')
        value['manuscript'] = read_manuscript(bound['manuscript_file'])
    if not value and ('[paper:demo]' in request or '默认演示材料' in request):
        return seed_pool()
    if value.get('demo'):
        return pool_from_business(value)
    manuscript = value.get('manuscript')
    if manuscript is not None:
        contracts.text(manuscript, 'manuscript')
        if len(manuscript) > MAX_MANUSCRIPT_CHARS:
            raise ValueError('论文正文超过当前 20 万字符限制，请缩小审查范围')
    if not value.get('topic'):
        if manuscript:
            title = next(line.strip().lstrip('#').strip() for line in manuscript.splitlines() if line.strip())
            value['topic'] = title[:120] or '论文审稿'
        elif request.strip():
            value['topic'] = request.strip()[:120]
        else:
            raise MissingMaterials('请提供论文文件、正文或研究主题')
    value.setdefault('request', request)
    return pool_from_business(value)


@diagnostics.workflow_trace
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
    request, instruction = _runtime_request(context.params)
    if step == 'prepare':
        # Old sessions pin their graph; keep them runnable without exposing this node in new graphs.
        if not bound and '[paper:demo]' not in request and '默认演示材料' not in request:
            raise MissingMaterials('请提供论文文件、正文或研究主题')
        pool = initialize_pool(bound, request)
        artifacts = [
            {'key': 'seed_pool', 'content_type': 'json', 'value': pool},
            {'key': 'prepare_document', 'content_type': 'text', 'value': preparation_document(pool)},
        ]
    elif step == 'finish':
        artifacts = [{'key': 'finish_document', 'content_type': 'text', 'value': '# 本次流程已结束\n已有产物保留，可按需继续或重新执行。'}]
    else:
        if step not in STEP_NODE:
            raise ValueError(f'未知 Workflow 步骤: {step}')
        if step == 'data_analysis':
            from .analysis.attachments import bind_attachment, retry_request
            request = retry_request(context.params, request)
            with diagnostics.stage('input_binding'):
                bound = bind_attachment(bound, context.workspace_path, request)
            with diagnostics.stage('input_validation'):
                pool = initialize_pool(bound, request)
        else:
            pool = initialize_pool(bound, request)
        if step == 'literature_review' and not any(key.endswith('_pool') for key in bound):
            uploaded = _conversation_papers(context.params.get('history_files_per_turn'))
            current = material(pool, 'papers')
            papers = copy.deepcopy(current['content']) if current else []
            existing_ids = {paper['id'] for paper in papers}
            papers.extend(paper for paper in uploaded if paper['id'] not in existing_ids)
            if len(papers) > len(existing_ids):
                derived = [pool['current']['papers']] if current else []
                add_material(pool, 'papers', papers, derived, [paper['id'] for paper in papers])
        if step == 'research_topic' and 'paper_input' not in bound and not any(
            key.endswith('_pool') for key in bound
        ):
            papers = research_topic.prepare_papers(
                _conversation_papers(context.params.get('history_files_per_turn')),
            )
            if papers and any(item['mock'] for item in pool['materials']):
                # Real uploaded papers take precedence over a demo marker in the request.
                pool = pool_from_business({'topic': research_topic.infer_topic(request, papers),
                                           'request': request, 'papers': papers})
            elif not any(item['mock'] for item in pool['materials']):
                if papers:
                    add_material(pool, 'papers', papers, [], [paper['id'] for paper in papers])
                if 'paper_topic' not in bound:
                    pool['topic'] = research_topic.infer_topic(pool.get('request') or request, papers)
        request = _step_request(step, pool, context.params)
        fail = '[mock:fail]' in request + instruction and '[mock:retry]' not in request + instruction
        result = execute_step(step, pool, request, fail=fail)
        artifacts = [
            {'key': f'{step}_pool', 'content_type': 'json', 'value': result['pool']},
            {'key': f'{step}_document', 'content_type': 'text', 'value': result['document']},
        ]
        # Old pinned graphs still receive the structured result inside their existing pool artifact.
        if step == 'peer_review' and 'peer_review_data' in getattr(context, 'output_slots', []):
            path = Path(context.workspace_path) / '审稿意见.json'
            path.write_text(json.dumps(material(result['pool'], 'review_data')['content'],
                                       ensure_ascii=False, indent=2), encoding='utf-8')
            artifacts.append({'key': 'peer_review_data', 'content_type': 'file', 'value': str(path)})
        if step == 'data_analysis':
            execution = result['pool']['history'][-1].get('analysis_execution', [])
            figure_exports = {f['id']: f.get('exports', {}) for record in execution for f in record.get('figures', [])}
            for figure in material(result['pool'], 'charts')['content']:
                chart = Path(context.workspace_path) / f'分析图表-{figure["id"]}.svg'
                chart.write_text(figure['svg'], encoding='utf-8')
                artifacts.append({'key': 'analysis_chart', 'content_type': 'file', 'value': str(chart),
                                  'caption': figure.get('caption') or figure['title']})
                for extension, encoded in figure.get('exports', figure_exports.get(figure['id'], {})).items():
                    exported = chart.with_suffix('.' + extension)
                    exported.write_bytes(base64.b64decode(encoded, validate=True))
                    artifacts.append({'key': 'analysis_chart', 'content_type': 'file', 'value': str(exported),
                                      'caption': f'{figure.get("caption") or figure["title"]} ({extension.upper()})'})
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
    with diagnostics.stage('output_publication'):
        saved = save_artifacts(artifacts)
        diagnostics.save('published-files.json', [
            {'slot': item['key'], 'filename': Path(item['value']).name}
            for item in artifacts if item['content_type'] == 'file'
        ])
    return {
        'status': 'completed', 'step': step, 'saved': saved,
        # Stop the node with user-facing text, not a stringified publication receipt.
        '_agent_control': {'stop': True, 'final_text': '产物已保存，请在工作流中查看。'},
    }
