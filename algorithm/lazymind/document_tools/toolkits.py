"""Toolkit composition and legacy compatibility exports."""

from __future__ import annotations

from .artifacts import (
    WRITER_BLOCK_SCHEMA,
    WRITER_DATA_MODEL_SCHEMA_PREFIX,
    WRITER_IR_SCHEMA,
    WriterArtifactCapabilities,
    writer_schema,
)
from .resources import (
    WriterResourceCapabilities,
    sync_writer_documents,
)
from .revision import WriterRevisionCapabilities
from .writing import (
    DraftMarkdownStreamEventEmitter,
    WriterWritingCapabilities,
)


class WriterCreateToolkit(WriterWritingCapabilities, WriterArtifactCapabilities):
    """Curated Chat Agent toolkit for document creation."""

    __public_apis__ = [
        "build_writing_task",
        "build_resources",
        "profile_resources",
        "create_writing_context",
        "prepare_outline",
        "generate_outline",
        "generate_rewrite_outline",
        "generate_rewrite_section_instructions",
        "generate_section_instructions",
        "generate_draft_section",
        "generate_draft_section_markdown",
        "generate_draft_blocks",
        "generate_draft_blocks_markdown",
        "generate_draft_document",
        "generate_draft_document_markdown",
        "update_writing_context",
        "check_consistency",
        "generate_final_document",
        "render_markdown",
    ]


class WriterRevisionToolkit(WriterRevisionCapabilities):
    """Curated Chat Agent toolkit for document revision."""

    __public_apis__ = [
        "build_revise_task",
        "build_revision_task",
        "locate_revision_target",
        "generate_modify_plan",
        "build_revision_visual_plan",
        "generate_patch_set",
        "generate_string_replace_set",
        "plan_revision",
        "validate_patch_set",
        "apply_patch",
        "apply_string_replace",
        "apply_revision",
    ]


class WriterResourceToolkit(WriterResourceCapabilities):
    """Curated capability toolkit for external document resources."""

    __public_apis__ = [
        "load_document",
        "create_document",
        "publish_revision",
        "convert_document",
        "write_document",
    ]


DocumentWritingToolkit = WriterCreateToolkit
DocumentRevisionToolkit = WriterRevisionToolkit
DocumentResourceToolkit = WriterResourceToolkit


class WriterToolkitBase(
    WriterWritingCapabilities,
    WriterRevisionCapabilities,
    WriterResourceCapabilities,
    WriterArtifactCapabilities,
):
    """Legacy aggregate. New code should use a concrete capability toolkit."""

    WRITER_IR_SCHEMA = WRITER_IR_SCHEMA
    WRITER_BLOCK_SCHEMA = WRITER_BLOCK_SCHEMA
    __public_apis__: list[str] = []


__all__ = [
    "DocumentResourceToolkit",
    "DocumentRevisionToolkit",
    "DocumentWritingToolkit",
    "DraftMarkdownStreamEventEmitter",
    "WRITER_BLOCK_SCHEMA",
    "WRITER_DATA_MODEL_SCHEMA_PREFIX",
    "WRITER_IR_SCHEMA",
    "WriterCreateToolkit",
    "WriterResourceToolkit",
    "WriterRevisionToolkit",
    "WriterToolkitBase",
    "sync_writer_documents",
    "writer_schema",
]
