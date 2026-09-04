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
    lmd_to_markdown,
    markdown_to_lmd,
    markdown_to_writer_document,
    writer_schema,
)
from .references import bind_cross_reference_targets
from .resources import (
    extract_provider_resources,
    resolve_provider_target,
    resolve_provider_targets,
    sync_document,
    sync_writer_documents,
)
from .toolkits import (
    DocumentResourceToolkit,
    DocumentRevisionToolkit,
    DocumentWritingToolkit,
    DraftMarkdownStreamEventEmitter,
    WriterCreateToolkit,
    WriterResourceToolkit,
    WriterRevisionToolkit,
    WriterToolkitBase,
)

__all__ = [
    "DocumentResourceToolkit",
    "DocumentRevisionToolkit",
    "DocumentWritingToolkit",
    "DraftMarkdownStreamEventEmitter",
    "WriterCreateToolkit",
    "WriterResourceToolkit",
    "WriterRevisionToolkit",
    "WriterToolkitBase",
    "bind_cross_reference_targets",
    "document_action_names",
    "extract_provider_resources",
    "get_document_action",
    "lmd_to_markdown",
    "markdown_to_lmd",
    "markdown_to_writer_document",
    "register_document_action",
    "resolve_provider_target",
    "resolve_provider_targets",
    "sync_document",
    "sync_writer_documents",
    "writer_schema",
]
