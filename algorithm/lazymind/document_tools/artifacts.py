"""MD/LMD artifacts, envelopes, paths, conversion, and rendering."""

from __future__ import annotations
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any
from lazyllm.tools.agent import ToolExecutionError
from lazyllm.tools.writer.data_models import (
    SectionInstruction,
    WriterBlock,
    WriterDocument,
)
from lazyllm.tools.writer.numbering import (
    build_numbering_view_from_markdown,
    compute_numbering,
    materialize_markdown,
)
from lazyllm.tools.writer.utils import (
    parse_document_markdown,
    save_artifact_json,
    writer_document_from_lmd,
    writer_document_to_lmd,
    writer_document_to_markdown,
)

WRITER_DATA_MODEL_SCHEMA_PREFIX = "lazyllm.tools.writer.data_models"
WRITER_IR_SCHEMA = f"{WRITER_DATA_MODEL_SCHEMA_PREFIX}.writer_ir.WriterDocument"
WRITER_BLOCK_SCHEMA = f"{WRITER_DATA_MODEL_SCHEMA_PREFIX}.writer_ir.WriterBlock"
_MARKDOWN_ATX_HEADING_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marks>#{1,6})[ \t]+(?P<title>.*?)(?:[ \t]+#+)?[ \t]*$"
)
_MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(?P<marks>`{3,}|~{3,})")
_MARKDOWN_DRAFT_ROOT_ERROR = (
    "Markdown draft section must contain exactly one H2 root heading."
)


def writer_schema(name: str) -> str:
    return f"{WRITER_DATA_MODEL_SCHEMA_PREFIX}.{name}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _json_loads(value: str, default: Any = None) -> Any:
    text = (value or "").strip()
    if not text:
        return default
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "data" in parsed:
        return parsed["data"]
    return parsed


def _read_artifact_data(path: str) -> Any:
    if Path(path).suffix.lower() in {".md", ".markdown"}:
        return Path(path).read_text(encoding="utf-8")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    return raw


def _temp_root() -> Path:
    root = Path(tempfile.gettempdir()) / "lazymind-writer-tools" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_input_artifact(
    root: Path, filename: str, data: Any, schema_name: str
) -> str:
    return save_artifact_json(
        data,
        str(root / filename),
        schema_name=schema_name,
        created_by="WriterToolkit",
    )


def _document_value(value: str) -> Any:
    try:
        return _json_loads(value, {})
    except json.JSONDecodeError:
        return value


def _write_document_input(root: Path, name: str, value: str) -> str:
    content = _document_value(value)
    if isinstance(content, str):
        path = root / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        return str(path)
    return _write_input_artifact(root, f"{name}.lmd", content, WRITER_IR_SCHEMA)


def _primary_data(result: dict) -> Any:
    artifact_path = result.get("artifact_path")
    if not artifact_path:
        raise ToolExecutionError("Writer tool did not return its primary artifact.")
    return _read_artifact_data(artifact_path)


def _markdown_heading_key(value: str) -> str:
    text = re.sub(r"^\s*\d+(?:\.\d+)*[、.．\s　]+", "", str(value or ""))
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


def _markdown_heading_rows(lines: list[str]) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    fence_mark = ""
    for index, line in enumerate(lines):
        fence = _MARKDOWN_FENCE_RE.match(line)
        if fence:
            mark = fence.group("marks")
            if not fence_mark:
                fence_mark = mark
            elif mark[0] == fence_mark[0] and len(mark) >= len(fence_mark):
                fence_mark = ""
            continue
        if fence_mark:
            continue
        heading = _MARKDOWN_ATX_HEADING_RE.match(line)
        if heading:
            rows.append(
                (index, len(heading.group("marks")), heading.group("title").strip())
            )
    return rows


def _normalize_streamed_markdown_section(
    markdown: str,
    instruction: SectionInstruction,
) -> str:
    """Repair relative Markdown headings without spending another model call."""
    title = instruction.section_title.strip()
    if not title:
        raise ValueError("Markdown draft section title must not be empty.")
    lines = (
        str(markdown or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
        .split("\n")
    )
    rows = _markdown_heading_rows(lines)
    title_key = _markdown_heading_key(title)
    root_index = next(
        (
            index
            for index, _, heading_title in rows
            if _markdown_heading_key(heading_title) == title_key
        ),
        -1,
    )

    removed: set[int] = set()
    if root_index >= 0:
        lines[root_index] = f"## {title}"
        removed.update(
            index
            for index, _, heading_title in rows
            if index != root_index and _markdown_heading_key(heading_title) == title_key
        )
    else:
        lines = [f"## {title}", "", *lines]
        root_index = 0
        rows = [
            (index + 2, level, heading_title) for index, level, heading_title in rows
        ]

    child_rows = [
        (index, level, heading_title)
        for index, level, heading_title in rows
        if index != root_index and index not in removed
    ]
    if child_rows:
        shift = 3 - min(level for _, level, _ in child_rows)
        for index, level, heading_title in child_rows:
            lines[index] = f"{'#' * min(6, max(3, level + shift))} {heading_title}"

    result = "\n".join(
        line for index, line in enumerate(lines) if index not in removed
    ).strip()
    top_rows = [
        row for row in _markdown_heading_rows(result.splitlines()) if row[1] <= 2
    ]
    if len(top_rows) != 1 or top_rows[0][1] != 2:
        raise ValueError(_MARKDOWN_DRAFT_ROOT_ERROR)
    if _markdown_heading_key(top_rows[0][2]) != title_key:
        raise ValueError(
            "Markdown draft section heading does not match its content_ref."
        )
    return result


def _result_data(result: dict, key: str) -> Any:
    path = ((result.get("metadata") or {}).get("artifact_paths") or {}).get(key)
    if not path:
        raise ToolExecutionError(f"Writer tool did not return artifact {key!r}.")
    return _read_artifact_data(path)


def _set_document_editable(value: Any, *, stage: str | None = None) -> WriterDocument:
    document = WriterDocument.model_validate(value)
    if stage is not None:
        document.stage = stage
    document.ui_editable = True

    def update_blocks(blocks: list[WriterBlock], level: int = 1) -> None:
        for block in blocks:
            block.editable = block.type != "wechat_opaque"
            if stage is not None:
                block.stage = stage
            heading_level = block.numbering.get("level")
            if block.type == "heading" and (
                not isinstance(heading_level, int)
                or isinstance(heading_level, bool)
                or not 1 <= heading_level <= 9
            ):
                block.numbering["level"] = min(level, 9)
            update_blocks(block.children, level + 1)

    update_blocks(document.blocks)
    return document


def _document_text(document: WriterDocument) -> str:
    return "\n".join(block.content for block in document.iter_blocks() if block.content)


def markdown_to_writer_document(
    markdown: str,
    *,
    document_id: str,
    stage: str = "final",
) -> WriterDocument:
    """Convert the established Markdown subset to Writer IR."""
    return parse_document_markdown(
        markdown,
        document_id=document_id,
        stage=stage,
    )


def markdown_to_lmd(
    markdown: str,
    *,
    document_id: str,
    stage: str = "final",
) -> str:
    """Convert Markdown to a serialized LMD Artifact envelope."""
    return writer_document_to_lmd(
        markdown_to_writer_document(
            markdown,
            document_id=document_id,
            stage=stage,
        )
    )


def lmd_to_markdown(value: str) -> str:
    """Convert serialized LMD to Markdown with the existing Writer converter."""
    return writer_document_to_markdown(writer_document_from_lmd(value))


class WriterArtifactCapabilities:
    WRITER_IR_SCHEMA = WRITER_IR_SCHEMA
    WRITER_BLOCK_SCHEMA = WRITER_BLOCK_SCHEMA

    def render_markdown(self, writer_document_json: str) -> str:
        """Return the current document title and Markdown content."""
        value = _document_value(writer_document_json)
        if isinstance(value, str):
            title_match = re.search(r"^#\s+(.+)$", value, re.MULTILINE)
            view = build_numbering_view_from_markdown(value)
            return _json_dumps(
                {
                    "title": title_match.group(1).strip() if title_match else "",
                    "markdown": materialize_markdown(
                        value, view, compute_numbering(view)
                    ),
                }
            )
        document = WriterDocument.model_validate(value)
        return _json_dumps(
            {
                "title": document.title,
                "markdown": writer_document_to_markdown(document),
            }
        )


__all__ = [
    "WRITER_BLOCK_SCHEMA",
    "WRITER_DATA_MODEL_SCHEMA_PREFIX",
    "WRITER_IR_SCHEMA",
    "WriterArtifactCapabilities",
    "lmd_to_markdown",
    "markdown_to_lmd",
    "markdown_to_writer_document",
    "writer_schema",
]
