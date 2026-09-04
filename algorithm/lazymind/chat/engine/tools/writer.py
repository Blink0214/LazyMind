"""Compatibility imports for the shared document toolkits.

New code should import from :mod:`lazymind.document_tools`.  This module keeps
existing Chat Agent and Workflow package imports stable during migration.
"""

from lazymind.document_tools.resources import sync_writer_documents  # noqa: F401
from lazymind.document_tools.toolkits import (  # noqa: F401
    DraftMarkdownStreamEventEmitter,
    WriterCreateToolkit,
    WriterResourceToolkit,
    WriterRevisionToolkit,
    WriterToolkitBase,
    writer_schema,
)

__all__ = [
    'DraftMarkdownStreamEventEmitter',
    'WriterCreateToolkit',
    'WriterResourceToolkit',
    'WriterRevisionToolkit',
    'WriterToolkitBase',
    'sync_writer_documents',
    'writer_schema',
]
