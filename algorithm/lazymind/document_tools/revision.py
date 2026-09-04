"""AI rewrite and validated document patch capabilities."""

from .toolkits import WriterToolkitBase, sync_writer_documents


class WriterRevisionToolkit(WriterToolkitBase):
    """Revise documents through a validated structured patch workflow."""

    __public_apis__ = [
        'build_revise_task', 'build_revision_task', 'locate_revision_target',
        'generate_modify_plan', 'build_revision_visual_plan', 'generate_patch_set',
        'generate_string_replace_set',
        'plan_revision', 'validate_patch_set', 'apply_patch',
        'apply_string_replace', 'apply_revision',
    ]


DocumentRevisionToolkit = WriterRevisionToolkit

__all__ = [
    'DocumentRevisionToolkit',
    'WriterRevisionToolkit',
    'sync_writer_documents',
]
