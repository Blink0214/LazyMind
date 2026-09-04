"""Writing-task, outlining, drafting, and finalization capabilities."""

from __future__ import annotations
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from queue import Empty, Queue
from threading import Event, RLock
from typing import Any, ClassVar
import lazyllm
from lazyllm import LOG, AutoModel, ThreadPoolExecutor
from lazyllm.module.llms.onlinemodule.base.model_call_runner import (
    is_retryable_transport_error,
)
from lazyllm.tools.agent import ToolExecutionError
from lazyllm.tools.writer.data_models import (
    InputResource,
    SectionInstruction,
    SectionInstructionList,
    VisualPlan,
    WriterBlock,
    WriterDocument,
    WritingTask,
)
from lazyllm.tools.writer.tools import (
    WriterContextTools,
    WriterDraftingTools,
    WriterExecutionTools,
    WriterMultimodalTools,
    WriterPlanningTools,
    WriterQualityTools,
    WriterResourceTools,
)
from lazyllm.tools.writer.utils import render_block_markdown
from lazyllm.tools.tools.search import (
    BingSearch,
    BochaSearch,
    GoogleSearch,
    SciverseSearch,
    TavilySearch,
)
from lazymind.chat.engine.tools.lazy_kb import KBToolkit
from .artifacts import (
    WRITER_BLOCK_SCHEMA,
    WRITER_IR_SCHEMA,
    _document_text,
    _document_value,
    _json_dumps,
    _json_loads,
    _normalize_streamed_markdown_section,
    _primary_data,
    _read_artifact_data,
    _result_data,
    _set_document_editable,
    _temp_root,
    _write_document_input,
    _write_input_artifact,
    writer_schema,
)
from .references import _bind_document_cross_reference_targets
from .resources import _extract_provider_resources, _target_from_document

_CHINESE_CHAR_LIMIT_RE = re.compile(
    r"(?P<prefix>不超过|至多|最多|约|大约|大概)?\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>万|千)?\s*字"
    r"(?P<suffix>左右|上下|以内|以下)?"
)
_MARKDOWN_ATX_HEADING_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marks>#{1,6})[ \t]+" r"(?P<title>.*?)(?:[ \t]+#+)?[ \t]*$",
)
_MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(?P<marks>`{3,}|~{3,})")
_MARKDOWN_DRAFT_ROOT_ERROR = (
    "Markdown draft section must contain exactly one H2 root heading."
)
_SECTION_STREAM_IDLE_ERROR_RE = re.compile(
    r"(?:^|:\s)Draft (?:Markdown|IR) stream was idle for " r"\d+(?:\.\d+)? seconds\.$",
)


class _WriterRetrievalError(RuntimeError):
    def __init__(self, message: str, tools_used: list[str]):
        super().__init__(message, tools_used)
        self.tools_used = tools_used

    def __str__(self) -> str:
        return str(self.args[0])


def _writer_selected_kb_ids() -> list[str]:
    config = lazyllm.globals.get("agentic_config") or {}
    selected = (config.get("filters") or {}).get("kb_id")
    values = selected if isinstance(selected, list) else [selected]
    return [str(value).strip() for value in values if str(value or "").strip()]


def _writer_retrieve(query: str) -> tuple[str, Any]:
    """Use the request-selected KB, otherwise the configured external search provider."""
    if _writer_selected_kb_ids():
        return "kb_search", KBToolkit().kb_search(query)

    attempted: list[str] = []
    candidates = (
        ("sciverse_search", SciverseSearch),
        ("google_search", GoogleSearch),
        ("bing_search", BingSearch),
        ("bocha_search", BochaSearch),
        ("tavily_search", TavilySearch),
    )
    for tool_name, search_type in candidates:
        try:
            engine = search_type()
            if not engine.__key_source__():
                continue
            attempted.append(tool_name)
            return tool_name, engine.search(query)
        except Exception as exc:
            LOG.warning(
                "[Writer] %s retrieval failed: %s", tool_name, type(exc).__name__
            )
    if attempted:
        raise _WriterRetrievalError(
            "All configured search providers failed.", attempted
        )
    raise _WriterRetrievalError(
        "No knowledge base is selected and no external search provider is configured.",
        [],
    )


def _is_retryable_section_error(exc: Exception) -> bool:
    """Recognize recoverable section failures after LazyLLM tool wrapping."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return True
        if _SECTION_STREAM_IDLE_ERROR_RE.search(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return is_retryable_transport_error(exc)


def _extract_length_constraints(query: str) -> dict[str, int]:
    match = _CHINESE_CHAR_LIMIT_RE.search(query)
    if match is None:
        return {}
    multiplier = {"万": 10000, "千": 1000}.get(match.group("unit"), 1)
    target_chars = int(float(match.group("value")) * multiplier)
    approximate = match.group("prefix") in {"约", "大约", "大概"} or match.group(
        "suffix"
    ) in {"左右", "上下"}
    return {
        "target_chars": target_chars,
        "max_chars": target_chars * 11 // 10 if approximate else target_chars,
    }


_MARKDOWN_NO_MEDIA_PATTERNS = (
    re.compile(
        r"(?:不要|不需要|无需|不用|禁止)\s*(?:使用|添加|插入|生成|展示|显示)?\s*"
        r"(?:任何\s*)?(?:图片|图像|插图|配图|视觉(?:素材|内容)?)"
        r"|不(?:使用|添加|插入|生成|展示|显示)\s*(?:任何\s*)?"
        r"(?:图片|图像|插图|配图|视觉(?:素材|内容)?)"
        r"|不插图|无图",
    ),
    re.compile(
        r"\b(?:no|without)\s+(?:any\s+)?(?:images?|pictures?|illustrations?|visuals?)\b"
        r"|\b(?:do\s+not|don't)\s+(?:use|include|add|generate|insert|show|display)\s+"
        r"(?:any\s+)?(?:images?|pictures?|illustrations?|visuals?)\b",
        re.IGNORECASE,
    ),
)


class DraftMarkdownStreamEventEmitter:
    """Publish one attempt-scoped Markdown preview for a Writer artifact."""

    MAX_DELTA_CHARS: ClassVar[int] = 2

    EVENT_TYPES: ClassVar[dict[str, str]] = {
        "start": "artifact_stream_start",
        "delta": "artifact_stream",
        "end": "artifact_stream_end",
        "abort": "artifact_stream_abort",
    }

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        slot: str = "draft_document",
    ) -> None:
        if not slot.strip():
            raise ValueError("slot must not be empty.")
        self._emit = emit
        self._slot = slot.strip()
        self._stream_id = uuid.uuid4().hex
        self._chunk_index = 0
        self._closed = False
        self._lock = RLock()
        with self._lock:
            self._publish_locked("start")

    @property
    def stream_id(self) -> str:
        return self._stream_id

    def feed(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            if self._closed:
                return
            # Model providers and the IR/Markdown normalizers may deliver a
            # whole sentence or paragraph in one callback. Keep the artifact
            # stream's display contract stable by publishing small deltas while
            # preserving the exact text and order.
            for start in range(0, len(delta), self.MAX_DELTA_CHARS):
                self._publish_locked(
                    "delta",
                    delta=delta[start : start + self.MAX_DELTA_CHARS],
                )

    def end(self) -> None:
        self._finish("end")

    def abort(self, message: str = "") -> None:
        self._finish("abort", message=message)

    def restart(self, prefix: str = "") -> None:
        """Replace an interrupted preview while keeping subsequent deltas live."""
        with self._lock:
            if not self._closed:
                self._publish_locked(
                    "abort",
                    message="Interrupted section is being regenerated.",
                )
            self._stream_id = uuid.uuid4().hex
            self._chunk_index = 0
            self._closed = False
            self._publish_locked("start")
            if prefix:
                self.feed(prefix)

    def flush(self) -> None:
        """Compatibility no-op: deltas are already published immediately."""

    def _finish(self, event: str, *, message: str = "") -> None:
        with self._lock:
            if self._closed:
                return
            self._publish_locked(event, message=message)
            self._closed = True

    def _publish_locked(
        self, event: str, *, delta: str = "", message: str = ""
    ) -> None:
        self._chunk_index += 1
        payload: dict[str, Any] = {
            "type": self.EVENT_TYPES[event],
            "slot": self._slot,
            "content_type": "text/markdown",
            "stream_id": self._stream_id,
            "chunk_index": self._chunk_index,
        }
        if event == "delta":
            payload["delta"] = delta
        elif event == "abort" and message:
            payload["message"] = message
        try:
            self._emit(payload)
        except Exception as exc:  # noqa: BLE001 - preview forwarding is best effort.
            LOG.warning("[Writer] failed to forward artifact stream event: %s", exc)


def _markdown_media_is_explicitly_disabled(writing_task: dict[str, Any]) -> bool:
    """Return whether the structured Writer request policy forbids visual media."""
    policy = (writing_task.get("constraints") or {}).get("visual_policy") or {}
    if policy.get("allow_visuals") is False:
        return True
    query = str(writing_task.get("query") or "")
    return any(pattern.search(query) for pattern in _MARKDOWN_NO_MEDIA_PATTERNS)


def _requires_input_image_reuse(writing_task: dict[str, Any]) -> bool:
    visual_policy = (writing_task.get("constraints") or {}).get("visual_policy") or {}
    return visual_policy.get("require_input_image_reuse") is True


def _ensure_required_visual_plan(
    visual_plan: dict[str, Any], *, required: bool
) -> None:
    if required and not (visual_plan.get("instructions") or []):
        raise ValueError(
            "Required input image reuse produced no visual plan instructions."
        )


def generate_short_writing_plan(
    writing_task_path: str,
    writing_context_path: str,
    *,
    artifact_store: str,
) -> Any:
    result = WriterPlanningTools(
        llm=AutoModel(model="llm"), artifact_store=artifact_store
    ).generate_short_writing_plan(
        task=writing_task_path,
        context=writing_context_path,
    )
    return _primary_data(result)


def generate_short_visual_plan(
    writing_task_path: str,
    short_writing_plan_path: str,
    writing_context_path: str,
    *,
    artifact_store: str,
) -> dict[str, Any]:
    writing_task = _read_artifact_data(writing_task_path)
    visual_policy = (writing_task.get("constraints") or {}).get("visual_policy") or {}
    require_reuse = visual_policy.get("require_input_image_reuse") is True
    warnings: list[str] = []
    payload: Any = {"instructions": []}
    if visual_policy.get("allow_visuals") is not False:
        try:
            result = WriterPlanningTools(
                llm=AutoModel(model="llm"), artifact_store=artifact_store
            ).generate_short_visual_plan(
                task=writing_task_path,
                short_writing_plan=short_writing_plan_path,
                context=writing_context_path,
            )
            payload = _primary_data(result)
            warnings.extend((result.get("metadata") or {}).get("warnings") or [])
        except Exception as exc:
            if require_reuse:
                raise RuntimeError(
                    f"Required visual planning failed: {type(exc).__name__}: {exc}"
                ) from exc
            warnings.append(f"Visual planning failed: {type(exc).__name__}: {exc}")
    visual_plan = VisualPlan.model_validate(
        payload or {"instructions": []}
    ).model_dump()
    _ensure_required_visual_plan(visual_plan, required=require_reuse)
    return {"visual_plan": visual_plan, "warnings": warnings}


def stream_short_document(
    writing_task_path: str,
    short_writing_plan_path: str,
    writing_context_path: str,
    *,
    artifact_store: str,
    visual_plan_path: str = "",
    media_assets_path: str = "",
    on_delta: Callable[[str], None] | None = None,
) -> Any:
    drafting = WriterDraftingTools(
        llm=AutoModel(model="llm"), artifact_store=artifact_store
    )
    with drafting.stream_short_document(
        task=writing_task_path,
        short_writing_plan=short_writing_plan_path,
        context=writing_context_path,
        visual_plan=visual_plan_path or None,
        media_assets=media_assets_path or None,
    ) as stream:
        for delta in stream:
            if on_delta is not None:
                try:
                    on_delta(str(delta))
                except Exception as exc:
                    LOG.warning(
                        "[Writer] Short document delta callback failed: %s", exc
                    )
        result = stream.result()
    return _primary_data(result)


_IMAGE_URL_KEYS = (
    "contentUrl",
    "content_url",
    "imageUrl",
    "image_url",
    "thumbnailUrl",
    "thumbnail_url",
    "src",
    "url",
)


def _is_image_url(value: str) -> bool:
    lower = value.lower()
    if not lower.startswith(("http://", "https://")):
        return False
    extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
    return any(ext in lower for ext in extensions) or any(
        token in lower for token in ("image", "img", "photo", "pic")
    )


def _collect_image_urls(node: Any, urls: list[str], seen: set[str]) -> None:
    if isinstance(node, dict):
        for key in _IMAGE_URL_KEYS:
            value = node.get(key)
            if isinstance(value, str) and _is_image_url(value) and value not in seen:
                seen.add(value)
                urls.append(value)
        for value in node.values():
            _collect_image_urls(value, urls, seen)
    elif isinstance(node, list):
        for value in node:
            _collect_image_urls(value, urls, seen)


def _tavily_image_urls(query: str, count: int) -> list[str]:
    engine = TavilySearch()
    if not engine.__key_source__():
        return []
    try:
        results = engine.search(query, include_images=True, max_results=count)
    except Exception as exc:
        LOG.warning("[Writer] Tavily image search failed: %s", type(exc).__name__)
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for item in results or []:
        for image in (item.get("extra") or {}).get("images") or []:
            if isinstance(image, str) and _is_image_url(image) and image not in seen:
                seen.add(image)
                urls.append(image)
    return urls[:count]


def _bocha_image_urls(query: str, count: int) -> list[str]:
    engine = BochaSearch()
    if not engine.__key_source__():
        return []
    try:
        response = engine._request(
            "POST",
            f"{engine._base_url}/v1/web-search",
            headers={"Content-Type": "application/json"},
            json={"query": query, "count": min(max(count, 1), 20)},
            timeout=engine._timeout,
        )
        payload = response.json()
    except Exception as exc:
        LOG.warning("[Writer] Bocha image search failed: %s", type(exc).__name__)
        return []
    urls: list[str] = []
    _collect_image_urls(payload, urls, set())
    return urls[:count]


def _pick_search_engine() -> Any | None:
    for search_type in (GoogleSearch, BingSearch, BochaSearch, TavilySearch):
        try:
            engine = search_type()
            if engine.__key_source__():
                return engine
        except Exception:
            continue
    return None


def _fallback_image_urls(query: str, count: int) -> list[str]:
    engine = _pick_search_engine()
    if engine is None:
        return []
    try:
        results = engine.search(f"{query} reference image illustration")
    except Exception as exc:
        LOG.warning(
            "[Writer] %s image search failed: %s",
            type(engine).__name__,
            type(exc).__name__,
        )
        return []
    return [
        str(item.get("url") or "").strip()
        for item in results or []
        if _is_image_url(str(item.get("url") or "").strip())
    ][:count]


def acquire_web_search_resources(request: Mapping[str, Any]) -> list[dict]:
    purpose = str(request.get("purpose") or "").strip()
    query = " ".join(
        part
        for part in (str(request.get("visual_type") or "").strip(), purpose)
        if part
    )
    urls = _tavily_image_urls(query, 5)
    if not urls:
        urls = _bocha_image_urls(query, 5)
    if not urls:
        urls = _fallback_image_urls(query, 5)
    instruction_id = str(request.get("instruction_id") or uuid.uuid4().hex)
    return [
        {
            "resource_id": f"web-search-{instruction_id}-{index}",
            "resource_type": "image",
            "uri": url,
            "title": purpose or url,
            "summary": purpose,
            "meta": {"source_type": "web_search", "semantic_status": "unverified"},
        }
        for index, url in enumerate(urls, start=1)
    ]


def acquire_visual_media(
    request: Mapping[str, Any],
    acquirers: Mapping[str, Callable[[Mapping[str, Any]], list[dict]]],
) -> Iterator[dict]:
    strategies = list(request["strategies"])
    for strategy in strategies:
        acquirer = acquirers.get(strategy)
        if acquirer is None:
            continue
        try:
            resources = acquirer(request)
        except Exception as exc:
            LOG.warning(
                "[Writer] Failed to acquire %s for visual instruction %r: %s",
                strategy,
                request.get("instruction_id"),
                type(exc).__name__,
            )
            continue
        for candidate in resources:
            resource = dict(candidate)
            resource["meta"] = {
                **dict(resource.get("meta") or {}),
                "requested_strategy": strategies[0],
                "acquisition_strategy": strategy,
            }
            yield resource


def fill_markdown_media_placeholders(markdown: str, resolved_media_assets: Any) -> str:
    """Replace resolved Markdown media placeholders with registered image paths."""
    wiki_placeholder_pattern = re.compile(
        r"!\[\[([^\]]*)\]\]\(media-placeholder://([A-Za-z0-9_-]+)\)"
    )
    placeholder_pattern = re.compile(
        r"!\[([^\]]*)\]\(media-placeholder://([A-Za-z0-9_-]+)\)"
    )
    need_asset_ids = (resolved_media_assets or {}).get("visual_need_asset_ids") or {}
    assets = (resolved_media_assets or {}).get("assets") or {}
    dropped: list[str] = []

    def replace_image(match: re.Match) -> str:
        caption, need_id = match.group(1), match.group(2)
        asset_ids = need_asset_ids.get(need_id) or []
        if asset_ids:
            asset = assets.get(asset_ids[0]) or {}
            path = str(asset.get("local_path") or asset.get("uri") or "")
            if path:
                return f"![{caption}]({path})"
        dropped.append(need_id)
        return ""

    normalized = wiki_placeholder_pattern.sub(
        lambda match: f"![{match.group(1)}](media-placeholder://{match.group(2)})",
        markdown or "",
    )
    filled = placeholder_pattern.sub(replace_image, normalized)
    filled = re.sub(
        r"\(media-placeholder://([A-Za-z0-9_-]+)\)",
        lambda match: (dropped.append(match.group(1)), "")[1],
        filled,
    )
    if dropped:
        LOG.warning(
            "[Writer] Markdown media fill dropped %d unresolved placeholder(s): %s",
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )
    return filled


def drop_unregistered_markdown_images(
    markdown: str,
    resolved_media_assets: Any,
) -> str:
    """Drop Markdown images that are not present in the resolved media library."""
    assets = (resolved_media_assets or {}).get("assets") or {}
    allowed = {
        str(path).strip()
        for asset in assets.values()
        if isinstance(asset, Mapping)
        for path in (asset.get("uri"), asset.get("local_path"))
        if str(path or "").strip()
    }
    image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")
    fence: str | None = None
    dropped: list[str] = []
    output: list[str] = []

    def replace_image(match: re.Match) -> str:
        destination = match.group(2).strip()
        if destination.startswith("<") and ">" in destination:
            target = destination[1 : destination.index(">")]
        else:
            target = destination.split(maxsplit=1)[0]
        if target in allowed:
            return match.group(0)
        dropped.append(target)
        return ""

    for line in (markdown or "").splitlines(keepends=True):
        fence_match = re.match(r"^\s*(```+|~~~+)", line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = marker if fence is None else None if fence == marker else fence
            output.append(line)
            continue
        output.append(image_pattern.sub(replace_image, line) if fence is None else line)

    if dropped:
        LOG.warning(
            "[Writer] Dropped %d unregistered Markdown image reference(s): %s",
            len(dropped),
            ", ".join(sorted(set(dropped))),
        )
    return "".join(output)


class WriterWritingCapabilities:
    WRITER_IR_SCHEMA = WRITER_IR_SCHEMA
    WRITER_BLOCK_SCHEMA = WRITER_BLOCK_SCHEMA

    def build_writing_task(self, query: str, task_id: str = "") -> str:
        """Build a provider-neutral writing task from the user's request."""
        task = WritingTask(
            task_id=task_id.strip() or None,
            query=query,
            task_type="write",
            constraints=_extract_length_constraints(query),
        )
        return _json_dumps(task.model_dump(exclude_defaults=True))

    def build_resources(
        self,
        file_paths_json: str = "[]",
        input_resources_json: str = "[]",
        source_document_json: str = "",
        knowledge_text: str = "",
    ) -> str:
        """Build normalized InputResource data from workflow runtime inputs."""
        file_paths = _json_loads(file_paths_json, [])
        if not isinstance(file_paths, list):
            raise ToolExecutionError("file_paths_json must be a JSON array.")
        resources = _json_loads(input_resources_json, [])
        if not isinstance(resources, list):
            raise ToolExecutionError("input_resources_json must be a JSON array.")
        resources = [dict(item) for item in resources if isinstance(item, dict)]
        known_uris = {str(item.get("uri") or "") for item in resources}
        resources += [
            {
                "resource_id": os.path.basename(path),
                "resource_type": "file",
                "uri": path,
                "title": os.path.basename(path),
                "mime_type": None,
                "summary": None,
                "meta": {},
            }
            for path in file_paths
            if str(path) not in known_uris
        ]

        if source_document_json:
            source = _document_value(source_document_json)
            document = (
                WriterDocument.model_validate(source)
                if isinstance(source, dict)
                else None
            )
            target = _target_from_document(document) if document else None
            resources.append(
                {
                    "resource_id": "source_document",
                    "resource_type": "text",
                    "inline_text": _document_text(document) if document else source,
                    "title": document.title or None if document else None,
                    "summary": None,
                    "meta": {
                        "provider": target.adapter if target else None,
                        "uri": target.uri if target else None,
                        "role": "background",
                    },
                }
            )

        if knowledge_text.strip():
            resources.append(
                {
                    "resource_id": "knowledge_base_evidence",
                    "resource_type": "text",
                    "inline_text": knowledge_text,
                    "title": "Knowledge base evidence",
                    "summary": None,
                    "meta": {"provider": "knowledge_base", "role": "background"},
                }
            )
        return _json_dumps(resources)

    def collect_available_media(
        self,
        writing_task_json: str,
        input_resources_json: str = "[]",
        media_store: str = "",
        use_vision_model: bool = False,
        source_document_json: str = "",
    ) -> str:
        """Collect available images through LazyLLM's multimodal writer tools."""
        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        resources_path = _write_input_artifact(
            root,
            "input_resources.json",
            _json_loads(input_resources_json, []),
            writer_schema("task.InputResource"),
        )
        source_document_path = (
            _write_document_input(root, "source_document", source_document_json)
            if source_document_json
            else None
        )
        artifact_store = Path(media_store.strip()) if media_store.strip() else root
        artifact_store.mkdir(parents=True, exist_ok=True)
        result = WriterMultimodalTools(
            llm=AutoModel(model="vlm") if use_vision_model else None,
            artifact_store=str(artifact_store),
        ).collect_available_media(
            task=task_path,
            input_resources=resources_path,
            source_document=source_document_path,
        )
        return _json_dumps(
            {
                "media_assets": _result_data(result, "media_assets"),
                "profile_input_resources": _result_data(
                    result, "profile_input_resources"
                ),
                "warnings": (result.get("metadata") or {}).get("warnings") or [],
            }
        )

    def resolve_visual_needs(
        self,
        visual_plan_json: str,
        media_assets_json: str,
        allowed_strategies_json: str = "",
    ) -> str:
        """Match visual needs against media already available to the task."""
        root = _temp_root()
        visual_plan_path = _write_input_artifact(
            root,
            "visual_plan.json",
            _json_loads(visual_plan_json, {}),
            writer_schema("multimodal.VisualPlan"),
        )
        media_assets_path = _write_input_artifact(
            root,
            "media_assets.json",
            _json_loads(media_assets_json, {}),
            writer_schema("multimodal.MediaAssetLibrary"),
        )
        allowed_strategies = _json_loads(allowed_strategies_json, None)
        if allowed_strategies is not None and not isinstance(allowed_strategies, list):
            raise ToolExecutionError(
                "allowed_strategies_json must contain a JSON list."
            )
        result = WriterMultimodalTools(
            llm=AutoModel(model="llm"),
        ).resolve_visual_needs(
            visual_plan=visual_plan_path,
            media_assets=media_assets_path,
            allowed_strategies=allowed_strategies,
        )
        return _json_dumps(
            {
                **result,
                "media_assets": result["media_assets"].model_dump(),
            }
        )

    def materialize_acquired_media(
        self,
        visual_plan_json: str,
        media_assets_json: str,
        acquired_resources_json: str,
        media_store: str = "",
    ) -> str:
        """Validate and bind explicitly acquired media to visual needs."""
        root = _temp_root()
        visual_plan_path = _write_input_artifact(
            root,
            "visual_plan.json",
            _json_loads(visual_plan_json, {}),
            writer_schema("multimodal.VisualPlan"),
        )
        media_assets_path = _write_input_artifact(
            root,
            "media_assets.json",
            _json_loads(media_assets_json, {}),
            writer_schema("multimodal.MediaAssetLibrary"),
        )
        acquired_resources_path = _write_input_artifact(
            root,
            "acquired_resources.json",
            _json_loads(acquired_resources_json, {}),
            "lazyllm.tools.writer.artifacts.acquired_resources",
        )
        artifact_store = Path(media_store.strip()) if media_store.strip() else root
        artifact_store.mkdir(parents=True, exist_ok=True)
        result = WriterMultimodalTools(
            artifact_store=str(artifact_store),
        ).materialize_acquired_media(
            visual_plan=visual_plan_path,
            media_assets=media_assets_path,
            acquired_resources=acquired_resources_path,
        )
        return _json_dumps(
            {
                **result,
                "media_assets": result["media_assets"].model_dump(),
            }
        )

    def profile_resources(
        self, writing_task_json: str, user_input: str, resources_json: str = "[]"
    ) -> str:
        """Profile writing resources."""
        root = _temp_root()
        task_data = _json_loads(writing_task_json, {})
        resources = _json_loads(resources_json, [])
        if resources is None:
            resources = []
        if not isinstance(resources, list):
            raise ToolExecutionError("resources_json must be a JSON array.")
        provider_resource_uris = {
            str(item.get("uri") or "")
            for item in resources
            if isinstance(item, dict)
            and isinstance(item.get("meta"), dict)
            and item["meta"].get("provider")
        }
        resources += [
            item
            for item in _extract_provider_resources(user_input)
            if item["uri"] not in provider_resource_uris
        ]

        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            task_data,
            writer_schema("task.WritingTask"),
        )
        input_resources = [InputResource.model_validate(item) for item in resources]
        result = WriterResourceTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).profile_resources(task=task_path, input_resources=input_resources)
        return _json_dumps(_primary_data(result))

    def create_writing_context(
        self,
        writing_task_json: str,
        resource_profiles_json: str = "[]",
        writer_document_json: str = "",
    ) -> str:
        """Create context from a task, profiles, and an optional document."""
        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        profiles_path = _write_input_artifact(
            root,
            "resource_profiles.json",
            _json_loads(resource_profiles_json, []),
            writer_schema("resource.ResourceProfile"),
        )
        document_path = None
        if writer_document_json:
            document_path = _write_document_input(
                root, "writer_document", writer_document_json
            )
        result = WriterContextTools(
            llm=None, artifact_store=str(root)
        ).create_writing_context(
            task=task_path,
            resource_profiles=profiles_path,
            document=document_path,
        )
        return _json_dumps(_primary_data(result))

    def generate_outline(
        self, writing_task_json: str, writing_context_json: str
    ) -> str:
        """Generate an outline in the task's selected representation."""
        planning, task_path, context_path = self._outline_planning(
            writing_task_json,
            writing_context_json,
        )
        result = planning.generate_outline(task=task_path, context=context_path)
        return self._outline_result(result)

    def _outline_planning(
        self,
        writing_task_json: str,
        writing_context_json: str,
    ) -> tuple[WriterPlanningTools, str, str]:
        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        planning = WriterPlanningTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        )
        return planning, task_path, context_path

    @staticmethod
    def _outline_result(result: dict) -> str:
        outline = _primary_data(result)
        if isinstance(outline, str):
            return outline
        return _set_document_editable(outline, stage="outline").model_dump_json(
            exclude_defaults=True
        )

    def stream_outline(
        self,
        writing_task_json: str,
        writing_context_json: str,
        on_delta: Callable[[str], None],
    ) -> str:
        """Generate an outline while exposing its Markdown preview deltas."""
        planning, task_path, context_path = self._outline_planning(
            writing_task_json,
            writing_context_json,
        )
        with planning.stream_outline(task=task_path, context=context_path) as stream:
            for delta in stream:
                try:
                    on_delta(delta)
                except (
                    Exception
                ) as exc:  # noqa: BLE001 - preview forwarding is best effort.
                    LOG.warning("[Writer] Outline delta callback failed: %s", exc)
            result = stream.result()
        return self._outline_result(result)

    def generate_rewrite_outline(
        self,
        writing_task_json: str,
        source_document_json: str,
        writing_context_json: str,
    ) -> str:
        """Generate an internal outline for a complete WriterDocument rewrite."""
        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        source_path = _write_document_input(
            root, "source_document", source_document_json
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterPlanningTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).generate_rewrite_outline(
            task=task_path,
            source_document=source_path,
            context=context_path,
        )
        return WriterDocument.model_validate(_primary_data(result)).model_dump_json(
            exclude_defaults=True
        )

    def generate_rewrite_section_instructions(
        self,
        writing_task_json: str,
        source_document_json: str,
        writing_context_json: str,
    ) -> str:
        """Plan a complete IR or Markdown rewrite without generating an outline."""
        root = _temp_root()
        writing_task = _json_loads(writing_task_json, {})
        require_input_image_reuse = _requires_input_image_reuse(writing_task)
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            writing_task,
            writer_schema("task.WritingTask"),
        )
        source_path = _write_document_input(
            root, "source_document", source_document_json
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        planning = WriterPlanningTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        )
        result = planning.generate_rewrite_section_instructions(
            task=task_path,
            source_document=source_path,
            context=context_path,
        )
        instructions = SectionInstructionList.model_validate(_primary_data(result))
        representation = str(instructions.meta.get("representation") or "markdown")
        document_title = str(instructions.meta.get("document_title") or "").strip()
        visual_plan: dict[str, Any] = VisualPlan().model_dump()
        warnings = []
        if representation == "markdown" and _markdown_media_is_explicitly_disabled(
            _json_loads(writing_task_json, {})
        ):
            return _json_dumps(
                {
                    "section_instructions": instructions.model_dump(
                        exclude_defaults=True
                    ),
                    "visual_plan": visual_plan,
                    "document_title": document_title,
                    "warnings": warnings,
                }
            )
        if representation == "ir":
            transient_outline = WriterDocument(
                document_id=f"{instructions.instruction_set_id}-visual-outline",
                stage="outline",
                title=document_title,
                blocks=[
                    WriterBlock(
                        node_id=instruction.content_ref.node_id
                        or f"rewrite-section-{index}",
                        type="heading",
                        content=instruction.section_title,
                        stage="outline",
                        numbering={"level": 1},
                    )
                    for index, instruction in enumerate(
                        instructions.instructions, start=1
                    )
                ],
            )
            outline_path = _write_input_artifact(
                root,
                "rewrite_visual_outline.lmd",
                transient_outline.model_dump(exclude_defaults=True),
                self.WRITER_IR_SCHEMA,
            )
        else:
            transient_markdown = f"# {document_title}\n" + "\n".join(
                f"## {instruction.section_title}"
                for instruction in instructions.instructions
            )
            outline_path = _write_document_input(
                root,
                "rewrite_visual_outline",
                transient_markdown,
            )
        try:
            visual_result = planning.generate_visual_plan(
                task=task_path,
                outline=outline_path,
                context=context_path,
            )
            visual_plan = _primary_data(visual_result)
            warnings.extend((visual_result.get("metadata") or {}).get("warnings") or [])
        except Exception as exc:
            if require_input_image_reuse:
                raise RuntimeError(
                    f"Required visual planning failed: {type(exc).__name__}: {exc}"
                ) from exc
            warnings.append(f"Visual planning failed: {type(exc).__name__}: {exc}")
        _ensure_required_visual_plan(
            visual_plan,
            required=require_input_image_reuse,
        )
        return _json_dumps(
            {
                "section_instructions": instructions.model_dump(exclude_defaults=True),
                "visual_plan": visual_plan,
                "document_title": document_title,
                "warnings": warnings,
            }
        )

    def prepare_outline(
        self,
        source_document_json: str,
        writing_task_json: str = "",
        writing_context_json: str = "",
    ) -> str:
        """Normalize a supplied document into an editable outline."""
        source = _document_value(source_document_json)
        if isinstance(source, str):
            return self._complete_prepared_outline(
                source,
                writing_task_json,
                writing_context_json,
            )
        document = WriterDocument.model_validate(source)
        if not any(block.type == "heading" for block in document.blocks):
            for block in document.blocks:
                if block.type != "paragraph":
                    continue
                lines = [
                    line.strip() for line in block.content.splitlines() if line.strip()
                ]
                if not lines:
                    continue
                block.type = "heading"
                block.content = lines[0]
                block.spans = []
                block.numbering["level"] = 1
                if len(lines) > 1:
                    block.children.insert(
                        0,
                        WriterBlock(
                            node_id=f"{block.node_id}-description",
                            type="paragraph",
                            content="\n".join(lines[1:]),
                            stage="outline",
                        ),
                    )
        if any(block.type == "heading" for block in document.blocks) and not any(
            child.type == "heading"
            for block in document.blocks
            for child in block.iter_blocks()
            if child is not block
        ):
            roots: list[WriterBlock] = []
            headings: list[tuple[int, WriterBlock]] = []
            for block in document.blocks:
                if block.type != "heading":
                    (headings[-1][1].children if headings else roots).append(block)
                    continue
                level = block.numbering.get("level")
                if (
                    not isinstance(level, int)
                    or isinstance(level, bool)
                    or not 1 <= level <= 9
                ):
                    level = 1
                while headings and headings[-1][0] >= level:
                    headings.pop()
                block.numbering["level"] = len(headings) + 1
                (headings[-1][1].children if headings else roots).append(block)
                headings.append((level, block))
            document.blocks = roots
        normalized = _set_document_editable(
            document,
            stage="outline",
        ).model_dump_json(exclude_defaults=True)
        return self._complete_prepared_outline(
            normalized,
            writing_task_json,
            writing_context_json,
        )

    def _complete_prepared_outline(
        self,
        normalized: str,
        writing_task_json: str,
        writing_context_json: str,
    ) -> str:
        if not writing_task_json or not writing_context_json:
            return normalized
        planning, task_path, context_path = self._outline_planning(
            writing_task_json,
            writing_context_json,
        )
        outline_path = _write_document_input(
            Path(task_path).parent,
            "outline_to_complete",
            normalized,
        )
        return self._outline_result(
            planning._complete_outline_instructions(
                outline=outline_path,
                task=task_path,
                context=context_path,
            )
        )

    def generate_section_instructions(
        self,
        writing_task_json: str,
        outline_json: str,
        writing_context_json: str,
    ) -> str:
        """Generate section instructions from an IR or Markdown outline."""
        root = _temp_root()
        writing_task = _json_loads(writing_task_json, {})
        require_input_image_reuse = _requires_input_image_reuse(writing_task)
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            writing_task,
            writer_schema("task.WritingTask"),
        )
        outline_path = _write_document_input(root, "outline", outline_json)
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        planning = WriterPlanningTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        )
        warnings = []
        visual_plan = VisualPlan().model_dump()
        if not (
            isinstance(_document_value(outline_json), str)
            and _markdown_media_is_explicitly_disabled(
                _json_loads(writing_task_json, {})
            )
        ):
            try:
                visual_result = planning.generate_visual_plan(
                    task=task_path,
                    outline=outline_path,
                    context=context_path,
                )
                visual_plan = _primary_data(visual_result)
                warnings.extend(
                    (visual_result.get("metadata") or {}).get("warnings") or []
                )
            except Exception as exc:
                if require_input_image_reuse:
                    raise RuntimeError(
                        f"Required visual planning failed: {type(exc).__name__}: {exc}"
                    ) from exc
                warnings.append(f"Visual planning failed: {type(exc).__name__}: {exc}")
        _ensure_required_visual_plan(
            visual_plan,
            required=require_input_image_reuse,
        )
        visual_plan_path = _write_input_artifact(
            root,
            "visual_plan.json",
            visual_plan,
            writer_schema("multimodal.VisualPlan"),
        )
        result = planning.generate_section_instructions(
            outline=outline_path,
            context=context_path,
            visual_plan=visual_plan_path,
            task=task_path,
        )
        return _json_dumps(
            {
                "section_instructions": _primary_data(result),
                "visual_plan": visual_plan,
                "warnings": warnings,
            }
        )

    def execute_writing_subtasks(
        self,
        outline_json: str,
        writing_context_json: str,
        on_progress: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> str:
        root = _temp_root()
        outline_path = _write_document_input(root, "outline", outline_json)
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterExecutionTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).execute_writing_subtasks(
            outline=outline_path,
            context=context_path,
            on_progress=on_progress,
            retrieve=_writer_retrieve,
        )
        completed = _primary_data(result)
        return completed if isinstance(completed, str) else _json_dumps(completed)

    def generate_draft_section(
        self,
        writing_task_json: str,
        section_instruction_json: str,
        writing_context_json: str,
        previous_blocks_json: str = "[]",
        visual_plan_json: str = "",
        media_assets_json: str = "",
    ) -> str:
        """Generate one draft section in the instruction's representation."""
        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        instruction = SectionInstruction.model_validate(
            _json_loads(section_instruction_json, {})
        )
        previous_blocks = _json_loads(previous_blocks_json, [])
        visual_plan_path = None
        if visual_plan_json:
            visual_plan_path = _write_input_artifact(
                root,
                "visual_plan.json",
                _json_loads(visual_plan_json, {}),
                writer_schema("multimodal.VisualPlan"),
            )
        media_assets_path = None
        if media_assets_json:
            media_assets_path = _write_input_artifact(
                root,
                "media_assets.json",
                _json_loads(media_assets_json, {}),
                writer_schema("multimodal.MediaAssetLibrary"),
            )
        result = WriterDraftingTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).generate_draft_section(
            task=task_path,
            section_instruction=instruction,
            context=context_path,
            previous_blocks=previous_blocks,
            visual_plan=visual_plan_path,
            media_assets=media_assets_path,
        )
        return _json_dumps(_primary_data(result))

    def generate_draft_section_markdown(
        self,
        writing_task_json: str,
        section_instruction_json: str,
        writing_context_json: str,
        previous_markdown: str = "",
    ) -> str:
        """Generate one Markdown draft section through the unified drafting API."""
        return _json_loads(
            self.generate_draft_section(
                writing_task_json=writing_task_json,
                section_instruction_json=section_instruction_json,
                writing_context_json=writing_context_json,
                previous_blocks_json=_json_dumps(
                    [previous_markdown] if previous_markdown else []
                ),
            ),
            "",
        )

    def generate_draft_blocks(
        self,
        writing_task_json: str,
        section_instructions_json: str,
        writing_context_json: str,
        visual_plan_json: str = "",
        media_assets_json: str = "",
    ) -> str:
        """Generate every planned draft section in order."""
        instructions_data = _json_loads(section_instructions_json, {})
        instructions = (
            instructions_data.get("instructions")
            if isinstance(instructions_data, dict)
            else None
        )
        if not isinstance(instructions, list):
            raise ToolExecutionError(
                "section_instructions_json must contain instructions."
            )
        _bind_document_cross_reference_targets(instructions)

        blocks: list[Any] = []
        for instruction in instructions:
            block = _json_loads(
                self.generate_draft_section(
                    writing_task_json=writing_task_json,
                    section_instruction_json=_json_dumps(instruction),
                    writing_context_json=writing_context_json,
                    previous_blocks_json="[]",
                    visual_plan_json=visual_plan_json,
                    media_assets_json=media_assets_json,
                ),
                {},
            )
            blocks.append(block)
        return _json_dumps(blocks)

    def generate_draft_blocks_markdown(
        self,
        writing_task_json: str,
        section_instructions_json: str,
        writing_context_json: str,
        visual_plan_json: str = "",
    ) -> str:
        """Generate every planned draft section in Markdown, in order."""
        return self.generate_draft_blocks(
            writing_task_json=writing_task_json,
            section_instructions_json=section_instructions_json,
            writing_context_json=writing_context_json,
            visual_plan_json=visual_plan_json,
        )

    def stream_draft_blocks_markdown(
        self,
        writing_task_json: str,
        section_instructions_json: str,
        writing_context_json: str,
        on_delta: Callable[[str], None],
        on_section_end: Callable[[], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        on_preview_restart: Callable[[str], None] | None = None,
        visual_plan_json: str = "",
        checkpoint_dir: str = "",
    ) -> str:
        """Generate Markdown sections through LazyLLM's non-tool streaming API."""
        return self._stream_draft_blocks(
            writing_task_json=writing_task_json,
            section_instructions_json=section_instructions_json,
            writing_context_json=writing_context_json,
            representation="markdown",
            on_delta=on_delta,
            on_section_end=on_section_end,
            on_progress=on_progress,
            on_preview_restart=on_preview_restart,
            visual_plan_json=visual_plan_json,
            checkpoint_dir=checkpoint_dir,
        )

    def stream_draft_blocks_ir(
        self,
        writing_task_json: str,
        section_instructions_json: str,
        writing_context_json: str,
        on_delta: Callable[[str], None],
        on_section_end: Callable[[], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        on_preview_restart: Callable[[str], None] | None = None,
        visual_plan_json: str = "",
        media_assets_json: str = "",
        checkpoint_dir: str = "",
    ) -> str:
        """Generate IR sections while exposing their Markdown preview deltas."""
        return self._stream_draft_blocks(
            writing_task_json=writing_task_json,
            section_instructions_json=section_instructions_json,
            writing_context_json=writing_context_json,
            representation="ir",
            on_delta=on_delta,
            on_section_end=on_section_end,
            on_progress=on_progress,
            on_preview_restart=on_preview_restart,
            visual_plan_json=visual_plan_json,
            media_assets_json=media_assets_json,
            checkpoint_dir=checkpoint_dir,
        )

    def _stream_draft_blocks(
        self,
        *,
        writing_task_json: str,
        section_instructions_json: str,
        writing_context_json: str,
        representation: str,
        on_delta: Callable[[str], None],
        on_section_end: Callable[[], None] | None,
        on_progress: Callable[[dict[str, Any]], None] | None,
        on_preview_restart: Callable[[str], None] | None,
        visual_plan_json: str = "",
        media_assets_json: str = "",
        checkpoint_dir: str = "",
    ) -> str:
        instructions_data = _json_loads(section_instructions_json, {})
        instructions = (
            instructions_data.get("instructions")
            if isinstance(instructions_data, dict)
            else None
        )
        if not isinstance(instructions, list):
            raise TypeError("section_instructions_json must contain instructions.")
        _bind_document_cross_reference_targets(instructions)

        def forward_delta(delta: str) -> None:
            try:
                on_delta(delta)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - preview forwarding is best effort.
                LOG.warning(
                    "[Writer] Draft %s delta callback failed: %s",
                    representation,
                    exc,
                )

        def forward_progress(**payload: Any) -> None:
            if on_progress is None:
                return
            try:
                on_progress(payload)
            except Exception as exc:  # noqa: BLE001 - progress is observability only.
                LOG.warning("[Writer] Draft progress callback failed: %s", exc)

        def restart_preview(prefix: str) -> None:
            if on_preview_restart is None:
                return
            try:
                on_preview_restart(prefix)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - preview forwarding is best effort.
                LOG.warning("[Writer] Draft preview restart callback failed: %s", exc)

        instruction_list_meta = instructions_data.get("meta")
        if not isinstance(instruction_list_meta, dict):
            instruction_list_meta = {}
        first_instruction = (
            instructions[0]
            if instructions and isinstance(instructions[0], dict)
            else {}
        )
        first_instruction_meta = first_instruction.get("meta")
        if not isinstance(first_instruction_meta, dict):
            first_instruction_meta = {}
        document_title = ""
        for candidate in (
            instruction_list_meta.get("document_title"),
            instruction_list_meta.get("outline_title"),
            first_instruction_meta.get("document_title"),
            first_instruction_meta.get("outline_title"),
        ):
            document_title = str(candidate or "").strip()
            if document_title:
                break
        committed_preview_parts: list[str] = []
        if document_title:
            title_preview = f"# {document_title}\n\n"
            committed_preview_parts.append(title_preview)
            forward_delta(title_preview)

        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        visual_plan_path = None
        if visual_plan_json:
            visual_plan_path = _write_input_artifact(
                root,
                "visual_plan.json",
                _json_loads(visual_plan_json, {}),
                writer_schema("multimodal.VisualPlan"),
            )
        media_assets_path = None
        if media_assets_json:
            media_assets_path = _write_input_artifact(
                root,
                "media_assets.json",
                _json_loads(media_assets_json, {}),
                writer_schema("multimodal.MediaAssetLibrary"),
            )
        checkpoint_root = (
            Path(checkpoint_dir) if checkpoint_dir else root / "section-checkpoints"
        )
        checkpoint_root.mkdir(parents=True, exist_ok=True)
        sections: list[Any] = [None] * len(instructions)
        event_queues: list[Queue] = [Queue() for _ in instructions]
        stop_event = Event()
        progress_lock = RLock()
        completed_count = 0
        max_attempts = max(
            1, int(os.getenv("LAZYMIND_WRITER_SECTION_MAX_ATTEMPTS", "2"))
        )
        section_total_timeout = max(
            1.0,
            float(os.getenv("LAZYMIND_WRITER_SECTION_TOTAL_TIMEOUT", "600")),
        )
        section_stream_idle_timeout = max(
            1.0,
            float(os.getenv("LAZYMIND_WRITER_SECTION_STREAM_IDLE_TIMEOUT", "180")),
        )
        first_section_idle_timeout = max(
            section_stream_idle_timeout,
            float(os.getenv("LAZYMIND_WRITER_FIRST_SECTION_IDLE_TIMEOUT", "180")),
        )
        section_started_at: list[float | None] = [None] * len(instructions)
        forward_progress(
            progress=5,
            current_phase="正在优先生成第 1 章",
            section_total=len(instructions),
            section_completed=0,
        )

        def checkpoint_path(index: int, instruction_data: dict[str, Any]) -> Path:
            payload = json.dumps(
                {
                    "version": 1,
                    "representation": representation,
                    "task": _json_loads(writing_task_json, {}),
                    "instruction": instruction_data,
                    "context": _json_loads(writing_context_json, {}),
                    "visual_plan": _json_loads(visual_plan_json, {}),
                    "media_assets": _json_loads(media_assets_json, {}),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
            suffix = ".md" if representation == "markdown" else ".json"
            return checkpoint_root / f"section-{index + 1:04d}-{digest}{suffix}"

        def load_checkpoint(path: Path, instruction: SectionInstruction) -> Any:
            if not path.is_file():
                return None
            try:
                if representation == "markdown":
                    value = path.read_text(encoding="utf-8")
                    return _normalize_streamed_markdown_section(value, instruction)
                value = json.loads(path.read_text(encoding="utf-8"))
                return WriterBlock.model_validate(value).model_dump(
                    exclude_defaults=True
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                LOG.warning(
                    "[Writer] Ignoring invalid draft section checkpoint %s", path
                )
                return None

        def save_checkpoint(path: Path, section: Any) -> None:
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            if representation == "markdown":
                temporary.write_text(str(section), encoding="utf-8")
            else:
                temporary.write_text(
                    json.dumps(section, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            temporary.replace(path)

        def cached_preview(section: Any) -> str:
            if representation == "markdown":
                return str(section)
            return (
                render_block_markdown(
                    WriterBlock.model_validate(section),
                    level=2,
                ).rstrip()
                + "\n"
            )

        def preview_has_body(preview: str) -> bool:
            """A generated section title alone is not effective body progress."""
            lines = preview.splitlines()
            while lines and not lines[0].strip():
                lines.pop(0)
            if lines and re.match(r"^#{1,6}\s+\S", lines[0].strip()):
                lines.pop(0)
            remainder = "\n".join(lines)
            # Markdown structure alone (another heading/list prefix, fence,
            # whitespace) is not document body progress.
            visible_text = re.sub(
                r"[\s#>*_`~+\-\[\](){}|.!:;，。！？、]+", "", remainder
            )
            return bool(visible_text)

        def mark_completed(index: int, *, cached: bool = False) -> None:
            nonlocal completed_count
            with progress_lock:
                completed_count += 1
                done = completed_count
            if done >= len(instructions):
                phase = f"全部 {len(instructions)} 章已生成，正在按大纲顺序组装"
            elif cached:
                phase = (
                    f"已复用 {done}/{len(instructions)} 章 checkpoint，其他章节仍在生成"
                )
            else:
                phase = f"已完成 {done}/{len(instructions)} 章，其他章节仍在后台生成"
            forward_progress(
                progress=5,
                current_phase=phase,
                section_index=index + 1,
                section_total=len(instructions),
                section_completed=done,
                section_state="checkpointed",
            )

        def generate_one(index: int, instruction_data: dict[str, Any]) -> None:
            events = event_queues[index]
            path = checkpoint_path(index, instruction_data)
            try:
                instruction = SectionInstruction.model_validate(instruction_data)
                cached = load_checkpoint(path, instruction)
                if cached is not None:
                    if not stop_event.is_set():
                        events.put(("delta", cached_preview(cached)))
                        events.put(("done", cached))
                        mark_completed(index, cached=True)
                    return
                for attempt in range(1, max_attempts + 1):
                    section_started_at[index] = time.monotonic()
                    buffered: list[str] = []
                    section_deltas: list[str] = []
                    body_started = False
                    try:
                        forward_progress(
                            progress=5,
                            current_phase=f"第 {index + 1} 章正在生成",
                            section_index=index + 1,
                            section_total=len(instructions),
                            section_completed=completed_count,
                            section_state="generating",
                            section_attempt=attempt,
                        )
                        section_root = (
                            root / f"section-{index + 1:04d}-attempt-{attempt}"
                        )
                        section_root.mkdir(parents=True, exist_ok=True)
                        drafting = WriterDraftingTools(
                            llm=AutoModel(model="llm"),
                            artifact_store=str(section_root),
                        )
                        stream_factory = (
                            drafting.stream_draft_section
                            if representation == "markdown"
                            else drafting.stream_draft_section_ir
                        )
                        stream_kwargs: dict[str, Any] = {
                            "task": task_path,
                            "section_instruction": instruction,
                            "context": context_path,
                        }
                        if visual_plan_path is not None:
                            stream_kwargs["visual_plan"] = visual_plan_path
                        if media_assets_path is not None:
                            stream_kwargs["media_assets"] = media_assets_path
                        stream_kwargs["idle_timeout"] = (
                            first_section_idle_timeout
                            if index == 0 and attempt == 1
                            else section_stream_idle_timeout
                        )
                        try:
                            with stream_factory(**stream_kwargs) as stream:
                                for delta in stream:
                                    if stop_event.is_set():
                                        return
                                    section_deltas.append(delta)
                                    if body_started:
                                        events.put(("delta", delta))
                                        continue
                                    buffered.append(delta)
                                    if preview_has_body("".join(buffered)):
                                        body_started = True
                                        for pending in buffered:
                                            events.put(("delta", pending))
                                        buffered.clear()
                                        forward_progress(
                                            progress=5,
                                            current_phase=f"第 {index + 1} 章正在输出正文",
                                            section_index=index + 1,
                                            section_total=len(instructions),
                                            section_completed=completed_count,
                                            section_state="streaming",
                                            section_attempt=attempt,
                                        )
                                result = stream.result()
                            section = _primary_data(result)
                        except ValueError as exc:
                            if (
                                representation != "markdown"
                                or str(exc) != _MARKDOWN_DRAFT_ROOT_ERROR
                                or not section_deltas
                            ):
                                raise
                            section = _normalize_streamed_markdown_section(
                                "".join(section_deltas),
                                instruction,
                            )
                            LOG.warning(
                                "[Writer] repaired Draft Markdown heading contract without "
                                "regeneration section=%s title=%r",
                                index + 1,
                                instruction.section_title,
                            )
                        if representation == "markdown" and not isinstance(
                            section, str
                        ):
                            raise TypeError(
                                "Markdown Draft stream returned a non-Markdown artifact."
                            )
                        if representation == "ir" and not isinstance(section, dict):
                            raise TypeError(
                                "IR Draft stream returned a non-WriterBlock artifact."
                            )
                        if representation == "markdown":
                            section = _normalize_streamed_markdown_section(
                                section, instruction
                            )
                        if not body_started and preview_has_body(
                            cached_preview(section)
                        ):
                            for pending in buffered:
                                events.put(("delta", pending))
                            body_started = True
                        if not body_started:
                            raise TimeoutError(
                                "Draft section completed without effective body content."
                            )
                        if stop_event.is_set():
                            return
                        save_checkpoint(path, section)
                        events.put(("done", section))
                        mark_completed(index)
                        return
                    except Exception as exc:
                        retryable = _is_retryable_section_error(exc)
                        preview_can_restart = (
                            not body_started or on_preview_restart is not None
                        )
                        if (
                            attempt >= max_attempts
                            or stop_event.is_set()
                            or not retryable
                            or not preview_can_restart
                        ):
                            raise
                        LOG.warning(
                            "[Writer] Retrying interrupted section %d from its instruction: %s",
                            index + 1,
                            exc,
                        )
                        if body_started:
                            # Queue this after every already-published delta from the failed
                            # attempt and before any delta from the next attempt. The ordered
                            # consumer replaces the preview with committed sections only.
                            events.put(("retry", None))
                        forward_progress(
                            progress=5,
                            current_phase=(
                                f"第 {index + 1} 章生成中断，正在根据原章节规划"
                                f"从头重新生成（第 {attempt + 1}/{max_attempts} 次）"
                            ),
                            section_index=index + 1,
                            section_total=len(instructions),
                            section_completed=completed_count,
                            section_state="retrying",
                            section_attempt=attempt + 1,
                        )
            except Exception as exc:  # noqa: BLE001 - propagated on the ordered stream.
                if not stop_event.is_set():
                    events.put(("error", exc))

        executor = ThreadPoolExecutor(max_workers=min(3, max(1, len(instructions))))
        futures: list[Any | None] = [None] * len(instructions)
        futures[0] = executor.submit(generate_one, 0, instructions[0])
        background_started = len(instructions) == 1

        def start_background_sections() -> None:
            nonlocal background_started
            if background_started:
                return
            background_started = True
            for background_index in range(1, len(instructions)):
                futures[background_index] = executor.submit(
                    generate_one,
                    background_index,
                    instructions[background_index],
                )

        try:
            for index, events in enumerate(event_queues):
                if index > 0:
                    start_background_sections()
                future = futures[index]
                if future is None:
                    raise RuntimeError(f"Draft section {index + 1} was not started.")
                wait_started_at = time.monotonic()
                last_wait_progress_at = wait_started_at
                last_stream_progress_at = 0.0
                streamed_chars = 0
                section_stream_started = False
                while True:
                    deadline = (
                        section_started_at[index] or wait_started_at
                    ) + section_total_timeout
                    if time.monotonic() >= deadline and not future.done():
                        raise TimeoutError(
                            f"Draft section {index + 1} exceeded total timeout "
                            f"of {section_total_timeout:g} seconds."
                        )
                    try:
                        event, payload = events.get(
                            timeout=min(
                                1.0,
                                max(0.001, deadline - time.monotonic()),
                            )
                        )
                    except Empty:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Draft section {index + 1} exceeded total timeout "
                                f"of {section_total_timeout:g} seconds."
                            )
                        if future.done():
                            future.result()
                            raise RuntimeError(
                                f"Draft section {index + 1} ended without a terminal event."
                            )
                        now = time.monotonic()
                        if now - last_wait_progress_at >= 10.0:
                            with progress_lock:
                                done = completed_count
                            waiting_for = (
                                "后续正文" if section_stream_started else "有效正文"
                            )
                            forward_progress(
                                progress=5,
                                current_phase=(
                                    f"第 {index + 1} 章仍在生成，正在等待{waiting_for}"
                                    f"（已完成 {done}/{len(instructions)} 章）"
                                ),
                                section_index=index + 1,
                                section_total=len(instructions),
                                section_completed=done,
                                section_state=(
                                    "streaming"
                                    if section_stream_started
                                    else "waiting_for_body"
                                ),
                            )
                            last_wait_progress_at = now
                        continue
                    if event == "delta":
                        if index == 0:
                            # Give the first section exclusive access until its
                            # first visible body arrives. Then keep streaming it
                            # while the remaining sections run in the background.
                            start_background_sections()
                        section_stream_started = True
                        streamed_chars += len(str(payload))
                        now = time.monotonic()
                        if (
                            last_stream_progress_at == 0.0
                            or now - last_stream_progress_at >= 2.0
                        ):
                            with progress_lock:
                                done = completed_count
                            forward_progress(
                                progress=5,
                                current_phase=(
                                    f"正在流式输出第 {index + 1} 章"
                                    f"（已完成 {done}/{len(instructions)} 章，"
                                    f"已输出 {streamed_chars} 字）"
                                ),
                                section_index=index + 1,
                                section_total=len(instructions),
                                section_completed=done,
                                section_state="streaming",
                                section_output_chars=streamed_chars,
                            )
                            last_stream_progress_at = now
                            last_wait_progress_at = now
                        forward_delta(payload)
                        continue
                    if event == "retry":
                        restart_preview("".join(committed_preview_parts))
                        section_stream_started = False
                        streamed_chars = 0
                        last_stream_progress_at = 0.0
                        last_wait_progress_at = time.monotonic()
                        continue
                    if event == "error":
                        raise payload
                    if index == 0:
                        start_background_sections()
                    sections[index] = payload
                    committed_preview_parts.append(cached_preview(payload))
                    if on_section_end is not None:
                        try:
                            on_section_end()
                        except (
                            Exception
                        ) as exc:  # noqa: BLE001 - preview forwarding is best effort.
                            LOG.warning(
                                "[Writer] Draft %s section callback failed: %s",
                                representation,
                                exc,
                            )
                    break
        finally:
            stop_event.set()
            for future in futures:
                if future is not None:
                    future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        return _json_dumps(sections)

    def generate_draft_document(
        self,
        draft_blocks_json: str,
        writing_context_json: str,
        outline_json: str = "",
        title: str = "",
    ) -> str:
        """Combine draft sections while preserving their representation."""
        root = _temp_root()
        blocks_data = _json_loads(draft_blocks_json, [])
        if not isinstance(blocks_data, list) or not blocks_data:
            raise ToolExecutionError(
                "draft_blocks_json must be a non-empty JSON array."
            )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        outline_path = None
        if outline_json:
            outline_path = _write_document_input(root, "outline", outline_json)
        result = WriterDraftingTools(
            llm=None, artifact_store=str(root)
        ).generate_draft_document(
            draft_blocks=blocks_data,
            context=context_path,
            outline=outline_path,
            title=title or None,
        )
        return _json_dumps(_primary_data(result))

    def generate_draft_document_markdown(
        self,
        draft_sections_json: str,
        writing_context_json: str,
        outline_json: str = "",
        title: str = "",
    ) -> str:
        """Combine Markdown sections through the unified drafting API."""
        markdown = _json_loads(
            self.generate_draft_document(
                draft_blocks_json=draft_sections_json,
                writing_context_json=writing_context_json,
                outline_json=outline_json,
                title=title,
            ),
            "",
        )
        return _json_dumps(
            {
                "draft_document": markdown,
            }
        )

    def update_writing_context(
        self, content_artifact_json: str, writing_context_json: str
    ) -> str:
        """Update context from IR or Markdown content."""
        root = _temp_root()
        content_data = _document_value(content_artifact_json)
        if isinstance(content_data, str):
            content_path = root / "writer_content.md"
            content_path.write_text(content_data, encoding="utf-8")
            content_path = str(content_path)
        else:
            schema_name = (
                self.WRITER_IR_SCHEMA
                if "document_id" in content_data
                else self.WRITER_BLOCK_SCHEMA
            )
            content_path = _write_input_artifact(
                root, "writer_content.lmd", content_data, schema_name
            )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterContextTools(
            llm=None, artifact_store=str(root)
        ).update_writing_context(
            artifacts=content_path,
            context=context_path,
        )
        return _json_dumps(_primary_data(result))

    def check_consistency(
        self, draft_document_json: str, writing_context_json: str
    ) -> str:
        """Validate an IR or Markdown draft document."""
        root = _temp_root()
        draft_path = _write_document_input(root, "draft_document", draft_document_json)
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterQualityTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).validate_draft_document(draft_document=draft_path, context=context_path)
        return _json_dumps(
            {
                "review_report": _primary_data(result),
                "review_summary": result.get("summary") or "",
            }
        )

    def generate_final_document(
        self, draft_document_json: str, writing_context_json: str
    ) -> str:
        """Return the final document without changing its representation."""
        root = _temp_root()
        draft_path = _write_document_input(root, "draft_document", draft_document_json)
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterDraftingTools(
            llm=None, artifact_store=str(root)
        ).generate_final_document(
            draft=draft_path,
            context=context_path,
        )
        output_path = result.get("output_file_path") or ""
        markdown = ""
        if output_path:
            with open(output_path, "r", encoding="utf-8") as fh:
                markdown = fh.read()
        final_document = _primary_data(result)
        if not isinstance(final_document, str):
            final_document = _set_document_editable(
                final_document, stage="final"
            ).model_dump(exclude_defaults=True)
        return _json_dumps(
            {
                "final_document": final_document,
                "final_document_md": markdown,
            }
        )


__all__ = [
    "DraftMarkdownStreamEventEmitter",
    "WriterWritingCapabilities",
]
