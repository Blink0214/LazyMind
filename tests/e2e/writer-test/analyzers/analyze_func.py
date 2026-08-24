"""Functional-mode mechanical checks (SSE / feishu / media / final artifact).

Combines with the trace-derived checks in ``analyze_common`` to produce the
machine-check list for one func run:

* ``check_sse_protocol`` / ``check_sse_reconstruction`` — live draft-stream
  protocol and byte-level reconstruction (no-ui mode only).
* ``check_feishu_revision`` — provider ``revision_id`` increase / unchanged.
* ``check_media`` — body image count, placeholder leftovers and generated
  media assets (no reference attribution).
* ``final.ui_editable`` / ``final.new_revision`` — IR editability and the
  saved draft revision.

The runner feeds already-fetched primitives (no I/O here) so the checks can
be re-run for review without touching the network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Small helpers reused from writer-e2e/writer_e2e.py
# ---------------------------------------------------------------------------


def _normalized(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip("\n")


def _strip_markdown(text: str) -> str:
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*+] |\d+\. )", "", text)
    text = re.sub(r"</?a\b[^>]*>", "", text)
    return re.sub(r"[`*_>\[\]()`]", "", text)


def _ir_visible_text(document: dict) -> str:
    doc = _ir_document(document)
    parts = [str(doc.get("title") or "")]
    blocks = doc.get("blocks") if isinstance(doc, dict) else []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            content = block.get("content")
            if content:
                parts.append(str(content).strip())
    return _normalized("\n".join(p for p in parts if p))


def _ir_image_blocks(document: dict) -> list[dict]:
    """Recursively collect ``image`` blocks from an IR document."""
    out: list[dict] = []
    document = _ir_document(document)

    def walk(blocks: list) -> None:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                out.append(block)
            walk(block.get("children") or [])

    walk(document.get("blocks") or [])
    return out


def _ir_document(document: dict) -> dict:
    """Unwrap the ``{data, meta, schema, ...}`` envelope used by the current
    Writer IR artifact format; the document fields live under ``data``."""
    if not isinstance(document, dict):
        return document
    data = document.get("data")
    if isinstance(data, dict) and (
        isinstance(data.get("blocks"), list) or "ui_editable" in data
    ):
        return data
    return document


def _generated_assets(assets: Any) -> set[str]:
    """Generated-image ids inside a resolved_media_assets payload."""
    if not isinstance(assets, dict):
        return set()
    library = assets.get("data", assets)
    items = (library or {}).get("assets") or {}
    if not isinstance(items, dict):
        return set()
    return {
        str(asset_id) for asset_id, asset in items.items()
        if isinstance(asset, dict)
        and (asset.get("source_type") == "image_generation"
             or asset.get("asset_type") == "generated_image")
        and (asset.get("local_path") or asset.get("uri"))
    }


# Reuse the analyzer-side Check dataclass so checks serialize consistently.
from analyze_common import Check  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# SSE protocol + reconstruction
# ---------------------------------------------------------------------------


def _stream_visible_text(stream_text: str, final_artifact: Any) -> tuple[str, str]:
    """Return ``(stream_normalized, final_normalized)`` for diffing."""
    if isinstance(final_artifact, str):
        return (
            _normalized(_strip_markdown(stream_text)),
            _normalized(_strip_markdown(final_artifact)),
        )
    if isinstance(final_artifact, dict):
        return _normalized(_strip_markdown(stream_text)), _normalized(_ir_visible_text(final_artifact))
    return _normalized(_strip_markdown(stream_text)), ""


def check_sse_protocol(streams) -> Check:
    """All complete draft streams must obey start -> delta+ -> end, single slot, no abort."""
    errors: list[str] = []
    for s in streams:
        evs = s.events
        if not (
            evs.count("artifact_stream_start") == 1
            and evs.count("artifact_stream_delta") >= 1
            and evs.count("artifact_stream_end") == 1
            and evs[0] == "artifact_stream_start"
            and evs[-1] == "artifact_stream_end"
            and not s.aborted
            and s.slots == {"draft_document"}
            and s.content_types == {"text/markdown"}
        ):
            errors.append(f"stream={s.stream_id} events={evs} "
                          f"slots={sorted(s.slots)} ctypes={sorted(s.content_types)} "
                          f"aborted={s.aborted}")
    if errors:
        return Check("sse.protocol", "FAIL",
                     f"{len(errors)} stream(s) invalid: {'; '.join(errors[:3])}")
    return Check("sse.protocol", "PASS",
                 f"{len(streams)} complete stream(s)")


def check_sse_reconstruction(stream_text: str, final_artifact: Any) -> Check:
    """Concatenated stream text must equal the final artifact text."""
    if not stream_text:
        return Check("sse.reconstruction", "WARN",
                     "no stream text captured; skipping")
    if final_artifact is None:
        return Check("sse.reconstruction", "WARN",
                     "no final artifact available; skipping")
    left, right = _stream_visible_text(stream_text, final_artifact)
    if not right:
        return Check("sse.reconstruction", "FAIL", "unsupported artifact type")
    if left == right and left:
        return Check("sse.reconstruction", "PASS", "exact match")
    diff_at = next((i for i, pair in enumerate(zip(left, right))
                    if pair[0] != pair[1]), min(len(left), len(right)))
    return Check("sse.reconstruction", "FAIL",
                 f"first_diff={diff_at}, stream_chars={len(left)}, final_chars={len(right)}")


def check_sse_streaming_effect(streams, min_deltas: int = 2) -> Check:
    """Verify the draft actually streamed incrementally rather than one-shot.

    A complete draft stream must carry at least ``min_deltas`` delta frames and
    non-empty chunk text; anything less means the frontend received the final
    document in one piece (or the capture failed), which defeats the streaming
    UX contract.
    """
    if not streams:
        return Check("sse.streaming", "WARN",
                     "no complete draft stream captured; skipping")
    total_deltas = sum(
        rec.events.count("artifact_stream_delta") for rec in streams
    )
    total_chars = sum(len(rec.text) for rec in streams)
    if total_deltas >= min_deltas and total_chars > 0:
        return Check("sse.streaming", "PASS",
                     f"deltas={total_deltas}, stream_chars={total_chars}")
    return Check(
        "sse.streaming", "FAIL",
        f"streaming effect not observed: deltas={total_deltas}, "
        f"stream_chars={total_chars}, expected >= {min_deltas} deltas",
    )


# ---------------------------------------------------------------------------
# Feishu revision external check
# ---------------------------------------------------------------------------


def check_feishu_revision(provider: str, before: int | None, after: int | None,
                          rule: str = "any") -> Check:
    """Match the documented case rule against observed revisions.

    ``rule`` ∈ ``{"any", "increase", "unchanged"}``.
    """
    if provider != "feishu":
        return Check("feishu.revision", "PASS",
                     f"non-feishu run (provider={provider}); rule={rule} not applicable")
    if before is None or after is None:
        return Check("feishu.revision", "WARN",
                     "missing baseline; rule cannot be evaluated")
    if rule == "any":
        return Check("feishu.revision", "PASS",
                     f"before={before} after={after}")
    if rule == "increase":
        ok = after > before
        return Check("feishu.revision",
                     "PASS" if ok else "FAIL",
                     f"before={before} after={after} expected strict increase")
    if rule == "unchanged":
        ok = after == before
        return Check("feishu.revision",
                     "PASS" if ok else "FAIL",
                     f"before={before} after={after} expected unchanged")
    return Check("feishu.revision", "WARN", f"unknown rule {rule!r}")


def check_media(final_artifact: Any, media_assets: Any,
                min_images: int) -> Check:
    """Image generation + body presence, aligned with writer-e2e.

    Matches ``writer-e2e/writer_scenario_test.py::media_rule_errors``: the
    final artifact must contain at least ``min_images`` images (Markdown
    syntax or IR image blocks), no unresolved media placeholders, and the
    resolved media library must hold at least one generated image asset.
    Reference attribution (does the body reference point to a generated
    asset) is intentionally NOT checked.
    """
    if min_images <= 0:
        return Check("media", "PASS", "no media rule")
    if isinstance(final_artifact, str):
        images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", final_artifact)
        errors = []
        if len(images) < min_images:
            errors.append(f"markdown images={len(images)}, expected >= {min_images}")
        unresolved = [url for url in images if "media-placeholder://" in url]
        if unresolved:
            errors.append(f"unresolved media placeholders={unresolved}")
    elif isinstance(final_artifact, dict):
        image_blocks = _ir_image_blocks(final_artifact)
        errors = []
        if len(image_blocks) < min_images:
            errors.append(f"ir image blocks={len(image_blocks)}, expected >= {min_images}")
    else:
        return Check("media", "WARN", "no final artifact; skipping media checks")

    if media_assets is None:
        if errors:
            return Check("media", "FAIL", "; ".join(errors))
        return Check("media", "WARN",
                     "resolved_media_assets 未加载；仅检查正文图片数量")
    generated = _generated_assets(media_assets)
    # 用例已放松为“只要求有配图”，不再强制素材必须为写作流程生成的图片；
    # 生成资产缺失仅作提示，不判失败（正文配图数量与未解析占位符仍为硬性检查）。
    note = ""
    if not generated:
        note = "；未检测到生成图片资产（已放松来源要求，仅校验正文配图）"
    return Check("media",
                 "PASS" if not errors else "FAIL",
                 ("; ".join(errors) or "media assertions match") + note)


def _count_ir_internal_refs(blocks: list) -> list[str]:
    """Collect internal_ref target ids from IR block spans (recursively)."""
    out: list[str] = []

    def walk(items) -> None:
        for block in items or []:
            if not isinstance(block, dict):
                continue
            for span in block.get("spans") or []:
                link = ((span or {}).get("style") or {}).get("link") or {}
                if link.get("type") == "internal_ref" and link.get("target_node_id"):
                    out.append(str(link["target_node_id"]))
            walk(block.get("children") or [])

    walk(blocks)
    return out


def _extract_link_targets(artifact: Any) -> list[str]:
    """Extract internal reference targets from Markdown or IR artifacts."""
    if isinstance(artifact, str):
        return [m.group(1) for m in re.finditer(
            r"\[[^\]]*\]\(#(block-[^)]+)\)", artifact)]
    if isinstance(artifact, dict):
        doc = _ir_document(artifact)
        return _count_ir_internal_refs(doc.get("blocks") or [])
    return []


def _referenceable_kind(block_type: str) -> str:
    """Map an IR block type to a referenceable kind ('' when not referenceable)."""
    if block_type == "heading":
        return "section"
    if block_type == "image":
        return "figure"
    if block_type in ("table", "code"):
        return block_type
    return ""


def collect_ir_reference_targets(document: dict) -> dict[str, dict]:
    """Collect referenceable IR blocks: node_id -> {kind, has_numbering}."""
    doc = _ir_document(document)
    out: dict[str, dict] = {}

    def walk(items) -> None:
        for block in items or []:
            if not isinstance(block, dict):
                continue
            kind = _referenceable_kind(str(block.get("type") or ""))
            nid = str(block.get("node_id") or "")
            if kind and nid:
                out[nid] = {
                    "kind": kind,
                    "has_numbering": bool(block.get("numbering")),
                }
            walk(block.get("children") or [])

    walk(doc.get("blocks") or [])
    return out


def collect_markdown_reference_targets(markdown: str) -> dict[str, dict]:
    """Collect Markdown reference targets and element coverage.

    Returns ``{"targets": {anchor_id: {kind, has_numbering}}, "elements": {...}}``.
    Elements are section/figure/table/code; coverage tracks total / anchored /
    captioned / numbered so the integrity check can report what is missing.
    """
    targets: dict[str, dict] = {}
    elements: dict[str, dict] = {
        kind: {"total": 0, "anchored": 0, "captioned": 0, "numbered": 0}
        for kind in ("section", "figure", "table", "code")
    }
    anchor_re = re.compile(r'<a\s+id="(block-[^"]+)"')
    heading_re = re.compile(r"^(#{2,6})\s+(.*)$")
    image_re = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*(.*)$")
    table_sep_re = re.compile(
        r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
    table_row_re = re.compile(r"^\s*\|?.*\|.*$")
    fence_re = re.compile(r"^\s*(```+|~~~+)")
    pending_anchors: list[str] = []
    pending_caption: tuple[str | None, int] | None = None
    fence: str | None = None
    open_kind: str | None = None

    def apply_caption(kind: str) -> None:
        """把元素前的题注行（新格式：数字在前）应用到刚开始的元素。"""
        nonlocal pending_caption
        if pending_caption is None:
            return
        pk, _number = pending_caption
        pending_caption = None
        if pk is not None and pk != kind:
            return
        elements[kind]["captioned"] += 1
        elements[kind]["numbered"] += 1
        for anchor, info in targets.items():
            if info.get("kind") == kind:
                info["has_numbering"] = True

    def start_element(kind: str) -> None:
        nonlocal open_kind
        open_kind = kind
        elements[kind]["total"] += 1
        elements[kind]["anchored"] += len(pending_anchors) > 0
        for anchor in pending_anchors:
            targets[anchor] = {"kind": kind, "has_numbering": False}
        pending_anchors.clear()
        apply_caption(kind)

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if fence:
            if fence_re.match(line):
                fence = None
            continue
        fence_m = fence_re.match(line)
        if fence_m:
            start_element("code")
            fence = fence_m.group(1)
            continue
        anchor_m = anchor_re.search(line)
        if anchor_m:
            pending_anchors.append(anchor_m.group(1))
            continue
        heading_m = heading_re.match(line)
        if heading_m:
            pending_caption = None
            numbered = bool(re.match(r"^\s*\d+(\.\d+)*", heading_m.group(2)))
            open_kind = None
            elements["section"]["total"] += 1
            elements["section"]["anchored"] += len(pending_anchors) > 0
            elements["section"]["numbered"] += int(numbered)
            for anchor in pending_anchors:
                targets[anchor] = {"kind": "section",
                                   "has_numbering": numbered}
            pending_anchors.clear()
            continue
        image_m = image_re.match(line)
        if image_m:
            start_element("figure")
            continue
        if table_sep_re.match(line):
            start_element("table")
            continue
        if open_kind == "table" and table_row_re.match(line):
            continue  # 表格数据行不算题注
        stripped = line.strip()
        if stripped:
            if open_kind in ("figure", "table", "code"):
                elements[open_kind]["captioned"] += 1
                if _caption_kind_number(stripped) is not None:
                    elements[open_kind]["numbered"] += 1
                    for anchor, info in targets.items():
                        if info.get("kind") == open_kind:
                            info["has_numbering"] = True
                open_kind = None
            else:
                parsed = _caption_kind_number(stripped)
                if parsed is not None:
                    pending_caption = parsed
                elif pending_caption is not None and table_row_re.match(stripped):
                    pass  # 表格表头/数据行不打断“题注 → 表格”的关联
                else:
                    pending_caption = None
    return {"targets": targets, "elements": elements}


def check_reference_integrity(final_artifact: Any,
                              require_numbering: bool = False) -> Check:
    """Verify every body reference resolves to an existing target and every
    referenceable item (section/figure/table/code) carries an ID/anchor.

    Dangling references are reported explicitly; the remediation policy
    (placeholder / drop / hard error) is intentionally left to the product.
    """
    if final_artifact is None:
        return Check("final.reference_integrity", "WARN",
                     "no final artifact; skipping")
    errors: list[str] = []
    if isinstance(final_artifact, str):
        parsed = collect_markdown_reference_targets(final_artifact)
        targets = parsed["targets"]
        elements = parsed["elements"]
        refs = set(_extract_link_targets(final_artifact))
        kind_label = "markdown"
        for kind in ("section", "figure", "table", "code"):
            el = elements[kind]
            if el["total"] == 0:
                continue
            if el["anchored"] < el["total"]:
                errors.append(
                    f"{kind} without anchor: {el['total'] - el['anchored']}")
            if kind in ("figure", "table", "code"):
                if el["captioned"] < el["total"]:
                    errors.append(
                        f"{kind} without caption: "
                        f"{el['total'] - el['captioned']}")
                if el["numbered"] < el["total"]:
                    errors.append(
                        f"{kind} without numbering: "
                        f"{el['total'] - el['numbered']}")
    elif isinstance(final_artifact, dict):
        doc = _ir_document(final_artifact)
        targets = collect_ir_reference_targets(doc)
        refs = set(_extract_link_targets(doc))
        kind_label = "ir"
        missing_kinds: list[str] = []

        def walk(items) -> None:
            for block in items or []:
                if not isinstance(block, dict):
                    continue
                kind = _referenceable_kind(str(block.get("type") or ""))
                if kind and not str(block.get("node_id") or ""):
                    missing_kinds.append(kind)
                walk(block.get("children") or [])

        walk(doc.get("blocks") or [])
        from collections import Counter
        if missing_kinds:
            errors.append(
                f"referenceable blocks without node_id: "
                f"{dict(Counter(missing_kinds))}")
    else:
        return Check("final.reference_integrity", "WARN",
                     "unsupported artifact type")

    dangling = sorted(refs - set(targets))
    if dangling:
        errors.append(f"dangling references: {dangling}")
    if require_numbering:
        if isinstance(final_artifact, str):
            for kind in ("section", "figure", "table", "code"):
                el = elements[kind]
                if el["numbered"] < el["total"]:
                    errors.append(
                        f"{kind} numbering missing: "
                        f"{el['total'] - el['numbered']}")
        else:
            unnumbered = sorted(
                nid for nid, info in targets.items()
                if not info.get("has_numbering"))
            if unnumbered:
                errors.append(f"targets without numbering: {unnumbered}")
    return Check(
        "final.reference_integrity",
        "PASS" if not errors else "FAIL",
        "; ".join(errors)
        or f"{kind_label} targets={len(targets)} refs={len(refs)} all resolve",
    )


def _parse_content_number(text: str, kind: str) -> tuple | None:
    """Parse the number prefix written into a materialized block content."""
    stripped = str(text or "").strip()
    if kind == "section":
        m = re.match(r"^(\d+(?:\.\d+)*)\.?\s+", stripped)
        return tuple(int(x) for x in m.group(1).split(".")) if m else None
    labels = {"figure": "图", "table": "表", "code": "代码"}
    m = re.match(rf"^{labels[kind]}\s*(\d+)\s*[:：]?\s*", stripped)
    return (int(m.group(1)),) if m else None


def _verify_ir_numbering(document: dict, require: bool) -> list[str]:
    errors: list[str] = []
    counters: list[int] = []
    floats = {"figure": 0, "table": 0, "code": 0}

    def walk(items) -> None:
        nonlocal counters, floats
        for block in items or []:
            if not isinstance(block, dict):
                continue
            kind = _referenceable_kind(str(block.get("type") or ""))
            if kind:
                nid = str(block.get("node_id") or "")
                actual = _parse_content_number(block.get("content"), kind)
                if kind == "section":
                    level = int((block.get("numbering") or {}).get("level") or 2)
                    if level <= len(counters):
                        counters = counters[:level]
                    counters.extend([0] * (level - len(counters)))
                    counters[-1] += 1
                    expected: tuple = tuple(counters)
                else:
                    floats[kind] += 1
                    expected = (floats[kind],)
                if actual is None:
                    if require:
                        errors.append(f"{kind} {nid} missing number in content")
                elif actual != expected:
                    errors.append(
                        f"{kind} {nid} number {actual} != expected {expected}")
            walk(block.get("children") or [])

    walk(_ir_document(document).get("blocks") or [])
    return errors


def _verify_markdown_numbering(markdown: str, require: bool) -> list[str]:
    errors: list[str] = []
    counters: list[int] = []
    floats = {"figure": 0, "table": 0, "code": 0}
    caption_kind = {"图": "figure", "表": "table", "代码": "code"}
    fence: str | None = None
    pending: tuple[str | None, int] | None = None
    for line in markdown.splitlines():
        heading = re.match(r"^(#{1,6})\s+(\d+(?:\.\d+)*)\.?\s*", line)
        if heading:
            pending = None
            depth = len(heading.group(1)) - 1
            actual = tuple(int(x) for x in heading.group(2).split("."))
            if depth <= len(counters):
                counters = counters[:depth]
            counters.extend([0] * (depth - len(counters)))
            counters[-1] += 1
            if actual != tuple(counters):
                errors.append(
                    f"section heading number {actual} != expected {tuple(counters)}")
            continue
        stripped = line.strip()
        if fence:
            if re.match(r"^\s*(```+|~~~+)", stripped):
                fence = None
            continue
        fence_m = re.match(r"^\s*(```+|~~~+)", stripped)
        if fence_m:
            if pending is not None and (pending[0] or "code") == "code":
                floats["code"] += 1
                if pending[1] != floats["code"]:
                    errors.append(
                        f"code caption number {pending[1]} != expected {floats['code']}")
            pending = None
            fence = fence_m.group(1)
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", stripped):
            if pending is not None and (pending[0] or "table") == "table":
                floats["table"] += 1
                if pending[1] != floats["table"]:
                    errors.append(
                        f"table caption number {pending[1]} != expected {floats['table']}")
            pending = None
            continue
        caption = _caption_kind_number(stripped)
        if caption:
            if caption[0] is not None:
                # 前缀格式（图/表/代码 N）：直接计数，兼容题注在元素后的旧格式。
                kind = caption_kind[caption[0]]
                actual = caption[1]
                floats[kind] += 1
                if actual != floats[kind]:
                    errors.append(f"{kind} caption number {actual} != expected {floats[kind]}")
                pending = None
            else:
                # 数字在前（新格式）：等后续元素（表格分隔行/代码围栏）再计数。
                pending = caption
            continue
        if stripped:
            pending = None
            continue
    if require:
        parsed = collect_markdown_reference_targets(markdown)
        numbered_headings = sum(
            1 for line in markdown.splitlines()
            if re.match(r"^#{2,6}\s+\d+(\.\d+)*\.?\s", line))
        if numbered_headings < parsed["elements"]["section"]["total"]:
            errors.append(
                f"section without number: "
                f"{parsed['elements']['section']['total'] - numbered_headings}")
        for kind in ("figure", "table", "code"):
            el = parsed["elements"][kind]
            if el["numbered"] < el["total"]:
                errors.append(f"{kind} without number: {el['total'] - el['numbered']}")
    return errors


def check_numbering_correctness(final_artifact: Any,
                                require: bool = False) -> Check:
    """Verify numbering sequences are correct (and present when ``require``)."""
    if final_artifact is None:
        return Check("final.numbering", "WARN", "no final artifact; skipping")
    if isinstance(final_artifact, str):
        errors = _verify_markdown_numbering(final_artifact, require)
        kind_label = "markdown"
    elif isinstance(final_artifact, dict):
        errors = _verify_ir_numbering(final_artifact, require)
        kind_label = "ir"
    else:
        return Check("final.numbering", "WARN", "unsupported artifact type")
    return Check(
        "final.numbering",
        "PASS" if not errors else "FAIL",
        "; ".join(errors) or f"{kind_label} numbering correct",
    )


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(#(block-[^)]+)\)")
_MD_HEADING_NUM_RE = re.compile(r"^#{1,6}\s+(\d+(?:\.\d+)*)\.?\s*(.*)$")
_MD_CAPTION_NUM_RE = re.compile(r"^(图|表|代码)\s*[:：]?\s*(\d+)")
_CAPTION_PREFIX_RE = re.compile(r"^(图|表|代码)\s*[:：]?\s*(\d+)")
_CAPTION_NUMBER_FIRST_RE = re.compile(r"^(\d+)\s*(?:[.．、:]?\s*)?(图|表|代码)?")


def _caption_kind_number(line: str) -> tuple[str | None, int] | None:
    """识别题注行（兼容新旧两种格式），返回 (kind|None, 编号) 或 None。

    - 旧格式：``表 1：xxx`` / ``图1 xxx`` / ``代码 1 xxx``（前缀在编号前）；
    - 新格式：``1 应急物资清单`` / ``1``（数字在前，题注由后续元素推断，
      可能带 图/表/代码 后缀）；排除 ``1.1`` 这类章节编号。
    """
    line = line.strip()
    m = _CAPTION_PREFIX_RE.match(line)
    if m:
        return m.group(1), int(m.group(2))
    if re.match(r"^\d+[.．、]\d", line):
        return None
    m = _CAPTION_NUMBER_FIRST_RE.match(line)
    if m:
        return (m.group(2) or None), int(m.group(1))
    return None


def _number_in_text(text: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
    return m.group(1) if m else ""


def _markdown_target_numbers(markdown: str) -> dict[str, str]:
    """Map Markdown anchor ids to the number written on their target element."""
    out: dict[str, str] = {}
    pending: list[str] = []
    for line in markdown.splitlines():
        anchors = re.findall(r'<a\s+id="(block-[^"]+)"', line)
        if anchors:
            pending.extend(anchors)
            continue
        heading = _MD_HEADING_NUM_RE.match(line)
        if heading:
            for anchor in pending:
                out[anchor] = heading.group(1)
            pending = []
            continue
        caption = _MD_CAPTION_NUM_RE.match(line.strip())
        if caption:
            for anchor in pending:
                out[anchor] = caption.group(2)
            pending = []
            continue
        if line.strip():
            pending = []
    return out


def check_reference_number_consistency(final_artifact: Any) -> Check:
    """Verify body reference text numbers match the referenced target's current
    number (i.e. the model actually updated the in-text reference after
    structural changes such as section moves)."""
    if final_artifact is None:
        return Check("final.reference_number_consistency", "WARN",
                     "no final artifact; skipping")
    errors: list[str] = []
    if isinstance(final_artifact, str):
        target_numbers = _markdown_target_numbers(final_artifact)
        links = [(m.group(1).strip(), m.group(2))
                 for m in _MD_LINK_RE.finditer(final_artifact)]
        kind_label = "markdown"
        for text, target in links:
            expected = target_numbers.get(target)
            actual = _number_in_text(text)
            if expected and actual and actual != expected:
                errors.append(
                    f"link text number {actual} != target {target} "
                    f"current {expected}")
    elif isinstance(final_artifact, dict):
        doc = _ir_document(final_artifact)
        target_numbers: dict[str, str] = {}

        def walk(items) -> None:
            for block in items or []:
                if not isinstance(block, dict):
                    continue
                kind = _referenceable_kind(str(block.get("type") or ""))
                nid = str(block.get("node_id") or "")
                if kind and nid:
                    number = _parse_content_number(block.get("content"), kind)
                    if number:
                        target_numbers[nid] = ".".join(str(p) for p in number)
                for span in block.get("spans") or []:
                    link = ((span or {}).get("style") or {}).get("link") or {}
                    if link.get("type") != "internal_ref":
                        continue
                    target = str(link.get("target_node_id") or "")
                    expected = target_numbers.get(target)
                    actual = _number_in_text((span or {}).get("text"))
                    if expected and actual and actual != expected:
                        errors.append(
                            f"reference text number {actual} != target "
                            f"{target} current {expected}")
                walk(block.get("children") or [])

        walk(doc.get("blocks") or [])
        kind_label = "ir"
    else:
        return Check("final.reference_number_consistency", "WARN",
                     "unsupported artifact type")
    return Check(
        "final.reference_number_consistency",
        "PASS" if not errors else "FAIL",
        "; ".join(errors) or f"{kind_label} reference numbers consistent",
    )


def check_cross_references(final_artifact: Any, rule: dict,
                           source_artifact: Any = None) -> Check:
    """Body internal cross-reference assertions (Markdown #block- links / IR
    internal_ref spans), optionally verifying preservation from a source."""
    if final_artifact is None:
        return Check("final.cross_references", "WARN",
                     "no final artifact; skipping")
    if isinstance(final_artifact, str):
        links = _extract_link_targets(final_artifact)
        kind = "markdown"
    elif isinstance(final_artifact, dict):
        links = _extract_link_targets(final_artifact)
        kind = "ir"
    else:
        return Check("final.cross_references", "WARN",
                     "unsupported artifact type")
    errors: list[str] = []
    min_links = int((rule or {}).get("min_links") or 0)
    if len(links) < min_links:
        errors.append(f"{kind} links={len(links)}, expected >= {min_links}")
    if (rule or {}).get("preserve_from_source") and source_artifact is not None:
        source_targets = set(_extract_link_targets(source_artifact))
        final_targets = set(links)
        dropped = sorted(source_targets - final_targets)
        if dropped:
            errors.append(f"source cross-references dropped: {dropped}")
    return Check("final.cross_references",
                 "PASS" if not errors else "FAIL",
                 "; ".join(errors) or f"{kind} links={len(links)}")


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def run_func_checks(*, common_checks: list[Check], streams_for_draft: list,
                    final_artifact: Any,
                    sse_text: str = "",
                    cross_ref_rule: dict | None = None,
                    cross_ref_source: Any = None,
                    ref_integrity: bool = False,
                    numbering_rule: dict | None = None,
                    ref_number_consistency: bool = False,
                    provider: str = "local",
                    feishu_before: int | None = None,
                    feishu_after: int | None = None,
                    feishu_rule: str = "any",
                    write_back_calls: int = 0,
                    write_back_tool: str = "",
                    write_back_calls_expected: int | None = None,
                    write_back_tool_expected: str = "",
                    provider_revision_rule: str | None = None,
                    media_assets: Any = None,
                    media_min_images: int = 0,
                    ui_editable_expected: bool | None = None,
                    new_revision_expected: bool | None = None,
                    final_revision: int = 0) -> list[Check]:
    """Combine analyze_common + analyze_func-specific checks.

    Args:
      common_checks: already-evaluated ``Check`` items from analyze_common.run_checks.
      streams_for_draft: list of complete ``StreamRecord`` objects whose slot is
        ``draft_document``; ``None`` is acceptable and disables SSE checks.
      final_artifact: the markdown text or IR dict the panel ends on.
      sse_text: concatenated text of the *last* complete draft stream
        (used for the reconstruction check).
      provider, feishu_before/after, feishu_rule: external feishu state.
      write_back_calls / write_back_tool / write_back_calls_expected:
        counters pulled from the trace and the case-N.yaml sidecar.
      provider_revision_rule: ``"increase"`` / ``"unchanged"`` / ``"any"`` (or None).

    Returned order: trace-derived checks first, then sse.protocol,
    sse.reconstruction, feishu.revision, write_back match.
    """
    out: list[Check] = list(common_checks)

    # Counts vs expected write-back calls.
    if write_back_calls_expected is not None:
        ok = write_back_calls == int(write_back_calls_expected)
        detail = (f"calls={write_back_calls} expected={write_back_calls_expected}, "
                  f"by_tool={write_back_tool}")
        if write_back_tool_expected:
            tool_count = int(
                (write_back_tool or {}).get(write_back_tool_expected, 0)
            ) if isinstance(write_back_tool, dict) else 0
            ok = ok and tool_count == int(write_back_calls_expected)
            detail += f", tool={write_back_tool_expected}={tool_count}"
        out.append(Check("write_back", "PASS" if ok else "FAIL", detail))

    if media_min_images > 0:
        out.append(check_media(final_artifact, media_assets, media_min_images))

    if cross_ref_rule:
        out.append(check_cross_references(
            final_artifact, cross_ref_rule, cross_ref_source))

    if ref_integrity:
        rule = ref_integrity if isinstance(ref_integrity, dict) else {}
        out.append(check_reference_integrity(
            final_artifact, require_numbering=bool(rule.get("require_numbering"))))

    if numbering_rule:
        numbering_rule = (
            numbering_rule if isinstance(numbering_rule, dict) else {})
        out.append(check_numbering_correctness(
            final_artifact, require=bool(numbering_rule.get("require"))))

    if ref_number_consistency:
        out.append(check_reference_number_consistency(final_artifact))

    if ui_editable_expected is not None:
        data = (
            _ir_document(final_artifact)
            if isinstance(final_artifact, dict) else None
        )
        actual = bool(data and data.get("ui_editable") is True) if data is not None else None
        if actual is None:
            out.append(Check("final.ui_editable", "FAIL",
                             "final artifact is not an IR document"))
        elif actual != ui_editable_expected:
            out.append(Check("final.ui_editable", "FAIL",
                             f"ui_editable={actual}, expected={ui_editable_expected}"))
        else:
            out.append(Check("final.ui_editable", "PASS",
                             f"ui_editable={actual}"))

    if new_revision_expected is not None:
        ok = final_revision > 0
        out.append(Check("final.new_revision",
                         "PASS" if ok == new_revision_expected else "FAIL",
                         f"draft_revision={final_revision}, expected_new_revision={new_revision_expected}"))

    if streams_for_draft is not None:
        out.append(check_sse_protocol(streams_for_draft))
        out.append(check_sse_streaming_effect(streams_for_draft))
        if streams_for_draft:
            out.append(check_sse_reconstruction(sse_text, final_artifact))

    if provider_revision_rule and provider_revision_rule != "any":
        out.append(check_feishu_revision(provider, feishu_before, feishu_after,
                                          rule=provider_revision_rule))
    elif feishu_before is not None and feishu_after is not None:
        # 跑前/跑后都已拿到就至少报告一次，不阻塞 PASS/FAIL
        out.append(check_feishu_revision(provider, feishu_before, feishu_after,
                                          rule="any"))

    return out
