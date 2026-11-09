import json
from unittest.mock import patch, Mock

import pytest
import requests

from lazyllm.common import KeyAuthError
from lazyllm.tools.fs.base import LinkDocumentFSBase
from lazyllm.tools.fs.errors import ProviderAPIError
from lazyllm.tools.fs.supplier.notion import NotionFS, _is_notion_object_not_found
from lazyllm.tools.fs.supplier.feishu import FeishuFSBase
from lazyllm.tools.writer.provider import WriterProviderWriteOutcomeError, is_ambiguous_write_error
from lazyllm.tools.writer.provider.notion import NotionWriterProvider
from lazyllm.tools.writer.adapter.base import NativePatchOperation


def response(status, body):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


@pytest.mark.parametrize('provider', ['notion', 'feishu'])
@pytest.mark.parametrize('status', [400, 403, 429, 503])
def test_platform_http_errors_share_minimal_contract(provider, status):
    fs = NotionFS(token='fixture') if provider == 'notion' else FeishuFSBase(dynamic_auth=True)
    body = {'code': 'validation_error' if provider == 'notion' else 99991672,
            'message' if provider == 'notion' else 'msg': 'File too large.', 'request_id': 'aabb-1234'}
    raw = requests.HTTPError(response=response(status, body))
    with patch.object(LinkDocumentFSBase, '_request', side_effect=raw):
        with pytest.raises(ProviderAPIError) as caught:
            fs._request('POST', 'https://example.test/upload')
    error = caught.value
    assert error.code == str(body['code'])
    assert str(error) == 'File too large.'
    assert error.status_code == status
    assert error.request_id == 'aabb-1234'
    assert error.__cause__ is raw
    assert not hasattr(error, 'details')
    assert not hasattr(error, 'retryable')
    assert is_ambiguous_write_error(error) == (status >= 500)


def test_feishu_http_200_business_failure():
    raw = response(200, {'code': 1254302, 'msg': 'Permission denied'})
    raw.headers['x-tt-logid'] = 'request-123'
    with patch.object(LinkDocumentFSBase, '_request', return_value=raw):
        with pytest.raises(ProviderAPIError) as caught:
            FeishuFSBase(dynamic_auth=True)._request('POST', '/docx')
    assert caught.value.code == '1254302'
    assert caught.value.status_code == 200
    assert caught.value.request_id == 'request-123'


@pytest.mark.parametrize('provider', ['notion', 'feishu'])
@pytest.mark.parametrize('raw', [requests.Timeout(), requests.ConnectionError()])
def test_network_errors_are_passed_through(provider, raw):
    fs = NotionFS(token='fixture') if provider == 'notion' else FeishuFSBase(dynamic_auth=True)
    with patch.object(LinkDocumentFSBase, '_request', side_effect=raw):
        with pytest.raises(type(raw)) as caught:
            fs._request('POST', '/pages')
    assert caught.value is raw
    assert is_ambiguous_write_error(raw)


def test_auth_and_notion_not_found_compatibility():
    fs = NotionFS(token='fixture')
    with patch.object(LinkDocumentFSBase, '_request', side_effect=KeyAuthError('private')):
        with pytest.raises(ProviderAPIError) as caught:
            fs._request('GET', '/pages')
    assert caught.value.code == 'unauthorized'
    assert caught.value.status_code == 401
    raw = response(404, {'code': 'object_not_found', 'message': 'Not found'})
    with patch.object(LinkDocumentFSBase, '_request', side_effect=requests.HTTPError(response=raw)):
        with pytest.raises(ProviderAPIError) as caught:
            fs._request('GET', '/pages')
    assert _is_notion_object_not_found(caught.value)


def test_safe_message():
    error = ProviderAPIError('bad', 'Invalid "private document" secret_ABC ntn_DEF Bearer xyz https://example.com/private')
    for value in ['private document', 'ABC', 'DEF', 'xyz', 'example.com']:
        assert value not in str(error)


@pytest.mark.parametrize('failed_index', [0, 1])
def test_notion_batch_preserves_cause_and_marks_partial_write(failed_index):
    provider = NotionWriterProvider()
    error = ProviderAPIError('validation_error', 'Invalid row', 400)
    rows = {str(i): NativePatchOperation('update', {'block_id': str(i)}) for i in range(2)}
    provider._execute_native_operation = Mock(side_effect=[None] * failed_index + [error])
    expected = ProviderAPIError if failed_index == 0 else WriterProviderWriteOutcomeError
    with pytest.raises(expected) as caught:
        provider._flush_cell_updates(None, 'page', rows, ['hunk'], [])
    if failed_index == 0:
        assert caught.value is error
    else:
        assert caught.value.__cause__ is error
        assert caught.value.retryable is False


@pytest.mark.parametrize('provider', ['notion', 'feishu'])
def test_caption_failure_after_main_operation_is_ambiguous(provider):
    from lazyllm.tools.writer.provider.feishu import FeishuWriterProvider
    error = ProviderAPIError('invalid', 'Caption rejected', 400)
    fs = Mock()
    fs.update_block.side_effect = [{}, error]
    operation = NativePatchOperation('update', {
        'block_id': 'main', '_caption_operation': NativePatchOperation('update', {'block_id': 'caption'}),
    })
    with pytest.raises(WriterProviderWriteOutcomeError) as caught:
        if provider == 'notion':
            NotionWriterProvider._execute_native_operation(fs, 'page', operation)
        else:
            FeishuWriterProvider._execute_native_operation(fs, 'page', operation, None)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize('provider', ['notion', 'feishu'])
def test_permission_errors_do_not_trigger_credential_rotation(provider):
    fs = NotionFS(token='fixture') if provider == 'notion' else FeishuFSBase(dynamic_auth=True)
    assert not fs._is_key_auth_error(response(403, {}))
    assert fs._is_key_auth_error(response(401, {}))


def test_successful_binary_response_is_unchanged():
    raw = response(200, {})
    raw._content = b'\x89PNG\r\n'
    with patch.object(LinkDocumentFSBase, '_request', return_value=raw):
        assert FeishuFSBase(dynamic_auth=True)._request('GET', '/media') is raw


@pytest.mark.parametrize('name', ['notion', 'feishu'])
@pytest.mark.parametrize('title', ['', 'New title'])
def test_body_failure_after_title_write_keeps_cause(name, title):
    from types import SimpleNamespace
    from lazyllm.tools.writer.provider.feishu import FeishuWriterProvider
    provider = NotionWriterProvider() if name == 'notion' else FeishuWriterProvider()
    error = ProviderAPIError('invalid', 'Body rejected', 400)
    fs = Mock()
    fs.replace_doc_blocks.side_effect = error
    fs.update_page_title.return_value = {}
    fs.update_document_title.return_value = {}
    source = SimpleNamespace(title=title, revision=None, provider_binding={})
    source.model_copy = lambda **kwargs: source
    converted = SimpleNamespace(provider=name, format=f'{name}_blocks', source_document=source, content=[{}])
    provider._resolve_document_target = Mock(return_value=(name, '/page', fs, Mock(), '/page', 'page'))
    provider._writer_adapter = Mock(return_value=Mock(materialize_internal_links=Mock(return_value=[{}])))
    provider._writable_media_content = Mock(return_value=[{}])
    if name == 'feishu':
        provider._update_document_title = Mock(return_value={})
    expected = WriterProviderWriteOutcomeError if title else ProviderAPIError
    with pytest.raises(expected) as caught:
        provider.write_document(converted, SimpleNamespace(title=title), mode='replace')
    assert (caught.value.__cause__ if title else caught.value) is error
