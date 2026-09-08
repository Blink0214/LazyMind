from pathlib import Path

import json
import pytest

from lazyllm.tools.writer.data_models import (
    PatchHunk,
    PatchSet,
    TargetDocument,
    WriterBlock,
    WriterDocument,
)
from lazyllm.tools.writer.provider.wechat import WeChatWriterProvider
from lazymind.chat.engine.tools.writer import (
    WriterResourceToolkit,
    _prepare_wechat_cover,
)
from PIL import Image


def test_wechat_prompt_title_load_patch_and_write_back(monkeypatch):
    calls = []

    class Client:
        def __init__(self, token):
            assert token == 'stable-token'

        def batch_get_drafts(self, offset, count, *, no_content=False):
            calls.append(('list', offset, count, no_content))
            return {
                'total_count': 2,
                'item': [
                    {'media_id': 'first', 'content': {'news_item': [
                        {'title': '另一篇文章'},
                        {'title': '需要修改的完整标题'},
                    ]}},
                    {'media_id': 'second', 'content': {'news_item': [
                        {'title': '需要修改的完整标题'},
                    ]}},
                ],
            }

        def get_draft(self, media_id):
            calls.append(('get', media_id))
            return {
                'update_time': 7,
                'news_item': [
                    {
                        'title': '另一篇文章',
                        'content': '<p>不能修改</p>',
                        'thumb_media_id': 'cover-0',
                    },
                    {
                        'title': '需要修改的完整标题',
                        'content': '<p style="color:red">需要修改</p>'
                        '<section data-card="keep"><custom-card /></section>',
                        'thumb_media_id': 'cover-1',
                    },
                ],
            }

        def update_draft(self, media_id, article, *, index=0):
            calls.append(('update', media_id, index, article))

    monkeypatch.setattr(
        'lazyllm.tools.writer.provider.wechat.WeChatClient', Client,
    )
    monkeypatch.setattr(
        WeChatWriterProvider,
        '_access_token',
        staticmethod(lambda: 'stable-token'),
    )

    toolkit = WriterResourceToolkit()
    loaded = json.loads(toolkit.load_document(
        '请把微信公众号草稿箱文章《需要修改的完整标题》中的“需要修改”改成“已经修改”',
    ))
    source = WriterDocument.model_validate(loaded['source_document'])
    changed = source.blocks[0].model_copy(deep=True)
    changed.content = '已经修改'
    changed.spans = []
    patch = PatchSet(
        patch_id='patch-title-match',
        target_doc_id=source.document_id,
        hunks=[PatchHunk(
            hunk_id='replace-paragraph',
            target_node_id=changed.node_id,
            modify_type='update',
            block=changed,
        )],
    )
    published = json.loads(toolkit.publish_revision(
        source_document_json=json.dumps(loaded['source_document'], ensure_ascii=False),
        patch_set_json=json.dumps(patch.model_dump(), ensure_ascii=False),
    ))

    assert calls[0] == ('list', 0, 20, True)
    assert calls[1] == ('get', 'first')
    update = calls[2]
    assert update[0:3] == ('update', 'first', 1)
    assert '<p>已经修改</p>' in update[3]['content']
    assert '<section data-card="keep"><custom-card /></section>' in update[3]['content']
    assert published['draft_document']['provider_binding']['document_id'] == 'first'


def test_wechat_prompt_title_not_found(monkeypatch):
    monkeypatch.setattr(
        WeChatWriterProvider,
        'list_drafts',
        lambda self: [{
            'media_id': 'draft-1',
            'content': {'news_item': [{'title': '真实完整标题'}]},
        }],
    )

    with pytest.raises(Exception, match='请输入草稿文章的准确完整标题'):
        WriterResourceToolkit().load_document(
            '请修改微信公众号草稿箱文章《不准确的标题》',
        )


@pytest.mark.parametrize(
    ('prompt', 'expected'),
    [
        ('请修改微信公众号草稿箱中的《目标文章》', True),
        ('请优化微信公众号草稿箱中的《目标文章》', True),
        ('请修改公众号草稿箱中的《目标文章》', False),
        ('请修改微信草稿箱中的《目标文章》', False),
        ('请查看微信公众号草稿箱中的《目标文章》', False),
    ],
)
def test_wechat_draft_revision_requires_all_explicit_terms(prompt, expected):
    assert WeChatWriterProvider.matches(prompt) is expected


def _document() -> WriterDocument:
    return WriterDocument(
        document_id='writer-cover',
        title='城市更新',
        stage='final',
        blocks=[WriterBlock(node_id='body', type='paragraph', content='正文内容')],
    )


def test_new_wechat_draft_generates_normalized_cover(tmp_path: Path):
    generated = tmp_path / 'generated.png'
    Image.new('RGB', (1200, 600), 'red').save(generated)
    calls = {}

    def generate(prompt, **kwargs):
        calls['prompt'] = prompt
        calls['kwargs'] = kwargs
        return {'local_path': str(generated)}

    target = _prepare_wechat_cover(
        TargetDocument(adapter='wechat', title='城市更新'),
        _document(),
        tmp_path,
        model_available=lambda role: role == 'image_generator',
        generator=generate,
    )

    assert '城市更新' in calls['prompt']
    assert calls['kwargs'] == {'image_size': '1024x1024', 'batch_size': 1}
    with Image.open(target.meta['cover_path']) as cover:
        assert cover.size == (900, 383)
        assert cover.format == 'PNG'


def test_new_wechat_draft_falls_back_to_provider_placeholder(tmp_path: Path):
    target = _prepare_wechat_cover(
        TargetDocument(adapter='wechat'),
        _document(),
        tmp_path,
        model_available=lambda role: False,
    )

    with Image.open(target.meta['cover_path']) as cover:
        assert cover.size == (900, 383)
        assert cover.format == 'PNG'


def test_new_wechat_draft_falls_back_when_generation_fails(tmp_path: Path):
    def fail(*args, **kwargs):
        raise RuntimeError('model failed')

    target = _prepare_wechat_cover(
        TargetDocument(adapter='wechat'),
        _document(),
        tmp_path,
        model_available=lambda role: True,
        generator=fail,
    )

    with Image.open(target.meta['cover_path']) as cover:
        assert cover.size == (900, 383)
        assert cover.format == 'PNG'


def test_wechat_cover_generation_does_not_run_for_existing_or_other_targets(
    tmp_path: Path,
):
    def generator(*args, **kwargs):
        raise AssertionError('must not run')

    existing = TargetDocument(adapter='wechat', doc_id='draft-1')
    notion = TargetDocument(adapter='notion')

    assert _prepare_wechat_cover(
        existing, _document(), tmp_path, generator=generator,
    ) is existing
    assert _prepare_wechat_cover(
        notion, _document(), tmp_path, generator=generator,
    ) is notion


@pytest.mark.parametrize('provider', ['wechat', 'feishu'])
def test_replace_preserves_provider_confirmed_ir(monkeypatch, tmp_path, provider):
    from lazymind.chat.engine.tools.writer import WriterResourceTools

    document = _document()
    persisted = tmp_path / 'persisted.json'
    persisted.write_text(document.model_dump_json())
    write_result = tmp_path / 'write_result.json'
    write_result.write_text(json.dumps({'success': True}))
    monkeypatch.setattr(WriterResourceTools, 'replace_document', lambda *args: {
        'artifact_path': str(write_result),
        'metadata': {
            'artifact_paths': {'persisted_document': str(persisted)},
            'representation': 'ir',
        },
    })

    def unexpected_reload(*args, **kwargs):
        pytest.fail('Confirmed writes must not reload the provider document')

    monkeypatch.setattr(WriterResourceTools, 'load_document', unexpected_reload)
    target = TargetDocument(
        adapter=provider, doc_id='existing-doc',
        meta={'browser_url': 'https://example.test/existing-doc'},
    )
    content = document.model_dump()
    result = json.loads(WriterResourceToolkit().replace_document(
        content_json=json.dumps(content),
        source_document_json='',
        target_document_json=target.model_dump_json(),
    ))

    assert result['provider'] == provider
    assert result['target_document']['adapter'] == provider
    assert result['target_document']['doc_id'] == 'existing-doc'
    assert result['representation'] == 'ir'
    published = WriterDocument.model_validate(result['draft_document'])
    assert published.document_id == document.document_id
    assert published.title == document.title
    assert [(b.type, b.content) for b in published.blocks] == [
        (b.type, b.content) for b in document.blocks
    ]


def test_replace_uses_generic_provider_readback_for_markdown(monkeypatch, tmp_path):
    from lazymind.chat.engine.tools.writer import WriterResourceTools

    markdown = '# GitHub draft\n\nBody'
    written = tmp_path / 'write-result.json'
    written.write_text(json.dumps({'success': True}))
    refreshed = tmp_path / 'source.md'
    refreshed.write_text(markdown)
    target = TargetDocument(
        adapter='github',
        doc_id='existing-doc',
        uri='githubrepo:/acme/docs/README.md?ref=main',
        meta={'browser_url': 'https://example.test/existing-doc'},
    )
    refreshed_target = tmp_path / 'target.json'
    refreshed_target.write_text(target.model_dump_json())
    monkeypatch.setattr(WriterResourceTools, 'replace_document', lambda *args: {
        'artifact_path': str(written),
        'metadata': {'artifact_paths': {}, 'representation': 'markdown'},
    })
    monkeypatch.setattr(WriterResourceTools, 'load_document', lambda *args: {
        'artifact_path': str(refreshed),
        'representation': 'markdown',
        'metadata': {
            'artifact_paths': {'target_document': str(refreshed_target)},
        },
    })

    result = json.loads(WriterResourceToolkit().replace_document(
        content_json=json.dumps(markdown),
        source_document_json='',
        target_document_json=target.model_dump_json(),
    ))

    assert result['provider'] == 'github'
    assert result['representation'] == 'markdown'
    assert result['draft_document'] == markdown
    assert result['target_document']['doc_id'] == 'existing-doc'


def test_missing_provider_url_reports_writeback_error():
    from lazymind.chat.engine.tools.writer import _published_link, ToolExecutionError

    with pytest.raises(ToolExecutionError, match='no browser URL was returned'):
        _published_link(TargetDocument(adapter='wechat', doc_id='existing-doc'))
