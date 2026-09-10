"""Typed, versioned document Actions used by Workflow hosts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DocumentActionPhase = Literal['preview', 'execute']
DocumentAction = Callable[..., Any]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class IRSelection(_StrictModel):
    type: Literal['ir']
    node_id: str = Field(min_length=1)


class MarkdownSelection(_StrictModel):
    type: Literal['markdown']
    selected_text: str = Field(min_length=1)


class RewriteSelectionPreviewArguments(_StrictModel):
    instruction: str = Field(min_length=1)
    selection: IRSelection | MarkdownSelection = Field(discriminator='type')

    @field_validator('instruction')
    @classmethod
    def instruction_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('instruction must not be blank')
        return value


class RewriteSelectionExecuteArguments(_StrictModel):
    commit_token: str = Field(pattern=r'^[0-9a-f]{32}$')


class RenderDocumentArguments(_StrictModel):
    pass


class SaveDocumentArguments(_StrictModel):
    base_artifact: Any
    numbering_update: dict[str, Any] | None = None


class SyncDocumentArguments(_StrictModel):
    source_document: dict[str, Any]
    revised_document: dict[str, Any]
    media_assets: dict[str, Any] | None = None


class ConvertDocumentArguments(_StrictModel):
    provider: str = ''
    output_format: Literal['native', 'markdown', 'latex', 'text'] = 'native'
    document: str | dict[str, Any] | None = None
    target_document: dict[str, Any] | None = None
    media_assets: dict[str, Any] | None = None
    template: str = ''


class WriteDocumentArguments(_StrictModel):
    converted_document: dict[str, Any]
    target_document: dict[str, Any] | None = None
    media_assets: dict[str, Any] | None = None
    title: str = ''
    parent_uri: str = ''
    mode: Literal['replace', 'append'] = 'replace'


class ActionArtifact(_StrictModel):
    content_type: str = Field(min_length=1)
    value: Any
    caption: str | None = None


class RewriteTarget(_StrictModel):
    type: Literal['block']
    block_type: str
    node_id: str | None = None


class RewritePreview(_StrictModel):
    old_text: str
    new_text: str


class RewritePatch(_StrictModel):
    type: Literal['writer_ir_patch', 'string_replace_set']
    payload: dict[str, Any]


class CommitReference(_StrictModel):
    token: str = Field(pattern=r'^[0-9a-f]{32}$')


class RewriteSelectionPreviewResult(_StrictModel):
    representation: Literal['ir', 'markdown']
    target: RewriteTarget
    preview: RewritePreview
    patch: RewritePatch
    artifact: ActionArtifact
    commit: CommitReference


class RewriteSelectionExecuteResult(_StrictModel):
    representation: Literal['ir', 'markdown']
    artifact: ActionArtifact


class RenderDocumentResult(_StrictModel):
    title: str
    representation: Literal['ir', 'markdown']
    document: Any
    numbering: dict[str, Any]
    export_document: str | None = None


class SaveDocumentResult(RenderDocumentResult):
    source_document: Any


class SyncDocumentResult(_StrictModel):
    success: bool
    changed: bool
    provider_synced: bool
    patch_result: dict[str, Any]
    persisted_document: Any = None
    patch_set: dict[str, Any] | None = None
    representation: str | None = None
    provider: str | None = None
    write_result: dict[str, Any] | None = None
    target_document: dict[str, Any] | None = None


class ConvertDocumentResult(_StrictModel):
    provider: str
    format: str
    content: Any
    source_document: dict[str, Any]
    media_references: dict[str, str]


class DocumentActionContext(_StrictModel):
    artifact: Any = None
    artifact_store: str = ''
    slot: str = ''


@dataclass(frozen=True, slots=True)
class DocumentActionSpec:
    """One immutable phase of a versioned backend-facing Action contract."""

    reference: str
    action: str
    version: int
    phase: DocumentActionPhase
    arguments_model: type[BaseModel]
    result_model: type[BaseModel]
    handler: DocumentAction
    durable_side_effects: bool = False
    external_side_effects: bool = False


class DocumentActionError(Exception):
    """A structured Action failure safe to forward through the API boundary."""

    def __init__(self, code: str, message: str, status_code: int,
                 retryable: bool = False,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(code, message, status_code, retryable, details)
        self.message = message
        self.error_code = code
        self.status_code = status_code
        self.retryable = retryable
        self.details = dict(details or {})

    def __str__(self) -> str:
        return self.message


_LEGACY_ACTIONS: dict[str, dict[DocumentActionPhase, DocumentAction]] = {}
_BUILTIN_ACTIONS: dict[str, dict[DocumentActionPhase, DocumentActionSpec]] = {}
_BUILTIN_REF_RE = re.compile(
    r'^builtin:document\.(?P<action>[a-z][a-z0-9_]*)\.v(?P<version>[1-9][0-9]*)$'
)


def register_document_action(name: str, phase: DocumentActionPhase,
                             handler: DocumentAction) -> None:
    """Register a compatibility Action without permitting replacement."""
    action = name.strip()
    if not action:
        raise ValueError('document action name must not be empty')
    if phase not in {'preview', 'execute'}:
        raise ValueError(f'unsupported document action phase: {phase!r}')
    if not callable(handler):
        raise TypeError('document action handler must be callable')
    phases = _LEGACY_ACTIONS.setdefault(action, {})
    if phase in phases:
        raise ValueError(
            f'document action {action!r} phase {phase!r} is already registered'
        )
    phases[phase] = handler


def _register_builtin(spec: DocumentActionSpec) -> None:
    match = _BUILTIN_REF_RE.fullmatch(spec.reference)
    if (match is None or match.group('action') != spec.action
            or int(match.group('version')) != spec.version):
        raise ValueError(f'invalid document action reference: {spec.reference!r}')
    phases = _BUILTIN_ACTIONS.setdefault(spec.reference, {})
    if spec.phase in phases:
        raise ValueError(
            f'document action {spec.reference!r} phase {spec.phase!r} is already registered'
        )
    if spec.phase == 'preview' and (
        spec.durable_side_effects or spec.external_side_effects
    ):
        raise ValueError('preview document actions must be side-effect-free')
    phases[spec.phase] = spec


def resolve_document_action(reference: str,
                            phase: DocumentActionPhase) -> DocumentActionSpec:
    """Resolve an exact built-in reference and supported phase."""
    if _BUILTIN_REF_RE.fullmatch(reference) is None:
        raise DocumentActionError(
            'DOCUMENT_ACTION_REFERENCE_INVALID',
            f'Invalid built-in document Action reference: {reference!r}.',
            status_code=422,
        )
    spec = _BUILTIN_ACTIONS.get(reference, {}).get(phase)
    if spec is None:
        raise DocumentActionError(
            'DOCUMENT_ACTION_UNAVAILABLE',
            f'Built-in document Action {reference!r} does not support {phase!r}.',
            status_code=422,
        )
    return spec


def get_document_action(name: str,
                        phase: DocumentActionPhase) -> DocumentAction | None:
    """Return a compatibility handler or an exact built-in handler."""
    if name.startswith('builtin:'):
        spec = _BUILTIN_ACTIONS.get(name, {}).get(phase)
        return spec.handler if spec else None
    return _LEGACY_ACTIONS.get(name.strip(), {}).get(phase)


def document_action_names() -> tuple[str, ...]:
    """Return all registered names and references in deterministic order."""
    return tuple(sorted((*_LEGACY_ACTIONS, *_BUILTIN_ACTIONS)))


def document_action_specs(
) -> Mapping[str, Mapping[DocumentActionPhase, DocumentActionSpec]]:
    """Expose a read-only snapshot of the versioned built-in registry."""
    return MappingProxyType({
        reference: MappingProxyType(dict(phases))
        for reference, phases in _BUILTIN_ACTIONS.items()
    })


def invoke_document_action(reference: str, phase: DocumentActionPhase,
                           arguments: Mapping[str, Any], *, artifact: Any = None,
                           artifact_store: str = '', slot: str = '',
                           action: str = '') -> dict[str, Any]:
    """Validate caller input, execute a built-in, and validate its result."""
    spec = resolve_document_action(reference, phase)
    if action and spec.action != action:
        raise DocumentActionError(
            'DOCUMENT_ACTION_REFERENCE_INVALID',
            f'Built-in reference {reference!r} cannot handle Action {action!r}.',
            status_code=422,
        )
    try:
        parsed = spec.arguments_model.model_validate(dict(arguments))
    except ValidationError as exc:
        raise DocumentActionError(
            'WORKFLOW_ACTION_INVALID',
            'Document Action arguments do not match the registered contract.',
            status_code=422,
            details={'errors': _validation_errors(exc)},
        ) from exc
    context = DocumentActionContext(
        artifact=artifact, artifact_store=artifact_store, slot=slot
    )
    try:
        call_arguments = {
            name: getattr(parsed, name) for name in type(parsed).model_fields
        }
        result = spec.handler(**call_arguments, context=context)
    except DocumentActionError:
        raise
    except (TypeError, ValueError) as exc:
        code = str(getattr(exc, 'error_code', 'WORKFLOW_ACTION_INVALID'))
        status, retryable = _error_policy(code, 422)
        raise DocumentActionError(
            code, str(exc), status_code=status, retryable=retryable,
            details=getattr(exc, 'details', None),
        ) from exc
    except Exception as exc:
        code = str(
            getattr(exc, 'error_code', '')
            or getattr(exc, 'code', '')
            or 'WORKFLOW_ACTION_FAILED'
        )
        raw_status = int(getattr(exc, 'status_code', 0) or 0)
        status, retryable = _error_policy(
            code, raw_status if 400 <= raw_status <= 599 else 502
        )
        raise DocumentActionError(
            code, str(exc), status_code=status,
            retryable=bool(getattr(exc, 'retryable', retryable)),
            details=getattr(exc, 'details', None),
        ) from exc
    try:
        validated = spec.result_model.model_validate(result)
    except ValidationError as exc:
        raise DocumentActionError(
            'WORKFLOW_ACTION_RESULT_INVALID',
            'Document Action returned a result outside its registered contract.',
            status_code=502,
            details={'errors': _validation_errors(exc)},
        ) from exc
    return validated.model_dump(exclude_none=True)


def _validation_errors(error: ValidationError) -> list[dict[str, Any]]:
    """Return JSON-safe Pydantic diagnostics for the HTTP error envelope."""
    return json.loads(json.dumps(
        error.errors(include_url=False), ensure_ascii=False, default=str
    ))


def _error_policy(code: str, default_status: int) -> tuple[int, bool]:
    if code in {'SELECTION_AMBIGUOUS', 'SELECTION_STALE',
                'ARTIFACT_CONFLICT', 'REVISION_CONFLICT'}:
        return 409, False
    if code.endswith(('ACCOUNT_REQUIRED', 'AUTH_REQUIRED', 'CREDENTIAL_REQUIRED')):
        return 401, False
    if 'PERMISSION' in code or code.endswith('FORBIDDEN'):
        return 403, False
    if code == 'PROVIDER_CAPABILITY_UNSUPPORTED':
        return 422, False
    if code == 'PROVIDER_WRITE_OUTCOME_AMBIGUOUS':
        return 502, False
    return default_status, False


def _artifact_data(value: Any) -> Any:
    from .artifacts import _read_artifact_data

    if isinstance(value, Mapping):
        if 'data' in value:
            return value['data']
        nested = value.get('value')
        if isinstance(nested, Mapping) and isinstance(nested.get('path'), str):
            return _read_artifact_data(nested['path'])
        if isinstance(value.get('path'), str):
            return _read_artifact_data(value['path'])
        return dict(value)
    if isinstance(value, str):
        candidate = Path(value)
        try:
            if candidate.is_file():
                return _read_artifact_data(value)
        except OSError:
            pass
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed.get('data') if isinstance(parsed, dict) and 'data' in parsed else parsed
    raise TypeError('artifact must be a JSON value, Markdown string, or file reference')


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _rewrite_store(context: DocumentActionContext) -> Path:
    base = (Path(context.artifact_store) if context.artifact_store else
            Path(tempfile.gettempdir()) / 'lazymind-document-actions')
    root = base / 'rewrite-selection-v1'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _artifact_payload(document: Any, representation: str,
                      title: str = '') -> dict[str, Any]:
    return {
        'content_type': 'text' if representation == 'markdown' else 'json',
        'value': document,
        'caption': title or None,
    }


def _rewrite_preview(instruction: str,
                     selection: IRSelection | MarkdownSelection, *,
                     context: DocumentActionContext) -> dict[str, Any]:
    from .revision import preview_selection_rewrite

    document = _artifact_data(context.artifact)
    if context.slot not in {'outline_document', 'flat_draft_document',
                            'draft_document'}:
        raise ValueError('selection rewrite is not enabled for this document slot')
    root = _rewrite_store(context)
    result = preview_selection_rewrite(
        document, instruction, selection.model_dump(),
        {
            'context_id': f'selection-{uuid.uuid4().hex}',
            'doc_id': document.get('document_id') if isinstance(document, dict) else None,
            'meta': {'source': 'rewrite_selection_action'},
        },
        artifact_store=str(root),
        flat_markdown=context.slot == 'flat_draft_document',
    )
    representation = result['representation']
    candidate = (
        result.pop('revised_document').model_dump(exclude_defaults=True)
        if representation == 'ir'
        else Path(result.pop('revised_document_md')).read_text(encoding='utf-8')
    )
    title = str(candidate.get('title') or '') if isinstance(candidate, dict) else ''
    artifact = _artifact_payload(candidate, representation, title)
    token = uuid.uuid4().hex
    manifest_path = root / f'{token}.json'
    temporary_path = root / f'.{token}.{uuid.uuid4().hex}.tmp'
    temporary_path.write_text(json.dumps({
        'source_hash': _canonical_hash(document),
        'candidate_hash': _canonical_hash(candidate),
        'representation': representation,
        'artifact': artifact,
    }, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary_path, manifest_path)
    return {**result, 'artifact': artifact, 'commit': {'token': token}}


def _rewrite_execute(commit_token: str, *,
                     context: DocumentActionContext) -> dict[str, Any]:
    manifest_path = _rewrite_store(context) / f'{commit_token}.json'
    if not manifest_path.is_file():
        raise DocumentActionError(
            'SELECTION_STALE', 'The rewrite preview expired; generate a new preview.',
            status_code=409,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocumentActionError(
            'SELECTION_STALE', 'The rewrite preview is invalid.', status_code=409
        ) from exc
    current = _artifact_data(context.artifact)
    artifact = manifest.get('artifact')
    candidate = artifact.get('value') if isinstance(artifact, dict) else None
    if (_canonical_hash(current) != manifest.get('source_hash')
            or _canonical_hash(candidate) != manifest.get('candidate_hash')):
        raise DocumentActionError(
            'SELECTION_STALE', 'The document changed after the rewrite preview.',
            status_code=409,
        )
    return {'representation': manifest.get('representation'), 'artifact': artifact}


def _render(*, context: DocumentActionContext) -> dict[str, Any]:
    from .artifacts import render_document
    return render_document(_artifact_data(context.artifact))


def _save(base_artifact: Any,
          numbering_update: dict[str, Any] | None = None, *,
          context: DocumentActionContext) -> dict[str, Any]:
    from .artifacts import save_document
    return save_document(_artifact_data(context.artifact),
                         _artifact_data(base_artifact), numbering_update)


def _sync(*, context: DocumentActionContext, **arguments: Any) -> dict[str, Any]:
    from .resources import sync_document
    return sync_document(artifact_store=context.artifact_store, **arguments)


def _convert(*, context: DocumentActionContext, **arguments: Any) -> dict[str, Any]:
    from .resources import convert_document
    snapshot = arguments.pop('document', None)
    if snapshot is not None:
        if arguments.get('output_format', 'native') == 'native':
            raise ValueError('Editor snapshots are only supported for portable conversions.')
        # The snapshot is content, never a file locator supplied by the client.
        source = snapshot
    else:
        source = _artifact_data(context.artifact)
    return convert_document(source, **arguments)


def _write(*, context: DocumentActionContext, **arguments: Any) -> dict[str, Any]:
    del context
    from .resources import write_document
    return write_document(**arguments)


def _install_builtins() -> None:
    definitions = (
        DocumentActionSpec(
            'builtin:document.rewrite_selection.v1', 'rewrite_selection', 1,
            'preview', RewriteSelectionPreviewArguments,
            RewriteSelectionPreviewResult, _rewrite_preview,
        ),
        DocumentActionSpec(
            'builtin:document.rewrite_selection.v1', 'rewrite_selection', 1,
            'execute', RewriteSelectionExecuteArguments,
            RewriteSelectionExecuteResult, _rewrite_execute,
        ),
        DocumentActionSpec(
            'builtin:document.render_document.v1', 'render_document', 1,
            'preview', RenderDocumentArguments, RenderDocumentResult, _render,
        ),
        DocumentActionSpec(
            'builtin:document.render_document.v1', 'render_document', 1,
            'execute', RenderDocumentArguments, RenderDocumentResult, _render,
        ),
        DocumentActionSpec(
            'builtin:document.save_document.v1', 'save_document', 1,
            'execute', SaveDocumentArguments, SaveDocumentResult, _save,
            durable_side_effects=True,
        ),
        DocumentActionSpec(
            'builtin:document.sync_document.v1', 'sync_document', 1,
            'execute', SyncDocumentArguments, SyncDocumentResult, _sync,
            durable_side_effects=True, external_side_effects=True,
        ),
        DocumentActionSpec(
            'builtin:document.convert_document.v1', 'convert_document', 1,
            'preview', ConvertDocumentArguments, ConvertDocumentResult, _convert,
        ),
        DocumentActionSpec(
            'builtin:document.convert_document.v1', 'convert_document', 1,
            'execute', ConvertDocumentArguments, ConvertDocumentResult, _convert,
        ),
        DocumentActionSpec(
            'builtin:document.write_document.v1', 'write_document', 1,
            'execute', WriteDocumentArguments, SyncDocumentResult, _write,
            durable_side_effects=True, external_side_effects=True,
        ),
    )
    for definition in definitions:
        _register_builtin(definition)


_install_builtins()


__all__ = [
    'ActionArtifact', 'DocumentAction', 'DocumentActionContext',
    'DocumentActionError', 'DocumentActionPhase', 'DocumentActionSpec',
    'ConvertDocumentArguments', 'ConvertDocumentResult',
    'RenderDocumentArguments', 'RenderDocumentResult',
    'RewriteSelectionExecuteArguments', 'RewriteSelectionExecuteResult',
    'RewriteSelectionPreviewArguments', 'RewriteSelectionPreviewResult',
    'SaveDocumentArguments', 'SaveDocumentResult', 'SyncDocumentArguments',
    'SyncDocumentResult', 'WriteDocumentArguments', 'document_action_names', 'document_action_specs',
    'get_document_action', 'invoke_document_action', 'register_document_action',
    'resolve_document_action',
]
