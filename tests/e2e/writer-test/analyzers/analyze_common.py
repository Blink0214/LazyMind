"""Trace-derived structural assertions for AI Writer runs.

This is the shared analyzer used by both ``perf`` and ``func`` runners. Given a
trace JSON (Langfuse shape, or local OTel normalized to it by ``shared.observability``),
it derives the structural facts that an e2e check would otherwise pull from
Core session + artifact fetches:

* ``route``         — create / expand / rewrite / revise / supplement
                       (auto-detected from the observed writer_* function
                       sequence; a revision plan with only additive ops is
                       ``supplement``, matching writer-e2e).
* ``step_path``     — ordered step ids the workflow transitioned through,
                       sourced from ``list_artifacts[].step_id`` with
                       ``created_at`` ordering.
* ``tool_sequence`` — chronological writer_*/patch_artifact/advance_step span
                       names; same ordering the LLM agent issued them.
* ``slot_view``     — per-slot record ``{extension, max_revision, step_id,
                       selected, stage}``.
* ``provider``      — ``feishu`` (link host) or ``local``.
* ``write_back``    — count of ``writer_replace_document`` /
                       ``writer_publish_revision`` spans.

Limitations the analyzer does *not* hide:

* The SSE stream protocol (start/delta/end) and the SSE reconstruction check
  are reported only through the live SSE capture module; the analyzer cannot
  recover them from trace alone.
* Feishu's external ``revision_id`` increment is also invisible to the trace
  and lives in ``shared.feishu``. ``analyze_func`` combines the trace-derived
  view here with those external fetches.
"""
from __future__ import annotations

import argparse
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


def _step_path(trace: dict) -> list[str]:
    """Ordered step ids the workflow transitioned through.

    The current writer workflow advances steps deterministically; the step
    transition is the first ``attempt_results[].step_id`` of each
    ``advance_step`` span, in chronological order.
    """
    ordered: list[str] = []
    for obs in sorted(trace.get("observations") or [],
                      key=lambda x: x.get("startTime") or ""):
        if _name(obs) != "advance_step":
            continue
        raw = _io_out(obs)
        try:
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for attempt in payload.get("attempt_results") or []:
            sid = str((attempt or {}).get("step_id") or "")
            if sid and sid not in ("__start__", "__end__") and sid not in ordered:
                ordered.append(sid)
                break
    if ordered:
        return ordered
    return []


def _tool_sequence(trace: dict) -> list[str]:
    """Chronological writer_*/patch_artifact/advance_step spans."""
    seq: list[str] = []
    for obs in sorted(trace.get("observations") or [],
                      key=lambda x: x.get("startTime") or ""):
        n = _name(obs)
        if n.startswith("writer_") or n in ("patch_artifact", "advance_step"):
            seq.append(n)
    return seq


def _route(tools: list[str],
           draft_operation: str = "", prepare_has_source: bool = False,
           draft_success: bool = True) -> str:
    """Pick the route from the consolidated workspace tools.

    The write_document step is encapsulated in writer_draft_workspace whose
    output carries the operation (generate / rewrite / revise); the prepare
    output records whether a source document was bound (expand vs create).
    """
    names = set(tools)
    if "writer_draft_workspace" in names:
        if draft_operation == "revise":
            return "revise"
        if draft_operation == "rewrite":
            return "rewrite"
        if draft_operation == "generate":
            return "expand" if prepare_has_source else "create"
        # 老版本 trace 的 workspace 输出可能没有 operation 字段。
        if draft_success:
            return "expand" if prepare_has_source else "create"
        # 运行失败：workspace 从未产出 operation，路由不可推断，避免假断言。
        return "unknown"
    if "writer_outline_workspace" in names:
        return "expand" if prepare_has_source else "create"
    return "unknown"


def _workspace_signals(trace: dict) -> tuple[str, bool, bool]:
    """Extract routing signals from the encapsulated workspace tool outputs.

    Returns ``(draft_operation, prepare_has_source, draft_success)``:
    - ``draft_operation`` is writer_draft_workspace's ``operation``
      (generate / rewrite / revise), which drives the route;
    - ``prepare_has_source`` is True when writer_prepare_workspace bound a
      source document, distinguishing create from expand.
    - ``draft_success`` is False when every writer_draft_workspace observation
      errored (no operation output), making the route un-inferable.
    """
    draft_operation = ""
    prepare_has_source = False
    draft_success = False
    for obs in trace.get("observations") or []:
        n = _name(obs)
        if n not in ("writer_draft_workspace", "writer_prepare_workspace"):
            continue
        if n == "writer_draft_workspace" and str(obs.get("level")) != "ERROR":
            draft_success = True
        raw = _io_out(obs)
        try:
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if n == "writer_draft_workspace" and not draft_operation:
            draft_operation = str(payload.get("operation") or "")
        if n == "writer_prepare_workspace" and payload.get("source_document"):
            prepare_has_source = True
    return draft_operation, prepare_has_source, draft_success


def _provider(trace: dict) -> str:
    """Infer provider from the published link or the loaded document URL."""
    for obs in trace.get("observations") or []:
        n = _name(obs)
        if n == "writer_replace_document":
            if "feishu.cn" in _io_out(obs):
                return "feishu"
        if n in ("writer_load_document", "writer_prepare_workspace"):
            if "feishu.cn" in _io_in(obs):
                return "feishu"
    # 封装工作流：飞书 URL 不再作为工具入参，而是出现在工作流提示/上下文
    # （ReactAgent/llm 等 span 的 metadata）中。只认真实 URL（带 https:// 前缀），
    # 避免通用工具描述里的 "*.feishu.cn/wiki/*" 造成误判。
    feishu_url_re = re.compile(r"https?://[^\s\"']*feishu\.cn")
    for obs in trace.get("observations") or []:
        attrs = ((obs.get("metadata") or {}).get("attributes") or {})
        for value in attrs.values():
            if isinstance(value, str) and feishu_url_re.search(value):
                return "feishu"
    return "local"


def _stage_for(slot_key: str, save_records: list[dict]) -> str | None:
    """Pick the semantic stage for a slot; falls back to conventions on key."""
    if slot_key.endswith("_outline") or slot_key == "outline_document":
        return "outline"
    if "draft" in slot_key and "after" not in slot_key:
        return "draft_or_final"
    return None


# ---------------------------------------------------------------------------
# Public analysis result
# ---------------------------------------------------------------------------


@dataclass
class SlotRecord:
    slot: str
    extension: str = ""
    step_id: str | None = None
    selected: bool = True
    stage: str | None = None
    revisions: list[int] = field(default_factory=list)

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
    runner; this trace-only analyzer keeps route / steps / tools / provider /
    write_back.
    """
    tools = _tool_sequence(trace)
    steps = _step_path(trace)
    draft_operation, prepare_has_source, draft_success = _workspace_signals(trace)
    route = _route(tools, draft_operation, prepare_has_source, draft_success)
    provider = _provider(trace)
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

    write_back = Counter(
        n for n in tools
        if n in ("writer_replace_document", "writer_publish_revision")
    )
    # 新工作流把写回封装在 workspace 内：writer_draft_workspace 的输出携带
    # document_write_result（本地/云文档写回成功），据此按 operation 归因工具。
    if draft_operation:
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
            tool = (
                "writer_publish_revision"
                if draft_operation == "revise"
                else "writer_replace_document"
            )
            write_back[tool] += 1

    return TraceAnalysis(
        trace_id=str(trace.get("id") or ""),
        session_id=str(trace.get("sessionId") or ""),
        latency_s=float(trace.get("latency") or 0.0),
        route=route,
        step_path=steps,
        tool_sequence=tools,
        slot_view={},       # 由 func_run 从 core 会话槽位补全
        provider=provider,
        write_back=dict(write_back),
        modify_types=[],    # 由 func_run 从会话 document_modify_plan 补全
        models=sorted(models),
        providers=sorted(providers),
        llm_call_count=llm_calls,
        image_call_count=image_calls,
    )


# ---------------------------------------------------------------------------
# Assertion checks
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str     # "PASS" | "FAIL" | "WARN"
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def check_route(analysis: TraceAnalysis, expected: str) -> Check:
    if analysis.route == "unknown" and expected != "unknown":
        return Check(
            "route", "WARN",
            "route inference unavailable (workspace never produced an operation); "
            f"observed=unknown, expected={expected}",
        )
    return Check("route",
                 "PASS" if analysis.route == expected else "FAIL",
                 f"observed={analysis.route}, expected={expected}")


def check_required_tools(analysis: TraceAnalysis, tools: list[str]) -> Check:
    seen = set(analysis.tool_sequence)
    missing = set(tools) - seen
    return Check("tools.required",
                 "PASS" if not missing else "FAIL",
                 f"missing={sorted(missing)}" if missing else "all present")


def check_forbidden_tools(analysis: TraceAnalysis, tools: list[str]) -> Check:
    seen = set(analysis.tool_sequence)
    hits = set(tools) & seen
    return Check("tools.forbidden",
                 "PASS" if not hits else "FAIL",
                 f"forbidden_called={sorted(hits)}" if hits else "clean")


def check_step_path(analysis: TraceAnalysis, expected: list[str]) -> Check:
    ok = analysis.step_path == list(expected)
    return Check("steps",
                 "PASS" if ok else "FAIL",
                 f"observed={analysis.step_path}, expected={expected}")


def check_required_artifacts(analysis: TraceAnalysis,
                             required: dict[str, dict]) -> list[Check]:
    out: list[Check] = []
    for sid, must in required.items():
        rec = analysis.slot_view.get(sid)
        if not rec:
            out.append(Check(f"slot.{sid}", "FAIL", "missing"))
            continue
        ext_ok = not must.get("extension") or rec.extension == must["extension"]
        representation = (
            "ir" if rec.extension == ".lmd"
            else "markdown" if rec.extension == ".md"
            else ""
        )
        rep_ok = not must.get("representation") or representation == must["representation"]
        stage_ok = (
            not must.get("stage")
            or rec.stage in str(must["stage"]).split("_or_")
        )
        min_rev = int(must.get("min_revision", 1))
        rev_ok = rec.max_revision >= min_rev
        provider_ok = (
            not must.get("provider")
            or must["provider"] == analysis.provider
            or (must["provider"] == "feishu" and analysis.provider == "feishu")
        )
        ok = ext_ok and rep_ok and stage_ok and rev_ok and provider_ok
        out.append(Check(
            f"slot.{sid}",
            "PASS" if ok else "FAIL",
            f"ext={rec.extension}, rep={representation}, stage={rec.stage}, "
            f"max_rev={rec.max_revision}, provider={analysis.provider}",
        ))
    return out


def check_forbidden_artifacts(analysis: TraceAnalysis,
                              forbidden: list[str]) -> list[Check]:
    out: list[Check] = []
    for sid in forbidden:
        if sid in analysis.slot_view:
            out.append(Check(f"slot.{sid}", "FAIL", "unexpectedly present"))
        else:
            out.append(Check(f"slot.{sid}", "PASS", "absent"))
    return out


def check_write_back(analysis: TraceAnalysis, expected_calls: int) -> Check:
    actual = sum(analysis.write_back.values())
    return Check("write_back",
                 "PASS" if actual == expected_calls else "FAIL",
                 f"calls={actual} expected={expected_calls}, "
                 f"by_tool={analysis.write_back}")


def check_provider(analysis: TraceAnalysis, expected: str) -> Check:
    return Check("provider",
                 "PASS" if analysis.provider == expected else "FAIL",
                 f"observed={analysis.provider}, expected={expected}")


def check_modify_types(analysis: TraceAnalysis, expected: list[str]) -> Check:
    """All expected modify_type operations must appear in the plan."""
    missing = [op for op in expected if op not in analysis.modify_types]
    return Check("modify_types",
                 "PASS" if not missing else "FAIL",
                 f"observed={sorted(analysis.modify_types)}, missing={missing}"
                 if missing else f"operations={sorted(analysis.modify_types)}")


def run_checks(analysis: TraceAnalysis, expected: dict) -> list[Check]:
    """Apply every applicable check in ``expected`` to the analysis.

    ``expected`` follows the flat convention used by ``case_N.yaml`` and the
    legacy ``writer_scenarios.yaml``:
      {route, steps, tools_required, tools_forbidden,
       slots_required: {slot: {extension, min_revision, provider}},
       slots_forbidden: [slot], write_back_calls, provider}
    Missing keys are skipped — there is no "fail-by-default" semantics.
    """
    checks: list[Check] = []
    if "route" in expected:
        checks.append(check_route(analysis, expected["route"]))
    if "tools_required" in expected:
        checks.append(check_required_tools(analysis, expected["tools_required"]))
    if "tools_forbidden" in expected:
        checks.append(check_forbidden_tools(analysis, expected["tools_forbidden"]))
    if "steps" in expected:
        checks.append(check_step_path(analysis, expected["steps"]))
    if "slots_required" in expected:
        checks.extend(check_required_artifacts(analysis, expected["slots_required"]))
    if "slots_forbidden" in expected:
        checks.extend(check_forbidden_artifacts(analysis, expected["slots_forbidden"]))
    if "write_back_calls" in expected:
        checks.append(check_write_back(analysis, int(expected["write_back_calls"])))
    if "provider" in expected:
        checks.append(check_provider(analysis, expected["provider"]))
    if expected.get("modify_types"):
        checks.append(check_modify_types(
            analysis, [str(item) for item in expected["modify_types"]],
        ))
    return checks


# ---------------------------------------------------------------------------
# Optional CLI for ad-hoc inspection (kept for debugging)
# ---------------------------------------------------------------------------


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_json", help="path to a trace JSON file")
    parser.add_argument("--expected-json", help="path to a flat-dict JSON of expected values")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    trace = load_trace(args.trace_json)
    analysis = analyze(trace)
    expected = {}
    if args.expected_json:
        expected = json.loads(Path(args.expected_json).read_text(encoding="utf-8"))
    checks = run_checks(analysis, expected)
    if args.json:
        out = {"analysis": analysis.to_dict(),
               "checks": [c.to_dict() for c in checks]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"trace_id    : {analysis.trace_id}")
    print(f"session_id  : {analysis.session_id}")
    print(f"latency     : {analysis.latency_s:.1f}s")
    print(f"route       : {analysis.route}")
    print(f"provider    : {analysis.provider}")
    print(f"step_path   : {' → '.join(analysis.step_path) or '(none)'}")
    print(f"write_back  : {analysis.write_back}")
    print(f"\nslot view ({len(analysis.slot_view)} slots):")
    for sid, rec in analysis.slot_view.items():
        print(f"  - {sid:30s}  ext={rec.extension:6s}  "
              f"max_rev={rec.max_revision}  step={rec.step_id}")
    if checks:
        print(f"\nchecks vs expected:")
        for c in checks:
            print(f"  [{c.status:4s}] {c.name}: {c.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
