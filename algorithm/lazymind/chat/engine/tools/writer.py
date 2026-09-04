"""Compatibility imports for the shared document toolkits.

New code should import from :mod:`lazymind.document_tools`.  This module keeps
existing Chat Agent and Workflow package imports stable during migration.
"""

from lazymind.document_tools.resources import WriterResourceToolkit  # noqa: F401
from lazymind.document_tools.revision import (  # noqa: F401
    WriterRevisionToolkit,
    sync_writer_documents,
)
from lazymind.document_tools.toolkits import WriterToolkitBase, writer_schema  # noqa: F401
from lazymind.document_tools.writing import (  # noqa: F401
    DraftMarkdownStreamEventEmitter,
    WriterCreateToolkit,
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
