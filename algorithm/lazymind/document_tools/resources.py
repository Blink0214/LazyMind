"""Provider-neutral document loading, creation, and write-back."""

from __future__ import annotations
import json
import logging
import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from lazyllm.tools.agent import ToolExecutionError
from lazyllm.tools.writer.data_models import (
    MediaAssetLibrary,
    PatchResult,
    PatchSet,
    TargetDocument,
    WriterDocument,
)
from lazyllm.tools.writer.provider import (
    get_writer_provider,
    match_writer_provider,
    resolve_writer_create_target,
)
from lazyllm.tools.writer.tools import WriterResourceTools, WriterRevisionTools
from lazyllm.tools.writer.tools.revision_tools import apply_patch_to_ir
from lazyllm.tools.writer.utils import parse_document_markdown
from .artifacts import (
    WRITER_BLOCK_SCHEMA,
    WRITER_IR_SCHEMA,
    _document_text,
    _document_value,
    _json_dumps,
    _json_loads,
    _primary_data,
    _read_artifact_data,
    _result_data,
    _set_document_editable,
    _temp_root,
)

_PROVIDER_LOCATOR_RE = re.compile(
    r"(?:https?://|[a-z][a-z0-9_+.-]*:(?://)?)[^\s<>\"'，。；！？、（）【】《》「」『』]+",
    re.IGNORECASE,
)
_WECHAT_COVER_SIZE = (900, 383)
LOG = logging.getLogger(__name__)


def _merge_provider_state(
    document: WriterDocument,
    persisted: WriterDocument,
) -> WriterDocument:
    merged = document.model_copy(deep=True)
    local_blocks = list(merged.iter_blocks())
    persisted_blocks = list(persisted.iter_blocks())
    if len(local_blocks) != len(persisted_blocks):
        raise ToolExecutionError(
            "Provider document block count does not match the written WriterDocument."
        )
    for local, remote in zip(local_blocks, persisted_blocks):
        if local.type != remote.type:
            raise ToolExecutionError(
                "Provider document block order does not match the written WriterDocument."
            )
        local.provider_binding = deepcopy(remote.provider_binding)
        local.provider_payload = deepcopy(remote.provider_payload)
        local.editable = remote.editable
    merged.revision = persisted.revision
    merged.provider_binding = deepcopy(persisted.provider_binding)
    return merged


def sync_writer_documents(
    source_value: Any,
    revised_value: Any,
    media_assets: Any = None,
    artifact_store: str = "",
) -> dict[str, Any]:
    """Persist one WriterDocument delta and bind its semantic IR to the provider."""
    source = WriterDocument.model_validate(source_value)
    revised = WriterDocument.model_validate(revised_value)
    if source.document_id != revised.document_id:
        raise ToolExecutionError("WriterDocument document_id values must match.")
    for field in ("stage", "revision", "provider_binding"):
        if getattr(source, field) != getattr(revised, field):
            raise ToolExecutionError(f"WriterDocument {field} values must match.")
    for source_block in source.iter_blocks():
        revised_block = revised.block_by_id(source_block.node_id)
        if revised_block is None:
            continue
        revised_block.provider_binding = deepcopy(source_block.provider_binding)
        revised_block.provider_payload = deepcopy(source_block.provider_payload)
        revised_block.editable = source_block.editable

    library = MediaAssetLibrary.model_validate(media_assets) if media_assets else None
    root = Path(artifact_store) if artifact_store else _temp_root()
    root.mkdir(parents=True, exist_ok=True)
    if source.title == revised.title and source.blocks == revised.blocks:
        patch = PatchSet(
            patch_id=f"patch-{source.document_id}",
            target_doc_id=source.document_id,
        )
    else:
        revision = WriterRevisionTools(llm=None, artifact_store=str(root))
        patch = PatchSet.model_validate(
            _primary_data(
                revision.build_patch_set_from_documents(source, revised, library),
            )
        )
    changed = bool(patch.hunks or patch.new_title is not None)
    if changed:
        output = WriterResourceTools(
            llm=None,
            artifact_store=str(root),
        ).apply_patch_to_document(patch, source, media_assets=library)
        persisted = WriterDocument.model_validate(
            _result_data(output, "persisted_document")
        )
        result = PatchResult.model_validate(_result_data(output, "patch_result"))
    else:
        persisted = source
        result = PatchResult(
            patch_id=patch.patch_id,
            success=True,
            message="No document changes.",
        )
    candidate = _merge_provider_state(revised, persisted)
    candidate.ui_editable = True
    return {
        "success": result.success,
        "changed": changed,
        "provider_synced": result.success,
        "patch_set": patch.model_dump(),
        "patch_result": result.model_dump(),
        "persisted_document": candidate.model_dump(),
    }


def sync_document(
    source_document: Mapping[str, Any] | None = None,
    revised_document: Mapping[str, Any] | None = None,
    media_assets: Mapping[str, Any] | None = None,
    markdown_content: str = "",
    target_document: Mapping[str, Any] | None = None,
    title: str = "",
    artifact_store: str = "",
    adapter: str = "",
) -> dict[str, Any]:
    """Synchronize a bound revision or explicitly publish an unbound document."""
    if markdown_content:
        markdown = markdown_content.strip()
        if not markdown:
            raise ValueError("Markdown draft is empty.")
        heading = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
        document_title = (
            heading.group(1).strip() if heading else title.strip()
        ) or "未命名文档"
        return _replace_document_and_read_back(
            markdown_content,
            title=document_title,
            target_document=target_document,
            media_assets=media_assets,
            source_format="markdown",
            adapter=adapter,
        )
    if revised_document is None:
        raise ValueError("revised_document is required for IR sync.")
    if source_document is not None:
        return sync_writer_documents(
            source_document,
            revised_document,
            media_assets,
            artifact_store,
        )
    document = WriterDocument.model_validate(revised_document)
    return _replace_document_and_read_back(
        document,
        title=document.title,
        target_document=target_document,
        media_assets=media_assets,
        source_format="lmd",
        adapter=adapter,
    )


def _replace_document_and_read_back(
    content: str | WriterDocument,
    *,
    title: str,
    source_format: str,
    target_document: Mapping[str, Any] | None = None,
    media_assets: Mapping[str, Any] | None = None,
    adapter: str = "",
) -> dict[str, Any]:
    if target_document:
        target = TargetDocument.model_validate(target_document)
    else:
        created = _json_loads(
            WriterResourceCapabilities().create_document(
                title=title.strip() or "未命名文档",
                adapter=adapter,
            ),
            {},
        )
        target = TargetDocument.model_validate(created)

    media_library = (
        MediaAssetLibrary.model_validate(media_assets) if media_assets else None
    )
    if isinstance(content, WriterDocument):
        publish_content = content.model_copy(deep=True)
    elif str(target.adapter or "").strip().lower() == "github":
        publish_content = content
    else:
        publish_content = parse_document_markdown(
            content,
            document_id=f"writer-document-{uuid.uuid4()}",
            stage="final",
            media_assets=media_library,
        )
        if media_library is not None:
            for block in publish_content.iter_blocks():
                if block.type != "image":
                    continue
                for reference in block.references:
                    asset = media_library.assets.get(reference.get("id"))
                    if asset is not None and asset.uri:
                        reference.setdefault("path", asset.uri)
    serialized_content = (
        json.dumps(publish_content.model_dump(), ensure_ascii=False)
        if isinstance(publish_content, WriterDocument)
        else publish_content
    )
    payload = _json_loads(
        WriterResourceCapabilities().replace_document(
            content_json=serialized_content,
            source_document_json=serialized_content,
            target_document_json=json.dumps(target.model_dump(), ensure_ascii=False),
            target_title=title,
            media_assets_json=(
                json.dumps(media_library.model_dump(), ensure_ascii=False)
                if media_library is not None
                else ""
            ),
        ),
        {},
    )
    write_result = payload.get("publish_result") or {}
    persisted = payload.get("draft_document")
    if isinstance(persisted, dict):
        persisted_document = WriterDocument.model_validate(persisted)
        persisted_document.ui_editable = True
        persisted = persisted_document.model_dump()
    result = PatchResult(
        success=True,
        message=(
            "Document written to GitHub successfully."
            if payload.get("provider") == "github"
            else "Document written to provider and read back successfully."
        ),
        meta={
            "mode": "replace",
            "source_format": source_format,
            "write_result": write_result,
        },
    )
    return {
        "success": True,
        "changed": True,
        "provider_synced": True,
        "patch_result": result.model_dump(),
        "persisted_document": persisted,
        "representation": payload.get("representation"),
        "provider": payload.get("provider"),
        "write_result": write_result,
        "target_document": payload.get("target_document"),
    }


def _provider_targets(
    user_input: str, *, stage: str | None = None
) -> list[TargetDocument]:
    targets: list[TargetDocument] = []
    seen: set[str] = set()
    for match in _PROVIDER_LOCATOR_RE.finditer(user_input or ""):
        locator = match.group(0).rstrip(").,;!?]}，。；！？】》」』")
        if locator in seen:
            continue
        try:
            target = match_writer_provider(locator).resolve(locator)
        except ValueError:
            continue
        seen.add(locator)
        if stage is not None:
            target.meta = {**target.meta, "stage": stage}
        targets.append(target)
    return targets


def find_provider_locator(user_input: str) -> str:
    """Return the first locator handled by a registered Writer provider."""
    for match in _PROVIDER_LOCATOR_RE.finditer(user_input or ""):
        locator = match.group(0).rstrip(").,;!?]}，。；！？】》」』")
        try:
            match_writer_provider(locator)
        except ValueError:
            continue
        return locator
    return ""


def _provider_target(user_input: str, *, stage: str | None = None) -> TargetDocument:
    targets = _provider_targets(user_input, stage=stage)
    if not targets:
        raise ToolExecutionError("A supported provider document locator is required.")
    if len(targets) > 1:
        raise ToolExecutionError("Exactly one provider document locator is required.")
    return targets[0]


def _source_document_target(
    user_input: str, *, stage: str = "final"
) -> TargetDocument:
    targets = _provider_targets(user_input, stage=stage)
    if len(targets) > 1:
        raise ToolExecutionError("Exactly one provider document locator is required.")
    if targets:
        return targets[0]
    try:
        provider = match_writer_provider(user_input)
        target = provider.resolve(user_input)
    except ValueError as exc:
        raise ToolExecutionError(
            "A supported provider document locator is required."
        ) from exc
    target.meta = {**target.meta, "stage": stage}
    return target


def _provider_create_target(user_input: str) -> tuple[str, TargetDocument] | None:
    for match in _PROVIDER_LOCATOR_RE.finditer(user_input or ""):
        locator = match.group(0).rstrip(").,;!?]}，。；！？】》」』")
        try:
            target = resolve_writer_create_target(locator)
        except ValueError:
            continue
        if target.meta.get("create_pending"):
            return locator, target
    return None


def _extract_provider_resources(user_input: str) -> list[dict]:
    resources: list[dict] = []
    for idx, target in enumerate(_provider_targets(user_input)):
        provider = str(target.adapter or "")
        resources.append(
            {
                "resource_id": f"{provider}_{idx}",
                "resource_type": "url",
                "uri": target.uri,
                "title": None,
                "mime_type": None,
                "summary": None,
                "meta": {"provider": provider, "role": "background"},
            }
        )
    return resources


def _target_from_document(value: Any) -> TargetDocument | None:
    document = WriterDocument.model_validate(value)
    binding = document.provider_binding
    target = TargetDocument(
        doc_id=binding.get("document_id"),
        uri=binding.get("uri"),
        adapter=binding.get("provider"),
        title=document.title or None,
        meta={
            key: binding[key]
            for key in ("article_index", "thumb_media_id", "browser_url")
            if binding.get(key) is not None
        },
    )
    if target.uri or target.doc_id:
        return target
    source = document.metadata.get("source")
    if not isinstance(source, dict):
        return None
    try:
        target = TargetDocument.model_validate(source)
    except Exception:
        return None
    return target if target.uri or target.doc_id else None


def _published_link(target: TargetDocument) -> str:
    link = str(
        target.meta.get("browser_url")
        or (target.uri if (target.uri or "").startswith(("http://", "https://")) else "")
    ).strip()
    if not link:
        raise ToolExecutionError(
            "Provider write succeeded but no browser URL was returned."
        )
    return link


def _resolve_target(
    source_document: WriterDocument | None = None,
    target_document_json: str = "",
    target_uri: str = "",
) -> TargetDocument | None:
    target = _target_from_document(source_document) if source_document else None
    if target_document_json.strip():
        target = TargetDocument.model_validate(
            _json_loads(target_document_json, {}),
        )
    if target_uri.strip():
        target = _provider_target(target_uri.strip())
    return target


def _prepare_wechat_cover(
    target: TargetDocument,
    document: WriterDocument | str,
    root: Path,
    *,
    model_available: Callable[[str], bool] | None = None,
    generator: Callable[..., dict[str, Any]] | None = None,
) -> TargetDocument:
    if (
        target.adapter != "wechat"
        or target.doc_id
        or target.meta.get("thumb_media_id")
    ):
        return target
    if isinstance(document, WriterDocument):
        binding = document.provider_binding
        if binding.get("provider") == "wechat" and binding.get("document_id"):
            return target
        title = document.title or target.title or "未命名文档"
        body = _document_text(document)
    else:
        title = target.title or "未命名文档"
        body = str(document)

    from PIL import Image, ImageOps

    cover_path = root / "wechat-cover.png"
    try:
        if model_available is None:
            from lazymind.model_config import is_model_role_available

            model_available = is_model_role_available
        if not model_available("image_generator"):
            raise RuntimeError("image_generator is not configured")
        if generator is None:
            from lazymind.chat.engine.tools.multimodal import image_generator

            generator = image_generator
        result = generator(
            "为微信公众号文章生成一张专业、简洁、无文字、无水印的横版封面图。\n"
            f"文章标题：{title}\n文章内容摘要：{body[:1000]}",
            image_size="1024x1024",
            batch_size=1,
        )
        generated_path = Path(str(result.get("local_path") or ""))
        if not generated_path.is_file():
            raise ValueError("image_generator returned no usable local image")
        with Image.open(generated_path) as source:
            cover = ImageOps.fit(
                source.convert("RGB"),
                _WECHAT_COVER_SIZE,
                method=Image.Resampling.LANCZOS,
            )
            cover.save(cover_path, format="PNG")
    except Exception as exc:
        LOG.warning(
            "[Writer] WeChat cover generation failed; using white cover: %s", exc
        )
        Image.new("RGB", _WECHAT_COVER_SIZE, "white").save(cover_path, format="PNG")

    prepared = target.model_copy(deep=True)
    prepared.meta["cover_path"] = str(cover_path)
    return prepared


class WriterResourceCapabilities:
    WRITER_IR_SCHEMA = WRITER_IR_SCHEMA
    WRITER_BLOCK_SCHEMA = WRITER_BLOCK_SCHEMA

    def load_document(self, user_input: str, stage: str = "final") -> str:
        """Load a provider document without changing its Writer representation."""
        if stage not in {"outline", "draft", "final"}:
            raise ToolExecutionError("stage must be outline, draft, or final.")
        root = _temp_root()
        target = _source_document_target(user_input, stage=stage)
        result = WriterResourceTools(
            llm=None,
            artifact_store=str(root),
        ).load_document(target)
        artifact_paths = (result.get("metadata") or {}).get("artifact_paths") or {}
        return _json_dumps(
            {
                "source_document": _primary_data(result),
                "target_document": _result_data(result, "target_document"),
                "representation": result.get("representation"),
                "input_resources": (
                    _result_data(result, "input_resources")
                    if artifact_paths.get("input_resources")
                    else []
                ),
                "resource_warnings": (result.get("metadata") or {}).get("warnings") or [],
            }
        )

    def resolve_create_target(self, user_input: str) -> str:
        """Resolve an optional deferred provider target for a new document."""
        resolved = _provider_create_target(user_input)
        if resolved is None:
            return _json_dumps({})
        locator, target = resolved
        return _json_dumps(
            {
                "target_ref": locator,
                "target_document": target.model_dump(exclude_defaults=True),
            }
        )

    def prepare_markdown_for_editor(
        self, markdown: str, target_document_json: str
    ) -> str:
        """Prepare Markdown through an optional provider capability."""
        target = TargetDocument.model_validate(_json_loads(target_document_json, {}))
        provider_name = str(target.adapter or "").strip()
        if not provider_name:
            return _json_dumps({"markdown": markdown, "target_document": None})
        provider = get_writer_provider(provider_name)
        prepare = getattr(provider, "normalize_code_fences_for_writer", None)
        if not callable(prepare):
            return _json_dumps({"markdown": markdown, "target_document": None})
        original_target = target.model_dump()
        prepared = prepare(markdown, target)
        changed = prepared != markdown or target.model_dump() != original_target
        return _json_dumps(
            {
                "markdown": prepared,
                "target_document": (
                    target.model_dump(exclude_defaults=True) if changed else None
                ),
            }
        )

    def create_document(
        self, title: str, parent_uri: str = "", adapter: str = ""
    ) -> str:
        """Create an empty provider document and return its target binding."""
        root = _temp_root()
        provider = adapter.strip()
        if not provider and parent_uri.strip():
            provider = str(_provider_target(parent_uri.strip()).adapter or "")
        if not provider:
            raise ToolExecutionError(
                "adapter is required when parent_uri cannot identify a provider."
            )
        result = WriterResourceTools(
            llm=None,
            artifact_store=str(root),
        ).create_document(
            title=title.strip() or "未命名文档",
            parent_uri=parent_uri.strip(),
            adapter=provider,
        )
        return _json_dumps(_primary_data(result))

    def publish_revision(
        self,
        source_document_json: str,
        patch_set_json: str,
        media_assets_json: str = "",
    ) -> str:
        """Apply a prepared PatchSet to its bound provider document."""
        root = _temp_root()
        source = WriterDocument.model_validate(
            _json_loads(source_document_json, {}),
        )
        patch = PatchSet.model_validate(_json_loads(patch_set_json, {}))
        media_assets = (
            MediaAssetLibrary.model_validate(_json_loads(media_assets_json, {}))
            if media_assets_json.strip()
            else None
        )
        revised, _ = apply_patch_to_ir(source, patch, media_assets=media_assets)
        target = _target_from_document(source)
        if target is None:
            raise ToolExecutionError(
                "source document must contain a cloud target binding."
            )
        result = WriterResourceTools(
            llm=None,
            artifact_store=str(root),
        ).apply_patch_to_document(
            patch_set=patch,
            source_document=source,
            media_assets=media_assets,
        )
        persisted = WriterDocument.model_validate(
            _result_data(result, "persisted_document")
        )
        published = _set_document_editable(
            _merge_provider_state(revised, persisted),
            stage=source.stage,
        )
        return _json_dumps(
            {
                "publish_result": _primary_data(result),
                "draft_document": published.model_dump(exclude_defaults=True),
                "provider": str(target.adapter or ""),
                "representation": "ir",
                "published_link": _published_link(target),
            }
        )

    def replace_document(
        self,
        content_json: str,
        source_document_json: str,
        target_document_json: str = "",
        target_uri: str = "",
        target_title: str = "",
        media_assets_json: str = "",
    ) -> str:
        """Replace a provider document with the selected Writer IR or Markdown."""
        return self._write_document(
            mode="replace",
            content_json=content_json,
            source_document_json=source_document_json,
            target_document_json=target_document_json,
            target_uri=target_uri,
            target_title=target_title,
            media_assets_json=media_assets_json,
        )

    def append_document(
        self,
        content_json: str,
        target_document_json: str = "",
        target_uri: str = "",
        publish_outline: bool = False,
        media_assets_json: str = "",
    ) -> str:
        """Append Writer IR or Markdown to a provider target."""
        document = _document_value(content_json)
        if (
            isinstance(document, dict)
            and WriterDocument.model_validate(document).stage == "outline"
            and not publish_outline
        ):
            raise ToolExecutionError(
                "Refusing to publish outline IR as the final document. "
                "Set publish_outline=true only for an explicit outline publish.",
            )
        return self._write_document(
            mode="append",
            content_json=content_json,
            source_document_json=content_json,
            target_document_json=target_document_json,
            target_uri=target_uri,
            media_assets_json=media_assets_json,
        )

    def _write_document(
        self,
        *,
        mode: str,
        content_json: str,
        source_document_json: str = "",
        target_document_json: str = "",
        target_uri: str = "",
        target_title: str = "",
        media_assets_json: str = "",
    ) -> str:
        root = _temp_root()
        document = _document_value(content_json)
        source_value = (
            _document_value(source_document_json) if source_document_json else None
        )
        source = (
            WriterDocument.model_validate(source_value)
            if isinstance(source_value, dict)
            else None
        )
        target = _resolve_target(source, target_document_json, target_uri)
        if target is None:
            raise ToolExecutionError("A target provider document is required.")
        if target.meta.get("create_pending") and not target.title and target_title.strip():
            target.title = target_title.strip()
        publish_document = (
            _set_document_editable(document, stage="final")
            if isinstance(document, dict)
            else document
        )
        if not isinstance(publish_document, (WriterDocument, str)):
            raise ToolExecutionError("content_json must contain Writer IR or Markdown.")
        resource = WriterResourceTools(llm=None, artifact_store=str(root))
        media_assets = (
            _json_loads(media_assets_json, {}) if media_assets_json.strip() else None
        )
        if mode == "replace":
            target = _prepare_wechat_cover(target, publish_document, root)
        write_result = (
            resource.replace_document(publish_document, target, media_assets)
            if mode == "replace"
            else resource.append_to_document(publish_document, target, media_assets)
        )
        if str(target.adapter or "").strip().lower() == "github":
            published_result = _primary_data(write_result)
            if published_result.get("success") is not True:
                raise ToolExecutionError(
                    "GitHub did not confirm that the document was written."
                )
            return _json_dumps(
                {
                    "publish_result": published_result,
                    "draft_document": (
                        publish_document.model_dump(exclude_defaults=True)
                        if isinstance(publish_document, WriterDocument)
                        else publish_document
                    ),
                    "representation": (
                        "ir" if isinstance(publish_document, WriterDocument) else "markdown"
                    ),
                    "provider": "github",
                    "published_link": _published_link(target),
                    "target_document": target.model_dump(exclude_defaults=True),
                }
            )
        refreshed_target = target.model_dump(exclude_defaults=True)
        artifact_paths = (write_result.get("metadata") or {}).get("artifact_paths") or {}
        persisted_path = artifact_paths.get("persisted_document")
        if persisted_path:
            published_value = _read_artifact_data(persisted_path)
            representation = str(
                (write_result.get("metadata") or {}).get("representation") or "ir"
            )
        else:
            refreshed = resource.load_document(
                TargetDocument(
                    **target.model_dump(exclude={"meta"}),
                    meta={**target.meta, "stage": "final"},
                )
            )
            published_value = _primary_data(refreshed)
            refreshed_target = _result_data(refreshed, "target_document")
            representation = str(refreshed.get("representation") or "")
        if representation == "ir":
            persisted = WriterDocument.model_validate(published_value)
            published = (
                _merge_provider_state(publish_document, persisted)
                if mode == "replace" and isinstance(publish_document, WriterDocument)
                else persisted
            )
            published = _set_document_editable(published, stage="final")
        else:
            published = published_value
        return _json_dumps(
            {
                "publish_result": _primary_data(write_result),
                "draft_document": (
                    published.model_dump(exclude_defaults=True)
                    if isinstance(published, WriterDocument)
                    else published
                ),
                "representation": representation,
                "provider": str(target.adapter or ""),
                "published_link": _published_link(target),
                "target_document": refreshed_target,
            }
        )


def resolve_provider_targets(
    user_input: str, *, stage: str | None = None
) -> list[TargetDocument]:
    return _provider_targets(user_input, stage=stage)


def resolve_provider_target(
    user_input: str, *, stage: str | None = None
) -> TargetDocument:
    return _provider_target(user_input, stage=stage)


def extract_provider_resources(user_input: str) -> list[dict]:
    return _extract_provider_resources(user_input)


def resolve_document_target(
    source_document: WriterDocument | None = None,
    target_document_json: str = "",
    target_uri: str = "",
) -> TargetDocument | None:
    return _resolve_target(source_document, target_document_json, target_uri)


__all__ = [
    "WriterResourceCapabilities",
    "extract_provider_resources",
    "resolve_provider_target",
    "resolve_provider_targets",
    "sync_document",
    "sync_writer_documents",
]
