"""Provider-neutral document loading, creation, and write-back capabilities."""

from __future__ import annotations

from lazyllm.tools.writer.data_models import TargetDocument, WriterDocument

from .toolkits import (
    WriterToolkitBase,
    _extract_provider_resources,
    _provider_target,
    _provider_targets,
    _resolve_target,
)


class WriterResourceToolkit(WriterToolkitBase):
    """Load and persist MD/LMD documents through provider-neutral tools."""

    __public_apis__ = [
        'load_document', 'create_document', 'publish_revision',
        'replace_document', 'append_document',
    ]


DocumentResourceToolkit = WriterResourceToolkit


def resolve_provider_targets(
    user_input: str,
    *,
    stage: str | None = None,
) -> list[TargetDocument]:
    """Resolve every supported provider locator found in user input."""
    return _provider_targets(user_input, stage=stage)


def resolve_provider_target(
    user_input: str,
    *,
    stage: str | None = None,
) -> TargetDocument:
    """Resolve exactly one provider locator from user input."""
    return _provider_target(user_input, stage=stage)


def extract_provider_resources(user_input: str) -> list[dict]:
    """Convert provider locators to normalized Writer input resources."""
    return _extract_provider_resources(user_input)


def resolve_document_target(
    source_document: WriterDocument | None = None,
    target_document_json: str = '',
    target_uri: str = '',
) -> TargetDocument | None:
    """Resolve an explicit target over a source document's existing binding."""
    return _resolve_target(source_document, target_document_json, target_uri)


__all__ = [
    'DocumentResourceToolkit',
    'WriterResourceToolkit',
    'extract_provider_resources',
    'resolve_document_target',
    'resolve_provider_target',
    'resolve_provider_targets',
]
