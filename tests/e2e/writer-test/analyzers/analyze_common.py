"""Trace-derived structural facts for AI Writer runs.

This is the shared analyzer used by both ``perf`` and ``func`` runners. Given a
trace JSON (Langfuse shape, or local OTel normalized to it by ``shared.observability``),
it derives trace-owned structural facts. Route, steps, slots, and provider are
filled from the authoritative Core workflow session by the functional runner:

* ``tool_sequence`` — chronological writer_*/patch_artifact/advance_step span
                       names; same ordering the LLM agent issued them.
* ``workspace_facts`` — directly traced control facts for each workspace:
                       operation, structure mode, representation and next step.
* ``write_back``    — concrete write-back function counts, taken directly
                       from spans when exposed or inferred from the traced
                       workspace operation and ``document_write_result``.
* ``write_back_modes`` — abstract write modes such as ``replace``.

Limitations the analyzer does *not* hide:

* The SSE stream protocol (start/delta/end) and the SSE reconstruction check
  are reported only through the live SSE capture module; the analyzer cannot
  recover them from trace alone.
* Feishu's external ``revision_id`` increment is also invisible to the trace
  and lives in ``shared.feishu``. ``analyze_func`` combines the trace-derived
  view here with those external fetches.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Trace loading
# ---------------------------------------------------------------------------


def load_trace(source) -> dict:
    """Accept a path to a trace JSON file, or a dict already in memory.

    The Langfuse payload returned by ``fetch_trace.fetch_via_backend`` (or the
    version dumped to disk by ``writer-benchmark/scripts/full_run.sh``) shares
    the same shape; both are accepted.
    """
    if isinstance(source, dict):
        return source
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"trace file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Structural extraction
# ---------------------------------------------------------------------------


def _name(obs: dict) -> str:
    return str(obs.get("name") or "")


def _attrs(obs: dict) -> dict:
    md = obs.get("metadata") or {}
    return (md.get("attributes") or {}) if isinstance(md, dict) else {}


def _io_in(obs: dict) -> str:
    return str(_attrs(obs).get("lazyllm.io.input") or "")


def _io_out(obs: dict) -> str:
    return str(_attrs(obs).get("lazyllm.io.output") or "")


WRITER_PHASES = ("prepare", "outline", "write_document")
WRITER_PHASE_FUNCTIONS = {
    "prepare": {
        "writer_prepare_workspace", "writer_build_writing_task",
        "writer_load_document", "writer_load_local_document",
        "writer_profile_resources", "writer_collect_available_media",
        "writer_create_writing_context",
    },
    "outline": {
        "writer_outline_workspace", "writer_prepare_outline",
        "writer_generate_outline", "writer_generate_section_instructions",
        "writer_generate_rewrite_outline",
        "writer_generate_rewrite_section_instructions",
    },
    "write_document": {
        "writer_draft_workspace", "writer_generate_draft_blocks",
        "writer_generate_draft_blocks_markdown",
        "writer_generate_draft_document",
        "writer_generate_draft_document_markdown",
        "writer_locate_revision_target", "writer_generate_modify_plan",
        "writer_generate_revision_set", "writer_apply_revision",
        "writer_publish_revision", "writer_write_document",
        "writer_replace_document", "writer_sync_document",
        "writer_append_document", "writer_create_document",
        "writer_preview_selection_rewrite", "writer_resolve_visual_media",
        "writer_resolve_revision_media", "writer_save_document",
        "writer_render_document", "writer_export_markdown",
        "writer_update_writing_context",
    },
}


def step_id_from_observation(obs: dict) -> str:
    """Normalize a workflow step transition across supported trace backends."""
    output = obs.get("output") or {}
    results = output.get("attempt_results") if isinstance(output, dict) else []
    results = results or []
    if not results:
        raw = _io_out(obs)
        try:
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
        except json.JSONDecodeError:
            payload = {}
        results = (payload or {}).get("attempt_results") or []
    return str((results[0] or {}).get("step_id") or "") if results else ""


def _tool_sequence(trace: dict) -> list[str]:
    """Chronological writer_*/patch_artifact/advance_step spans."""
    seq: list[str] = []
    for obs in sorted(trace.get("observations") or [],
                      key=lambda x: x.get("startTime") or ""):
        n = _name(obs)
        if n.startswith("writer_") or n in ("patch_artifact", "advance_step"):
            seq.append(n)
    return seq


_WORKSPACE_PHASES = {
    "writer_prepare_workspace": "prepare",
    "writer_outline_workspace": "outline",
    "writer_draft_workspace": "write_document",
}


def _workspace_facts(trace: dict) -> dict[str, dict[str, Any]]:
    """Return the latest successful, directly observed output facts per workspace."""
    facts: dict[str, dict[str, Any]] = {}
    for obs in sorted(trace.get("observations") or [],
                      key=lambda item: item.get("startTime") or ""):
        phase = _WORKSPACE_PHASES.get(_name(obs))
        if not phase or str(obs.get("level") or "DEFAULT") == "ERROR":
            continue
        raw = _io_out(obs)
        try:
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        observed: dict[str, Any] = {}
        for key in ("status", "operation", "structure_mode", "representation"):
            if payload.get(key) is not None:
                observed[key] = payload[key]
        next_step = payload.get("next_step")
        if next_step is None and isinstance(payload.get("control"), dict):
            next_step = payload["control"].get("next_step")
        if next_step is not None:
            observed["next_step"] = next_step
        saved_keys = payload.get("saved_artifact_keys")
        if isinstance(saved_keys, list):
            observed["saved_artifact_keys"] = [str(item) for item in saved_keys]
        if observed:
            facts[phase] = observed
    return facts


# ---------------------------------------------------------------------------
# Public analysis result
# ---------------------------------------------------------------------------


@dataclass
class SlotRecord:
    slot: str
    extension: str = ""
    selected: bool = True
    stage: str | None = None
    provider: str = ""
    revisions: list[int] = field(default_factory=list)
    # Small, non-sensitive facts extracted from the loaded artifact.  The
    # runner deliberately does not put the artifact body in facts.json.
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def max_revision(self) -> int:
        return max(self.revisions) if self.revisions else 0


@dataclass
class TraceAnalysis:
    trace_id: str
    session_id: str
    latency_s: float
    route: str
    step_path: list[str]
    tool_sequence: list[str]
    slot_view: dict[str, SlotRecord]
    provider: str
    write_back: dict[str, int]
    workspace_facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    write_back_modes: dict[str, int] = field(default_factory=dict)
    write_back_evidence: list[dict[str, str]] = field(default_factory=list)
    modify_types: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    llm_call_count: int = 0
    image_call_count: int = 0

    def to_dict(self) -> dict:
        out = asdict(self)
        out["slot_view"] = {
            sid: {**asdict(rec), "max_revision": rec.max_revision}
            for sid, rec in self.slot_view.items()
        }
        return out


def analyze(trace: dict) -> TraceAnalysis:
    """Compute structural facts about the Writer run embedded in ``trace``.

    The current writer workflow encapsulates each stage in one workspace tool,
    so per-slot facts and modify types are merged from the core session by the
    runner; this trace-only analyzer keeps tools, workspace facts, model
    metadata, and write-back evidence.
    """
    tools = _tool_sequence(trace)
    workspace_facts = _workspace_facts(trace)
    draft_operation = str(
        (workspace_facts.get("write_document") or {}).get("operation") or ""
    )
    models: set[str] = set()
    providers: set[str] = set()
    llm_calls = 0
    image_calls = 0
    for obs in trace.get("observations") or []:
        name = str(obs.get("name") or "")
        attrs = ((obs.get("metadata") or {}).get("attributes") or {})
        if name == "llm":
            llm_calls += 1
            prompt = str(attrs.get("lazyllm.io.input") or "")
            # 具体模型名在 input.resolved_prompt.model（业务代码配置的默认对话模型）。
            concrete = ""
            try:
                prompt_payload = json.loads(prompt)
                resolved = (prompt_payload or {}).get("resolved_prompt") or {}
                concrete = str((resolved or {}).get("model") or "")
            except (json.JSONDecodeError, AttributeError):
                pass
            if concrete:
                models.add(concrete)
            else:
                model = attrs.get("gen_ai.request.model") or attrs.get("lazyllm.entity.config.model")
                if model:
                    models.add(str(model))
            # 系统提示词形如 "... provided by Minimax. You are ..."，
            # 只在句子边界截断，避免把整句提示词当成供应商名。
            m = re.search(r"provided by\s+([^.，。；;：]+)", prompt)
            if m:
                providers.add(m.group(1).strip())
        elif name == "image_generator":
            image_calls += 1

    write_back = Counter()
    write_back_modes = Counter()
    write_back_evidence: list[dict[str, str]] = []
    for obs in sorted(trace.get("observations") or [],
                      key=lambda item: item.get("startTime") or ""):
        name = _name(obs)
        if name == "writer_write_document":
            raw = _io_in(obs)
            try:
                payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
            except json.JSONDecodeError:
                payload = {}
            kwargs = payload.get("kwargs") if isinstance(payload, dict) else {}
            mode = str(
                (kwargs or {}).get("mode")
                or (payload.get("mode") if isinstance(payload, dict) else "")
                or "replace"
            )
            write_back[name] += 1
            write_back_modes[mode] += 1
            write_back_evidence.append({
                "function": name, "mode": mode, "source": "direct_span",
            })
        elif name == "writer_publish_revision":
            write_back[name] += 1
            write_back_modes["revision"] += 1
            write_back_evidence.append({
                "function": name, "mode": "revision", "source": "direct_span",
            })

    # 当 workspace 封装屏蔽了内部 span 时，trace 仍会在顶层输出里记录
    # operation 与 document_write_result。当前 workspace 合同中：revise 调用
    # writer_publish_revision，generate/rewrite 调用 writer_write_document(mode=replace)。
    if draft_operation and not write_back:
        for obs in trace.get("observations") or []:
            if _name(obs) != "writer_draft_workspace":
                continue
            raw = _io_out(obs)
            try:
                payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            saved_keys = payload.get("saved_artifact_keys") or []
            wrote_back = bool(payload.get("document_write_result")) or (
                isinstance(saved_keys, list)
                and "document_write_result" in saved_keys
            )
            if not wrote_back:
                continue
            function = (
                "writer_publish_revision"
                if draft_operation == "revise"
                else "writer_write_document"
            )
            mode = "revision" if draft_operation == "revise" else "replace"
            write_back[function] += 1
            write_back_modes[mode] += 1
            write_back_evidence.append({
                "function": function,
                "mode": mode,
                "source": (
                    "writer_draft_workspace.output:"
                    f"operation={draft_operation}+document_write_result"
                ),
            })

    return TraceAnalysis(
        trace_id=str(trace.get("id") or ""),
        session_id=str(trace.get("sessionId") or ""),
        latency_s=float(trace.get("latency") or 0.0),
        route="unknown",
        step_path=[],
        tool_sequence=tools,
        slot_view={},       # 由 func_run 从 core 会话槽位补全
        provider="",
        write_back=dict(write_back),
        workspace_facts=workspace_facts,
        write_back_modes=dict(write_back_modes),
        write_back_evidence=write_back_evidence,
        modify_types=[],    # 由 func_run 从会话 document_modify_plan 补全
        models=sorted(models),
        providers=sorted(providers),
        llm_call_count=llm_calls,
        image_call_count=image_calls,
    )


# ---------------------------------------------------------------------------
# Assertion checks
# ---------------------------------------------------------------------------
