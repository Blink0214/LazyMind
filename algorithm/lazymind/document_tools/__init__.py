"""Shared document capabilities for Chat Agents and Workflow plugins.

The package is the stable LazyMind boundary over LazyLLM Writer.  Legacy
imports from ``lazymind.chat.engine.tools.writer`` remain supported while new
callers should import document capabilities from here.
"""

from .actions import (
    document_action_names,
    get_document_action,
    register_document_action,
)
from .artifacts import (
    decode_document_value,
    load_artifact_data,
    writer_schema,
)
from .references import bind_cross_reference_targets
from .resources import (
    DocumentResourceToolkit,
    WriterResourceToolkit,
    extract_provider_resources,
    resolve_provider_target,
    resolve_provider_targets,
)
from .revision import (
    DocumentRevisionToolkit,
    WriterRevisionToolkit,
    sync_writer_documents,
)
from .writing import (
    DocumentWritingToolkit,
    DraftMarkdownStreamEventEmitter,
    WriterCreateToolkit,
)
from .toolkits import WriterToolkitBase

__all__ = [
    'DocumentResourceToolkit',
    'DocumentRevisionToolkit',
    'DocumentWritingToolkit',
    'DraftMarkdownStreamEventEmitter',
    'WriterCreateToolkit',
    'WriterResourceToolkit',
    'WriterRevisionToolkit',
    'WriterToolkitBase',
    'bind_cross_reference_targets',
    'decode_document_value',
    'document_action_names',
    'extract_provider_resources',
    'get_document_action',
    'load_artifact_data',
    'register_document_action',
    'resolve_provider_target',
    'resolve_provider_targets',
    'sync_writer_documents',
    'writer_schema',
]
