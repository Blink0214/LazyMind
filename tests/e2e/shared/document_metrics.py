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


def unwrap_artifact(value: Any) -> Any:
    while isinstance(value, dict) and "data" in value:
        value = value["data"]
    return value


def writer_document(value: Any) -> dict:
    """Return the canonical Writer IR document or an empty mapping."""
    value = unwrap_artifact(value)
    return value if isinstance(value, dict) else {}


def iter_writer_blocks(value: Any):
    """Yield every IR block depth-first from one Writer artifact envelope."""
    document = writer_document(value)

    def walk(items):
        for block in items or []:
            if not isinstance(block, dict):
                continue
            yield block
            yield from walk(block.get("children") or [])

    yield from walk(document.get("blocks") or [])


def writer_document_visible_text(value: Any) -> str:
    document = writer_document(value)
    parts = [str(document.get("title") or "")]
    parts.extend(
        str(block.get("content") or "").strip()
        for block in iter_writer_blocks(document)
        if block.get("content")
    )
    return "\n".join(part for part in parts if part)


def writer_document_headings(value: Any) -> list[str]:
    return [
        str(block.get("content") or "").strip()
        for block in iter_writer_blocks(value)
        if block.get("type") == "heading"
    ]


def writer_image_blocks(value: Any) -> list[dict]:
    return [
        block for block in iter_writer_blocks(value)
        if block.get("type") == "image"
    ]


def is_writer_document(value: Any) -> bool:
    value = unwrap_artifact(value)
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
    document = unwrap_artifact(value)
    title = str(document.get("title") or "").strip()
    body = "\n\n".join(filter(None, (
        _block_markdown(block) for block in document.get("blocks") or []
    )))
    return "\n\n".join(filter(None, (f"# {title}" if title else "", body))) + "\n"


def inline_artifact_to_markdown(value: Any) -> str | None:
    value = unwrap_artifact(value)
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
        if slot.get("selected", True)
        and slot.get("slot_id") in ranked
    ]
    return sorted(
        slots,
        key=lambda slot: ranked.get(
            slot.get("slot_id"), len(ranked),
        ),
    )


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
            "changed_characters": added + deleted,
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


def revision_source_from_trace(trace: dict | None, *, provider_title: bool = False) -> tuple[str | None, dict]:
    """Recover the complete material actually consumed by this run, never live docs.

    Several resources or differing retry inputs are ambiguous: return missing
    rather than silently choosing an attachment/current provider baseline.
    """
    candidates = []
    start = 'Material content to analyze:\n---\n'
    end = '\n---\n\nAnalyze the content above'
    for obs in (trace or {}).get('observations', []):
        attrs = (obs.get('metadata') or {}).get('attributes') or {}
        payloads = []
        try:
            io = attrs.get('lazyllm.io.input')
            io = json.loads(io) if isinstance(io, str) else io
            if isinstance(io, dict):
                payloads.append(io.get('resolved_prompt') or {})
        except (ValueError, TypeError):
            pass
        try:
            diag = attrs.get('lazyllm.diagnostics.llm')
            diag = json.loads(diag) if isinstance(diag, str) else diag
            for attempt in (diag or {}).get('attempts', []):
                req = (attempt.get('request') or {}).get('json')
                payloads.append(json.loads(req) if isinstance(req, str) else req or {})
        except (ValueError, TypeError, AttributeError):
            pass
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for message in payload.get('messages', []):
                content = message.get('content')
                if not isinstance(content, str) or not content.startswith('Analyze the following material for a writing task.') or start not in content:
                    continue
                tail = content.split(start, 1)[1]
                if end not in tail:
                    continue
                source = tail.split(end, 1)[0].strip() + '\n'
                if '<truncated>' in source:
                    continue
                if provider_title:
                    title = re.search(r'^- Resource title: (.+)$', content, re.M)
                    if not title:
                        continue
                    source = '# ' + title.group(1).strip() + '\n\n' + source
                candidates.append((source, obs.get('id')))
    distinct = {visible_text(s) for s, _ in candidates}
    if len(distinct) != 1:
        return None, {'status': 'unavailable', 'reason': 'ambiguous_source' if candidates else 'complete_source_missing'}
    return candidates[0][0], {'status': 'verified', 'source': 'same_run_trace_material',
                             'observation_ids': sorted({i for _, i in candidates if i}),
                             'provider_title_included': provider_title}
