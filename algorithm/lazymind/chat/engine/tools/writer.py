"""Compatibility imports for the shared document toolkits.

New code should import from :mod:`lazymind.document_tools`.  This module keeps
existing Chat Agent and Workflow package imports stable during migration.
"""

from lazyllm.tools.agent import ToolExecutionError  # noqa: F401
from lazyllm.tools.writer.provider.wechat import (  # noqa: F401
    prepare_wechat_cover as _prepare_wechat_cover,
)
from lazyllm.tools.writer.tools import WriterResourceTools  # noqa: F401
from lazymind.document_tools.resources import (  # noqa: F401
    _published_link,
    sync_writer_documents,
)
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
    'ToolExecutionError',
    'WriterCreateToolkit',
    'WriterResourceTools',
    'WriterResourceToolkit',
    'WriterRevisionToolkit',
    'WriterToolkitBase',
    '_prepare_wechat_cover',
    '_published_link',
    'sync_writer_documents',
    'writer_schema',
]
