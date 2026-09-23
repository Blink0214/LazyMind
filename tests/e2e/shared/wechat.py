"""Independent WeChat official-account draft readback using stdlib HTTP."""
from __future__ import annotations

import hashlib
import json
import os
import time
from html.parser import HTMLParser
from urllib import request
from urllib.parse import urlencode

from .readback_common import ReadbackDocument


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_json(endpoint: str, body: dict, timeout: float) -> dict:
    req = request.Request('https://api.weixin.qq.com/cgi-bin/' + endpoint,
                          data=json.dumps(body).encode('utf-8'), method='POST',
                          headers={'Content-Type': 'application/json'})
    try:
        with request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError) as exc:
        # Exception URLs can contain the token; do not expose exception chains.
        raise RuntimeError('WeChat request failed; check network, credentials and account access') from None
    if not isinstance(payload, dict):
        raise RuntimeError('WeChat returned invalid JSON object')
    code = payload.get('errcode', 0)
    if str(code) != '0':
        safe_code = str(code) if str(code).lstrip('-').isdigit() else 'unknown'
        raise RuntimeError(f'WeChat API error {safe_code}; check token, account permissions and IP allowlist')
    return payload


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'img':
            attributes = dict(attrs)
            self.images.append({'src': attributes.get('data-src') or attributes.get('src') or '',
                                'alt': attributes.get('alt') or ''})


def fetch_wechat_document(media_id: str, *, article_index: int = 0,
                          timeout: int = 180) -> ReadbackDocument:
    if not media_id.strip():
        raise ValueError('WeChat media_id required')
    if isinstance(article_index, bool) or not isinstance(article_index, int) or article_index < 0:
        raise ValueError('article_index must be a non-negative integer')
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    deadline = time.monotonic() + timeout
    def post(endpoint, body):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('WeChat readback timed out')
        return _post_json(endpoint, body, remaining)
    token = os.environ.get('LAZYMIND_E2E_WECHAT_ACCESS_TOKEN', '').strip()
    if not token:
        app_id = os.environ.get('LAZYMIND_E2E_WECHAT_APP_ID', '').strip()
        secret = os.environ.get('LAZYMIND_E2E_WECHAT_APP_SECRET', '').strip()
        if not app_id or not secret:
            raise RuntimeError('Set LAZYMIND_E2E_WECHAT_ACCESS_TOKEN or both LAZYMIND_E2E_WECHAT_APP_ID and LAZYMIND_E2E_WECHAT_APP_SECRET')
        auth = post('stable_token', {'grant_type': 'client_credential', 'appid': app_id,
                                     'secret': secret, 'force_refresh': False})
        token = auth.get('access_token')
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError('WeChat returned no access token')
    payload = post('draft/get?' + urlencode({'access_token': token}), {'media_id': media_id})
    articles = payload.get('news_item')
    if not isinstance(articles, list) or not all(isinstance(item, dict) for item in articles):
        raise RuntimeError('WeChat returned no valid news_item list')
    if article_index >= len(articles):
        raise RuntimeError(f'WeChat article index out of range (article count={len(articles)})')
    article = articles[article_index]
    content = article.get('content')
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError('WeChat draft article has no HTML content')
    images = _Images()
    images.feed(content)
    digest = hashlib.sha256(json.dumps(article, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # A content_source_url is a "read original" link, NOT a draft preview.
    return ReadbackDocument('wechat', f'{media_id}:article-{article_index}', digest,
                            content, article.get('title', ''), '',
                            {'media_id': media_id, 'article_index': article_index,
                             'article_count': len(articles), 'article': article,
                             'images': images.images, 'revision_kind': 'article_sha256'})
