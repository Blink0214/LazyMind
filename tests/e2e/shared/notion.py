"""Read full Notion block trees through independently authenticated ntn."""
from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit
from .readback_common import CliReader, ReadbackDocument


def page_id(reference: str) -> str:
    value = reference.strip()
    if value.startswith(('https://', 'http://')):
        parsed = urlsplit(value)
        if not (parsed.hostname or '').endswith(('.notion.so', '.notion.site')) and parsed.hostname not in ('notion.so', 'notion.site'):
            raise ValueError('Expected a Notion page URL or page ID')
        value = parsed.path.rstrip('/').split('/')[-1]
    match = re.search(r'([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$', value)
    if not match:
        raise ValueError('Invalid Notion page ID')
    return str(uuid.UUID(match.group(1)))


def fetch_notion_document(reference: str, *, timeout: int = 180) -> ReadbackDocument:
    identifier = page_id(reference)
    cli = CliReader(timeout)
    metadata = cli.json(['ntn', 'api', f'v1/pages/{identifier}', '-X', 'GET'])
    if metadata.get('object') != 'page' or metadata.get('archived') or metadata.get('in_trash'):
        raise RuntimeError('Notion target is not an active page')
    visited = set()

    def children(block_id):
        if block_id in visited:
            raise RuntimeError('Notion returned a cyclic block tree')
        visited.add(block_id)
        blocks, cursors, cursor = [], set(), None
        while True:
            args = ['ntn', 'api', f'v1/blocks/{block_id}/children', '-X', 'GET', 'page_size==100']
            if cursor:
                args.append(f'start_cursor=={cursor}')
            payload = cli.json(args)
            if payload.get('object') != 'list' or not isinstance(payload.get('results'), list):
                raise RuntimeError('Notion returned invalid block results')
            for block in payload['results']:
                if block.get('has_children'):
                    block['children'] = children(block['id'])
                blocks.append(block)
            if not payload.get('has_more'):
                return blocks
            cursor = payload.get('next_cursor')
            if not cursor or cursor in cursors:
                raise RuntimeError('Notion pagination cursor missing or repeated')
            cursors.add(cursor)

    blocks = children(identifier)
    def rich_text(items):
        return ''.join(x.get('plain_text', x.get('text', {}).get('content',
                       x.get('equation', {}).get('expression', ''))) for x in items)
    def lines(items):
        for block in items:
            body = block.get(block.get('type'), {})
            if block.get('type') == 'table_row':
                yield '\t'.join(rich_text(cell) for cell in body.get('cells', []))
            else:
                text = rich_text(body.get('rich_text', []))
                if text:
                    yield text
                if body.get('expression'):
                    yield body['expression']
                caption = rich_text(body.get('caption', []))
                if caption:
                    yield caption
            yield from lines(block.get('children', []))
    title = next((rich_text(p.get('title', [])) for p in metadata.get('properties', {}).values()
                  if p.get('type') == 'title'), '')
    return ReadbackDocument('notion', identifier, metadata.get('last_edited_time', ''),
                            '\n'.join(lines(blocks)), title, metadata.get('url', ''),
                            {'page': metadata, 'blocks': blocks})
