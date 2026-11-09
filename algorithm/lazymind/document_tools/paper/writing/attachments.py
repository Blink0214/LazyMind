"""Initialize writing materials from the selected conversation attachments."""
from __future__ import annotations

import copy
import html
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit


from ..contracts import MissingMaterials
from ..publication import prose, split_references

DOCUMENTS = {'.pdf', '.doc', '.docx', '.txt', '.md', '.markdown', '.tex', '.rst', '.html', '.htm'}
SUPPORTED = DOCUMENTS | {'.json', '.png', '.jpg', '.jpeg', '.svg'}


def select_files(config):
    """Use the latest upload batch, never silently merge historical versions."""
    history = config.get('history_files_per_turn') or {}
    groups = [history[key] for key in sorted(history, key=lambda key: int(key), reverse=True)]
    groups.append(config.get('files') or [])
    for group in groups:
        files = list(dict.fromkeys(str(path) for path in group or []))
        if files:
            return [path for path in files if Path(urlsplit(path).path).suffix.lower() in SUPPORTED]
    return []


def import_files(files, request):
    from lazymind.chat.engine.tools.file_resources.resolver import resolve_text_target
    from lazymind.document_tools.writing import collect_document_media
    from lazymind.chat.service.utils.static_file_url import _upload_root
    from ...artifacts import validate_paper_svg

    documents, images, svg_resources = [], [], []
    for path in files:
        suffix = Path(urlsplit(path).path).suffix.lower()
        if suffix not in SUPPORTED or suffix == '.json':
            raise MissingMaterials(f'不支持作为写作附件导入的格式：{suffix}')
        if suffix in DOCUMENTS:
            resolved = resolve_text_target(path, resources_only=True)
            text = Path(resolved.path).read_text(encoding='utf-8-sig').strip()
            if not text or re.search(r'\[Attachment truncated after \d+ characters\.\]', text):
                raise MissingMaterials('附件为空或解析被截断，请提供完整可解析的文件。')
            documents.append({'filename': Path(urlsplit(path).path).name, 'text': text})
        else:
            if suffix == '.svg':
                from lazymind.chat.engine.tools.host_file_resolution import open_input_file
                with open_input_file(path) as stream:
                    raw = stream.read(20 * 1024 * 1024 + 1)
                if len(raw) > 20 * 1024 * 1024:
                    raise ValueError('SVG exceeds 20 MB')
                validate_paper_svg(raw.decode('utf-8-sig'))
                root = ET.fromstring(raw)
                labels = [''.join(node.itertext()).strip() for node in root.iter()
                          if node.tag.rsplit('}', 1)[-1] in {'title', 'desc', 'text'}]
                label_text = '; '.join(filter(None, labels))[:12000] or 'No text labels; meaning unspecified.'
                svg_resources.append({'resource_type': 'image', 'uri': path,
                                      'title': Path(urlsplit(path).path).name, 'mime_type': 'image/svg+xml',
                                      'summary': 'Uploaded SVG. Source text labels (not visual interpretation): '
                                                 + label_text,
                                      'meta': {'summary_source': 'svg_text'}})
            images.append(path)
    value = {'topic': next((item['text'].splitlines()[0].lstrip('# ').strip()[:120]
                            for item in documents), request[:120]), 'request': request}
    for item in documents:
        append_note(value, item['filename'], item['text'])
    if images:
        task = {'task_id': 'paper-upload', 'task_type': 'write', 'query': request,
                'constraints': {'visual_policy': {'allow_web_search': False, 'allow_image_generation': False,
                                                  'require_input_image_reuse': True}}}
        batch_id = hashlib.sha256(json.dumps(images, ensure_ascii=False).encode()).hexdigest()[:20]
        collected = collect_document_media(
            task,
            file_paths=[path for path in images if Path(urlsplit(path).path).suffix.lower() != '.svg'],
            input_resources=svg_resources,
            media_store=str(Path(_upload_root()) / 'paper-media' / batch_id))
        library = collected['media_assets']
        if not library.get('assets') or collected.get('warnings'):
            raise MissingMaterials('图片导入未完整成功：' + '; '.join(collected.get('warnings') or []))
        assets = {}
        for asset in library['assets'].values():
            local = Path(asset['local_path'])
            raw = local.read_bytes()
            if local.suffix.lower() == '.svg':
                validate_paper_svg(raw.decode('utf-8-sig'))
            identifier = 'image-' + hashlib.sha256(raw).hexdigest()[:20]
            original = Path(urlsplit(asset.get('uri') or '').path).name
            asset.update(media_asset_id=identifier,
                         uri='/static-files/' + local.relative_to(Path(_upload_root())).as_posix())
            asset['caption'] = asset.get('caption') or original or identifier
            asset.setdefault('meta', {}).update(source_filename=original, sha256=hashlib.sha256(raw).hexdigest())
            assets[identifier] = asset
        library.update(assets=assets, visual_need_asset_ids={})
        library.setdefault('meta', {})['required_input_asset_ids'] = list(assets)
        value['media_assets'] = library
    return value


def bind_attachment(bound: dict, workspace: str, request: str) -> dict:
    """F5 import only; current-session pools remain authoritative."""
    if any(key.endswith('_pool') for key in bound):
        return bound
    import tempfile
    import lazyllm
    from ..analysis.attachments import (_bound_reference, _select_attachment, _unique_fields,
                                        _invalid_constant, MAX_SOURCE_BYTES)
    from ..workflow import _unwrap

    config = lazyllm.globals.get('agentic_config') or {}
    explicit = 'paper_input' in bound
    reference = _bound_reference(bound['paper_input']) if explicit else None
    if explicit and reference is None:
        # Runtime has already supplied a local business/pool JSON artifact.
        return bound
    files = select_files(config)
    if 'outline_file' in bound:
        # The explicitly bound outline is read once by initialize_pool.
        path = Path(bound['outline_file'])
        name = path.read_text(encoding='utf-8-sig').strip().strip('"') if path.is_file() else ''
        files = [f for f in files if f != str(path) and f != name
                 and Path(urlsplit(f).path).name != name]
    candidates = [p for p in files if Path(urlsplit(p).path).suffix.lower() == '.json']
    if reference is not None:
        source = _select_attachment(config, request, reference=reference)
    else:
        named = [p for p in candidates if Path(urlsplit(p).path).name in request]
        candidates = named or candidates
        if len(candidates) > 1:
            raise MissingMaterials('本轮有多份 JSON 材料，请明确使用哪个文件')
        source = candidates[0] if candidates else None
    if source:
        from lazymind.chat.engine.tools.file_resources.resolver import resolve_text_target
        from lazymind.chat.engine.tools.host_file_resolution import open_input_file
        resolved = resolve_text_target(source, resources_only=True)
        if resolved.kind != 'attachment_text':
            raise MissingMaterials('JSON 必须来自当前会话的实际附件')
        with open_input_file(resolved.path) as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError('JSON 附件超过 100 MiB')
        value = _unwrap(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_fields,
                                   parse_constant=_invalid_constant))
        others = [p for p in files if Path(urlsplit(p).path).suffix.lower() != '.json']
        if others:
            value = merge_upload(initialize_upload(value, request), import_files(others, request))
    else:
        if explicit:
            return bound
        if any(key in bound for key in ('manuscript_file', 'manuscript_text')):
            # Existing-manuscript routing can bind the manuscript separately; still
            # import the report/images uploaded alongside it.
            if 'manuscript_file' in bound:
                path = Path(bound['manuscript_file'])
                if not path.is_file():
                    return bound
                name = path.read_text(encoding='utf-8-sig').strip().strip('"')
                files = [f for f in files if f != str(path) and f != name
                         and Path(urlsplit(f).path).name != name]
            if not files:
                return bound
        if not files:
            return bound
        value = import_files(files, request)
    if 'paper_topic' in bound:
        value['topic'] = Path(bound['paper_topic']).read_text(encoding='utf-8-sig')
    directory = Path(tempfile.mkdtemp(prefix='paper-input-', dir=workspace))
    target = directory / 'paper_input.json'
    target.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    return {**bound, 'paper_input': str(target)}


def reference_catalog(papers=(), references=()):
    """Merge identities, never infer bibliography from prose or overwrite conflicts."""
    catalog = {}
    for item in [*(papers or []), *(references or [])]:
        identifier, citation = item['id'], item.get('citation')
        if not citation:
            continue
        if identifier in catalog and catalog[identifier] != citation:
            raise ValueError(f'文献 {identifier} 的书目冲突')
        catalog[identifier] = citation
    return [{'id': key, 'citation': value} for key, value in catalog.items()]


def validate_references(items):
    if not isinstance(items, list):
        raise ValueError('references 必须是列表')
    ids = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'id', 'citation'}
                or not isinstance(item['id'], str) or not re.fullmatch(r'[\w.-]+', item['id'])
                or item['id'].isdigit()):
            raise ValueError('references 必须包含明确的来源 id 和 citation，不支持猜配数字编号')
        citation = item['citation']
        if (not isinstance(citation, str) or not citation.strip() or '\n' in citation or '\r' in citation
                or '[文献:' in citation or 'paper-ref-' in citation or re.match(r'^\s*\[\d+\]', citation)):
            raise ValueError('citation 必须是无编号、无内部引用标记的单行书目')
        if item['id'] in ids:
            raise ValueError('references 来源 ID 重复')
        ids.add(item['id'])


def extract_references(text, *, material=False):
    """Read our F2 [source_id] citation lines and F5 published reference regions.

    Only explicit bibliography headings and managed regions define entries.
    Inline citations, terminal prose and Markdown links are not entries.
    """
    # A material bundle may contain several complete documents. Validate each
    # managed region separately; a manuscript itself still requires one at EOF.
    from ..publication import START, END
    if material:
        pattern = re.escape(START) + r'.*?' + re.escape(END)
        matches = list(re.finditer(pattern, prose(text), re.S))
        regions = [text[m.start():m.end()] for m in matches]
        body = text
        for match in reversed(matches):
            blank = re.sub(r'[^\n]', ' ', body[match.start():match.end()])
            body = body[:match.start()] + blank + body[match.end():]
        split_references(body)  # Reject unmatched or malformed boundaries.
    else:
        body, managed = split_references(text)
        regions = [managed] if managed else []
    found = []
    for managed in regions:
        split_references(managed)
        pattern = r'<a id="paper-ref-([\w.-]+)"></a>\s*\n\s*\[\d+\] ([^\n]+)'
        entries = []
        for match in re.finditer(pattern, managed):
            citation = html.unescape(re.sub(r'\\([\\`*_{}\[\]])', r'\1', match[2]))
            entries.append({'id': match[1], 'citation': citation})
        if not entries:
            raise ValueError('受管参考文献区域无法解析')
        found.extend(entries)
    lines = body.splitlines()
    masked = prose(body).splitlines()
    entry = re.compile(r'^\s*(?:[-*]\s+)?\[([\w.-]+)\][ \t]+(\S.*)$')
    heading = re.compile(r'^(#{1,6})\s+(.+?)\s*$')
    in_bibliography = None
    for index, line in enumerate(masked):
        h = heading.match(line)
        if h:
            if in_bibliography is not None and len(h[1]) <= in_bibliography:
                in_bibliography = None
            if re.fullmatch(r'(?:参考文献|文献列表|References|Bibliography)', h[2], re.I):
                in_bibliography = len(h[1])
            continue
        if re.fullmatch(r'\*\*(参考文献|References|Bibliography)\*\*', line, re.I):
            in_bibliography = 6
            continue
        m = entry.match(line)
        if not m or m[1].isdigit():
            continue
        if in_bibliography is None:
            continue
        original = entry.match(lines[index])
        found.append({'id': m[1], 'citation': original[2].strip()})
    result = reference_catalog(references=found)
    validate_references(result)
    return result


def append_note(value, filename, text):
    old = value.get('writing_materials') or ''
    value['writing_materials'] = old + ('\n\n' if old else '') + f'## Source: {filename}\n\n{text}'


def source_catalog(source):
    """Build the transient Writer catalog from papers and literal document bibliographies."""
    references = []
    for key in ('question', 'evidence', 'writing_materials', 'outline', 'manuscript'):
        if isinstance(source.get(key), str):
            references.extend(extract_references(source[key], material=key == 'writing_materials'))
    return reference_catalog(source.get('papers'), references)


def pool_catalog(pool):
    from ..workflow import material
    return source_catalog({kind: material(pool, kind)['content'] for kind in pool['current']})


def initialize_upload(value, request):
    """Start a new F5 session from business fields or the CURRENT imported pool."""
    from .. import workflow
    value = workflow._unwrap(copy.deepcopy(value))
    if not isinstance(value, dict):
        raise ValueError('F5 JSON 输入必须是业务对象或导出的材料池')
    if value.get('demo'):
        return workflow.pool_from_business(value)
    if not value and ('[paper:demo]' in request or '默认演示材料' in request):
        return workflow.seed_pool()
    original = None
    if 'schema' in value:
        original = workflow.validate_pool(value)
        value = {'topic': original['topic'], 'constraints': original.get('constraints', {})}
        for kind in original['current']:
            value[kind] = copy.deepcopy(workflow.material(original, kind)['content'])
    value['request'] = request or value.get('request', '')
    value.pop('selected_issue_ids', None)
    if not value.get('topic'):
        title = value.get('manuscript') or value.get('outline') or request
        if not title.strip():
            raise MissingMaterials('请提供研究材料、稿件或研究主题')
        value['topic'] = title.splitlines()[0].lstrip('# ').strip()[:120]
    if value.get('review') and not value.get('review_data'):
        if value['review'] not in (value.get('writing_materials') or ''):
            append_note(value, 'uploaded-review.md', value['review'])
    catalog = source_catalog(value)
    if value.get('manuscript') and not value.get('review_data'):
        from ..publication import normalize_source_citations, publish_citations
        value['manuscript'] = publish_citations(normalize_source_citations(value['manuscript'], catalog), catalog)
    pool = workflow.pool_from_business(value)
    if original:
        # Reset execution/selection, not provenance. Historical materials remain
        # available to resolve derived_from; they never select the next step.
        fresh = copy.deepcopy(original)
        fresh.update(sequence=0, history=[], selected_issue_ids=[], request=value['request'])
        for kind in pool['current']:
            new = workflow.material(pool, kind)
            old = workflow.material(fresh, kind)
            if old is None or old['content'] != new['content']:
                workflow.add_material(fresh, kind, new['content'],
                                      [fresh['current'][kind]] if old else [],
                                      old['source_refs'] if old else new['source_refs'],
                                      is_demo=old['mock'] if old else new['mock'])
        pool = fresh
    return workflow.validate_pool(pool)


def merge_upload(pool, extra):
    """Merge explicitly co-uploaded Markdown/images without guessing replacements."""
    from .. import workflow
    pool = copy.deepcopy(pool)
    old_notes = (workflow.material(pool, 'writing_materials') or {}).get('content', '')
    incoming_notes = extra.get('writing_materials') or ''
    for kind, incoming in extra.items():
        if kind in {'topic', 'request'}:
            continue
        old = workflow.material(pool, kind)
        previous = old['content'] if old else None
        if kind == 'writing_materials':
            incoming = old_notes + ('\n\n' if old_notes and incoming_notes else '') + incoming_notes
        elif kind == 'papers':
            indexed = {p['id']: p for p in previous or []}
            for paper in incoming:
                if paper['id'] in indexed and indexed[paper['id']] != paper:
                    raise ValueError('同批上传的文献内容冲突')
                indexed[paper['id']] = paper
            incoming = list(indexed.values())
        elif previous is not None and previous != incoming:
            raise MissingMaterials(f'同批上传的 {kind} 存在多个版本，请明确使用哪个版本')
        workflow.add_material(pool, kind, incoming, [pool['current'][kind]] if old else [],
                              old['source_refs'] if old else [],
                              is_demo=old['mock'] if old else False)
    return initialize_upload(pool, pool.get('request', ''))
