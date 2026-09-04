from lazymind.chat.engine.tools.writer import (
    WriterCreateToolkit as LegacyWriterCreateToolkit,
)
from lazymind.chat.engine.tools import WriterRevisionToolkit as RegisteredWriterRevisionToolkit
from lazymind.document_tools import (
    DocumentResourceToolkit,
    DocumentRevisionToolkit,
    DocumentWritingToolkit,
    WriterCreateToolkit,
    WriterResourceToolkit,
    WriterRevisionToolkit,
    document_action_names,
    get_document_action,
    register_document_action,
)


def test_legacy_writer_import_uses_shared_document_toolkit():
    assert LegacyWriterCreateToolkit is WriterCreateToolkit
    assert DocumentWritingToolkit is WriterCreateToolkit
    assert DocumentRevisionToolkit is WriterRevisionToolkit
    assert DocumentResourceToolkit is WriterResourceToolkit
    assert RegisteredWriterRevisionToolkit is WriterRevisionToolkit


def test_document_action_registry_is_explicit_and_deterministic():
    def preview():
        return 'preview'

    register_document_action('test_document_action', 'preview', preview)

    assert get_document_action('test_document_action', 'preview') is preview
    assert 'test_document_action' in document_action_names()
