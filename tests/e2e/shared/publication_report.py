"""Publication execution receipts and review links; no provider reads or content verdicts."""
from __future__ import annotations

from urllib.parse import quote, urlsplit

from .readback_common import sanitize


def _web_url(value):
    if not isinstance(value, str):
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
            return ''
    except ValueError:
        return ''
    return sanitize(value)


def review_target(provider: str, response: dict) -> dict:
    """Extract only receipt-owned targets. A management console is not a draft link."""
    target = response.get('target_document') or {}
    meta = target.get('meta') or {}
    links = []
    identifiers = []
    def link(label, url):
        url = _web_url(url)
        if url and url not in [item['url'] for item in links]:
            links.append({'label': label, 'url': url})
    identifier = str(target.get('doc_id') or '')
    if provider == 'wechat':
        if identifier:
            identifiers.append(f"草稿 ID：{identifier}；文章索引：{meta.get('article_index', 0)}")
        identifiers.append('未提供可核查的草稿预览链接，请按草稿 ID 在公众号后台核查')
    elif provider == 'obsidian':
        local_path = str(meta.get('local_path') or '')
        if local_path:
            identifiers.append(f'本地路径：{local_path}')
            if local_path.startswith('/'):
                links.append({'label': '本地文件', 'url': local_path})
        if identifier:
            identifiers.append(f'文档 ID：{identifier}')
    else:
        if provider == 'github':
            link('PR', meta.get('pull_request_url'))
        link('文档', target.get('uri'))
        link('文档', meta.get('browser_url'))
        if provider == 'github':
            link('PR', meta.get('pull_request_url'))
            parts = identifier.split(':', 2)
            commit = str(meta.get('commit_sha') or '')
            if len(parts) == 3:
                repository, branch, path = parts
                identifiers.extend([f'仓库：{repository}', f'分支：{branch}', f'文件：{path}'])
                # Construct a permalink only from the repository file identity and write commit in the receipt.
                import re
                if re.fullmatch(r'[\w.-]+/[\w.-]+', repository) and re.fullmatch(r'[0-9a-fA-F]{40,64}', commit):
                    link('写入版本', f'https://github.com/{repository}/blob/{commit}/{quote(path, safe="/")}')
            if commit:
                identifiers.append(f'提交：{commit}')
        if identifier:
            identifiers.append(f'文档 ID：{identifier}')
    return sanitize({'links': links, 'identifiers': identifiers})


def summarize_publications(rule: dict, evidence: dict | None) -> dict:
    entries = (evidence or {}).get('publications') or {}
    rows, checks = [], []
    for provider in rule['providers']:
        entry = entries.get(provider) or {}
        response = entry.get('response') or {}
        status = entry.get('status') or 'not_attempted'
        reason = entry.get('reason') or entry.get('error_code') or ''
        if status == 'succeeded':
            valid = (response.get('provider') == provider and response.get('provider_synced') is True
                     and response.get('artifact_saved') is True and bool(response.get('artifact_id')))
            check_status = 'PASS' if valid else 'FAIL'
            target = response.get('target_document') or {}
            if valid and not (target.get('doc_id') or (target.get('meta') or {}).get('local_path')
                              or review_target(provider, response)['links']):
                check_status, reason = 'WARN', '发布回执成功，但未返回可核查目标'
            if not valid:
                status, reason = 'invalid_receipt', '发布成功回执不完整或平台不匹配'
        elif status == 'failed_before_write':
            check_status = 'FAIL'
        else:
            check_status = 'WARN'
            reason = reason or (evidence or {}).get('capture_error') or '未完成发布或缺少发布证据'
        checks.append({'name': f'publication.{provider}.execution', 'status': check_status,
                       'detail': reason or '发布回执确认成功；内容尚待 AI 独立回读验收'})
        rows.append({'provider': provider, 'execution_status': status, 'reason': reason,
                     'acceptance_status': 'PENDING_AI_REVIEW', **review_target(provider, response)})
    states = {item['status'] for item in checks}
    execution = 'FAIL' if 'FAIL' in states else 'INCONCLUSIVE' if 'WARN' in states else 'PASS'
    return sanitize({'execution_verdict': execution, 'acceptance_status': 'PENDING_AI_REVIEW',
                     'platforms': rows, 'checks': checks})


def _cell(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\n', ' ').replace('[', '&#91;').replace(']', '&#93;').replace('`', '&#96;')


def report_lines(summary: dict) -> list[str]:
    labels = {'succeeded': '发布回执成功', 'failed_before_write': '写入前失败',
              'unavailable': '未配置或不可用', 'not_attempted': '未执行',
              'blocked': '阻断', 'outcome_unknown': '结果未知', 'write_pending': '未取得结果',
              'invalid_receipt': '回执无效'}
    lines = ['', '## 发布目标与人工核查', '',
             f"发布执行结果：**{summary['execution_verdict']}**；业务验收：**待 AI 验收**。",
             '链接和标识来自本次发布回执，不代表已确认目标内容正确。', '',
             '| 平台 | 发布执行状态 | 文档或地址链接 | 目标标识 / 说明 | 业务验收 |',
             '|---|---|---|---|---|']
    for row in summary['platforms']:
        links = ' · '.join(f"[{_cell(item['label'])}](<{quote(item['url'], safe='/:#?=&%@+~')}>)" for item in row['links']) or '—'
        info = '；'.join([*row['identifiers'], row['reason']]).strip('；') or '未返回目标标识'
        lines.append(f"| {_cell(row['provider'])} | {_cell(labels.get(row['execution_status'], row['execution_status']))} | {links} | {_cell(info)} | 待 AI 验收 |")
    lines.extend(['', '按 evidence.json.acceptance 逐项回读并填写结论；保留上述执行状态、链接和原始证据。',
                  '每个平台补充：回读方式与时间、证据文件、逐项结果、缺失证据，以及 PASS / FAIL / INCONCLUSIVE 结论。', ''])
    return lines
