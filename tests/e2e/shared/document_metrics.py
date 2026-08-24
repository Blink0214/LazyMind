"""Final-artifact / document-size metrics shared by both test suites.

Ported from ``writer-benchmark/scripts/document_metrics.py`` so the unified
``writer-test`` stack (``shared.api`` / ``analyze_perf``) renders IR and
Markdown finals identically to the performance suite.
"""

from __future__ import annotations

import difflib
import html
import json
import re
from typing import Any


FINAL_SLOT_PRIORITY = (
    "delivered_markdown", "final_document_md", "writing_output_md",
    "writing_output", "final_document", "draft_document",
)


def _unwrap(value: Any) -> Any:
    while isinstance(value, dict) and "data" in value:
        value = value["data"]
    return value


def is_writer_document(value: Any) -> bool:
    value = _unwrap(value)
    return isinstance(value, dict) and isinstance(value.get("blocks"), list)


def _block_markdown(block: Any, depth: int = 0) -> str:
    if not isinstance(block, dict):
        return ""
    children = "\n\n".join(filter(None, (
        _block_markdown(child, depth + 1) for child in block.get("children") or []
    )))
    if block.get("type") == "document":
        return children
    content = str(block.get("content") or "").strip()
    kind = block.get("type")
    numbering = block.get("numbering") or {}
    if kind == "heading" and content:
        level = min(6, max(1, int(numbering.get("level") or 2)))
        content = f"{'#' * level} {content}"
    elif kind == "list_item" and content:
        marker = "1." if numbering.get("ordered") else "-"
        content = f"{'  ' * depth}{marker} {content}"
    elif kind == "quote" and content:
        content = "\n".join(f"> {line}" for line in content.splitlines())
    elif kind == "code" and content:
        content = f"```\n{content}\n```"
    elif kind == "divider":
        content = "---"
    return "\n\n".join(filter(None, (content, children)))


def writer_document_to_markdown(value: Any) -> str:
    document = _unwrap(value)
    title = str(document.get("title") or "").strip()
    body = "\n\n".join(filter(None, (
        _block_markdown(block) for block in document.get("blocks") or []
    )))
    return "\n\n".join(filter(None, (f"# {title}" if title else "", body))) + "\n"


def inline_artifact_to_markdown(value: Any) -> str | None:
    value = _unwrap(value)
    if is_writer_document(value):
        return writer_document_to_markdown(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            if is_writer_document(parsed):
                return writer_document_to_markdown(parsed)
        return value
    if isinstance(value, dict):
        if "text" in value:
            return str(value["text"])
        if is_writer_document(value):
            return writer_document_to_markdown(value)
    return None


def selected_final_slots(session: dict) -> list[dict]:
    ranked = {name: index for index, name in enumerate(FINAL_SLOT_PRIORITY)}
    slots = [
        slot for slot in (session.get("slots") or [])
        if slot.get("selected", True) and slot.get("slot") in ranked
    ]
    return sorted(slots, key=lambda slot: ranked.get(slot.get("slot"), len(ranked)))


def visible_text(markdown: str) -> str:
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", markdown, flags=re.S)
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}|>|[-+*]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$", "", text, flags=re.M)
    text = re.sub(r"[`*_~]", "", text)
    return re.sub(r"\s+", "", text)


def document_stats(final_markdown: str, original_markdown: str | None = None) -> dict:
    final_visible = visible_text(final_markdown)
    result = {
        "final": {
            "visible_characters": len(final_visible),
            "markdown_characters": len(final_markdown),
            "lines": len(final_markdown.splitlines()),
            "bytes": len(final_markdown.encode("utf-8")),
        },
        "revision": {"present": original_markdown is not None},
    }
    if original_markdown is not None:
        original_visible = visible_text(original_markdown)
        matcher = difflib.SequenceMatcher(None, original_visible, final_visible, autojunk=False)
        added = deleted = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("delete", "replace"):
                deleted += i2 - i1
            if tag in ("insert", "replace"):
                added += j2 - j1
        result["original"] = {"visible_characters": len(original_visible)}
        result["revision"].update({
            "changed_original_characters": deleted,
            "changed_final_characters": added,
            "original_coverage": round(deleted / len(original_visible), 6) if original_visible else 0,
            "net_character_change": len(final_visible) - len(original_visible),
        })
    return result


def original_from_prompt(text: str) -> str | None:
    marker = text.find("正文如下")
    tail = text[marker:] if marker >= 0 else text
    blocks = re.findall(r"```(?:markdown|md|text)?\s*\n(.*?)```", tail, flags=re.S | re.I)
    return blocks[-1].strip() + "\n" if blocks else None
