"""Outline, drafting, and final-document capabilities."""

from .toolkits import DraftMarkdownStreamEventEmitter, WriterToolkitBase


class WriterCreateToolkit(WriterToolkitBase):
    """Create long-form writing from source profiling through final output."""

    __public_apis__ = [
        'build_writing_task', 'build_resources', 'profile_resources',
        'create_writing_context', 'prepare_outline', 'generate_outline',
        'generate_rewrite_outline', 'generate_rewrite_section_instructions',
        'generate_section_instructions', 'generate_draft_section',
        'generate_draft_section_markdown',
        'generate_draft_blocks', 'generate_draft_blocks_markdown',
        'generate_draft_document', 'generate_draft_document_markdown',
        'update_writing_context', 'check_consistency',
        'generate_final_document', 'render_markdown',
    ]


DocumentWritingToolkit = WriterCreateToolkit

__all__ = [
    'DocumentWritingToolkit',
    'DraftMarkdownStreamEventEmitter',
    'WriterCreateToolkit',
]
