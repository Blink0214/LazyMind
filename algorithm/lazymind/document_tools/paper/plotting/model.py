"""Use the application's configured text model for planning and Python generation."""
from __future__ import annotations

import json
import os

from .code_response import parse_code_response, normalize_response, json_body
from . import transport
from ..analysis import trace as diagnostics


class PlotModelError(RuntimeError):
    """No model output is a service failure, not a generated-program repair request."""


def request_model(prompt: str, json_output=True, *, output_format=None):
    from lazyllm import AutoModel

    options = {'stream_output': False}
    if json_output:
        options['response_format'] = {'type': 'json_object'}
    policy = transport.configuration()
    client = AutoModel(model='llm')
    supported = transport.configure(client, policy)
    with diagnostics.stage('model_request', model_alias='llm', json_output=json_output,
                           request_bytes=len(prompt.encode()), transport_controlled=supported,
                           connect_timeout_seconds=policy.connect if supported else None,
                           read_timeout_seconds=policy.read if supported else None,
                           max_transport_attempts=policy.attempts if supported else None):
        preview = diagnostics.response_preview(json_output, output_format=output_format)
        streaming = (preview is not None and supported and not getattr(preview.recorder, 'streaming_rejected', False)
                     and os.environ.get('LAZYMIND_PAPER_MODEL_STREAM', 'auto') != 'off')
        reasoning_chunks = []
        if streaming:
            def sink(event):
                if event.get('tag') == 'think' and event.get('delta'):
                    reasoning_chunks.append(str(event['delta']))
                preview.sink(event)
            options['stream_output'] = {'_stream_sink': sink}
        try:
            try:
                raw = transport.call(client, prompt, options, policy, supported)
            except Exception as exc:
                # Downgrade only an explicit pre-response rejection of streaming.
                # Interrupted streams and ordinary service errors retain their existing policy.
                failure = getattr(getattr(exc, 'terminal', None), 'failure', None)
                messages = ' '.join(str(item) for item in diagnostics.exception_chain(exc)).lower()
                unsupported = (streaming and not preview.received_text and failure is not None
                               and not getattr(getattr(exc, 'terminal', None), 'has_semantic_output', True)
                               and getattr(failure, 'provider_http_status', None) in (400, 422, 501)
                               and 'stream' in messages
                               and any(word in messages for word in ('unsupported', 'not supported', 'not allowed')))
                if not unsupported:
                    raise
                preview.recorder.streaming_rejected = True
                raw = transport.call(client, prompt, {**options, 'stream_output': False}, policy, supported)
            # F4 keeps every emitted reasoning delta, including repeated ones;
            # the shared SDK's metadata merge policy remains unchanged.
            if reasoning_chunks and isinstance(raw, dict):
                raw = dict(raw, reasoning_content=''.join(reasoning_chunks))
            normalized = normalize_response(raw)
            if preview:
                preview.finish(raw, normalized=normalized)
        except BaseException:
            if preview:
                preview.abort()
            raise
        diagnostics.response(raw)
    text = normalized.body
    if not text or isinstance(text, str) and not text.strip():
        raise PlotModelError('绘图模型未返回内容，请检查模型服务状态；不将空响应当作绘图代码错误重写')
    return text


def call_code_model(prompt: str) -> dict:
    return parse_code_response(request_model(prompt, json_output=False))


def call_markdown_model(prompt: str) -> str:
    return request_model(prompt, json_output=False, output_format='markdown')


def call_model(prompt: str) -> dict:
    raw = request_model(prompt)
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    result = json.loads(json_body(text))
    if not isinstance(result, dict):
        raise ValueError('绘图模型必须返回 JSON 对象')
    return result


def profile(table: dict) -> dict:
    columns = {}
    for index, name in enumerate(table['columns']):
        values = [row[index] for row in table['rows'] if row[index] is not None]
        numeric = values and all(type(v) in (int, float) for v in values)
        columns[name] = {'missing': len(table['rows']) - len(values),
                         'types': sorted({type(v).__name__ for v in values})}
        if numeric:
            columns[name].update(min=min(values), max=max(values))
        else:
            unique = list(dict.fromkeys(values))
            columns[name].update(unique_count=len(unique), examples=unique[:20])
    # Counts come from all rows, even at planning time; previews cannot reveal every group's size.
    candidates = ([table['options']['group_by']] if table.get('options', {}).get('group_by') else
                  [name for name, info in columns.items() if info.get('unique_count', 21) <= 20
                   and info['types'] == ['str']][:5])
    group_profiles = {}
    numeric_columns = {name: table['columns'].index(name) for name, info in columns.items() if 'min' in info}
    for name in candidates:
        if columns[name].get('unique_count', 21) > 20:
            continue
        index = table['columns'].index(name)
        groups = {}
        for row in table['rows']:
            group = row[index]
            if group not in groups:
                groups[group] = {'name': group, 'row_count': 0,
                                 'non_null_counts': dict.fromkeys(numeric_columns, 0)}
            item = groups[group]
            item['row_count'] += 1
            for metric, metric_index in numeric_columns.items():
                item['non_null_counts'][metric] += row[metric_index] is not None
        group_profiles[name] = list(groups.values())
    return {'row_count': len(table['rows']), 'columns': columns, 'group_profiles': group_profiles,
            'sample_rows': table['rows'][:8], 'metadata': table.get('metadata', {}),
            'explicit_options': table.get('options', {})}
