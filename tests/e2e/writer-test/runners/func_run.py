"""Functional-mode runner: execute + collect evidence + mechanical checks.

Default mode is ``--ui`` which invokes the Node-side Playwright bridge
(``frontend/tests/e2e/writer_bridge.mjs``) to drive the chat UI; ``--no-ui``
runs the chat through ``shared.api`` and captures the live SSE stream via
``shared.observability``.

The runner executes one YAML-registered functional scenario, collects the
evidence package and runs the deterministic checks:

  case_<N>/
    run_N.json         runner envelope (status, feishu before/after, ...)
    final.md           markdown artifact pulled from the session
    trace.json         normalized Langfuse/local trace (when fetch succeeds)
    evidence.json      expected + facts + checks + retries + SSE
    draft_document.json / export_document.md
                       representation-specific evidence when needed
    report.md          mechanical table + LLM anomaly/final-verdict sections
    _ui_bridge/        Playwright artifacts (UI mode)

The exit code reflects both execution completion and the deterministic
``mechanical_verdict``.  The business-facing verdict written to the test table
remains a separate Agent/human judgment based on the persisted evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tests/e2e"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))


from shared.api import (
    ChatResult,
    load_slot,
    wait_for_writer_completion,
)
from shared.case_loader import Case, load_writer_e2e_scenario
from shared.execution import (
    collect_trace_evidence,
    correlate_workflow_session as _correlate_workflow_session,
    prepare_execution,
    resolve_workflow_session,
    send_case,
)
from shared.feishu import fetch_feishu_document, fetch_revision
from shared.observability import (
    TaskCapture,
    _consume_event,
    collect_workflow_task_streams,
    concatenated_text,
    select_streams,
    task_capture_to_dict,
)
from analyze_common import Check  # noqa: E402


DEFAULT_BASE_URL = os.environ.get("LAZYMIND_BASE_URL", "http://localhost:8090")
DEFAULT_TIMEOUT = 600
SESSION_GRACE_S = 20
PLAYWRIGHT_BRIDGE = REPO_ROOT / "frontend/tests/e2e/writer_bridge.mjs"


def _post_writer_json(base_url: str, token: str, path: str,
                      payload: dict, *, timeout: int = 60) -> tuple[bytes, str]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), str(response.headers.get("Content-Type") or "")


def _sanitized_media_evidence(payload: Any) -> dict:
    """Keep asset identity/type facts without persisting paths or signed URLs."""
    if not isinstance(payload, dict):
        return {"assets": {}}
    library = payload.get("data", payload)
    assets = (library or {}).get("assets") or {}
    safe: dict[str, dict[str, Any]] = {}
    if isinstance(assets, dict):
        for asset_id, asset in assets.items():
            item = asset if isinstance(asset, dict) else {}
            safe[str(asset_id)] = {
                "source_type": item.get("source_type"),
                "asset_type": item.get("asset_type"),
                "has_location": bool(item.get("local_path") or item.get("uri")),
            }
    bindings = (library or {}).get("visual_need_asset_ids") or {}
    safe_bindings = {
        str(need): [str(asset_id) for asset_id in ids]
        for need, ids in bindings.items()
        if isinstance(ids, list)
    } if isinstance(bindings, dict) else {}
    return {
        "assets": safe,
        "visual_need_asset_ids": safe_bindings,
        "credential_data_included": False,
    }


def _export_writer_document(base_url: str, token: str, session_id: str,
                            *, timeout: int = 60) -> tuple[str, dict]:
    """Return the server-produced, number-materialized Markdown export.

    The editable artifact stays canonical.  Tests must inspect the same
    render/export boundary used by the UI instead of expecting numbering
    prefixes to be persisted in canonical Markdown or IR content.
    """
    raw, _ = _post_writer_json(
        base_url, token,
        f"/api/core/workflow-sessions/{session_id}/writer-document:render",
        {"slot": "draft_document"}, timeout=timeout,
    )
    envelope = json.loads(raw.decode("utf-8"))
    rendered = envelope.get("data") or {}
    representation = str(rendered.get("representation") or "")
    document = rendered.get("document")
    if representation == "markdown":
        exported = rendered.get("export_document")
        if not isinstance(exported, str):
            raise ValueError("Writer render response has no Markdown export_document")
        return exported, rendered
    if representation != "ir" or not isinstance(document, dict):
        raise ValueError("Writer render response has an unsupported document")
    converted, _ = _post_writer_json(
        base_url, token,
        "/api/core/writer-download-conversions:convert",
        {
            "source_format": "writer_document",
            "target_format": "markdown",
            "content": json.dumps(document, ensure_ascii=False),
            "document_id": str(document.get("document_id") or "writer-document"),
        }, timeout=timeout,
    )
    return converted.decode("utf-8"), rendered


def _analyzers():
    """Lazily import the analyzers so the module loads without trace deps."""
    from analyze_common import analyze, run_checks
    from analyze_func import CheckContext, run_func_checks
    return analyze, run_checks, CheckContext, run_func_checks


@dataclass
class FuncResult:
    conversation_id: str
    session_id: str
    task_id: str
    started_at: float
    finished_at: float
    elapsed_s: float
    finish_reason: str
    writer_status: str
    mechanical_verdict: str
    approvals: int
    final_artifact_path: str | None
    final_artifact_text: str | None
    trace_id: str | None
    trace_path: str | None
    sse_capture: dict | None
    checks: list[dict] = field(default_factory=list)
    mode: str = "ui"
    report_path: str | None = None
    evidence_path: str | None = None
    feishu_before: int | None = None
    feishu_after: int | None = None
    retry_events: list = field(default_factory=list)
    retry_count: int = 0
    panel_recovery_count: int = 0
    internal_error_count: int = 0
    models: list = field(default_factory=list)
    providers: list = field(default_factory=list)
    llm_call_count: int = 0
    image_call_count: int = 0


@dataclass
class ExecutionEvidence:
    """Collected inputs for checks and persistence; contains no credentials."""

    case: Case
    output_dir: Path
    base_url: str
    timeout: int
    use_ui: bool
    compute_func_checks: bool
    started_at: float
    finished_at: float
    state: dict
    chat_info: dict
    session_payload: dict
    final_artifact: Any
    final_artifact_text: str | None
    final_revision: int
    final_md_path: Path
    source_artifact: Any
    materialized_artifact: str | None
    materialized_checks_required: bool
    use_provider_materialization: bool
    render_evidence: dict | None
    materialization_error: str | None
    sse_capture: dict | None
    sse_streams: list
    sse_text: str
    trace_json: dict | None
    trace_id: str | None
    trace_path: Path
    trace_error: str | None
    trace_error_spans: list[dict]
    media_min_images: int
    media_assets: Any
    feishu_before: int | None
    feishu_after: int | None
    feishu_materialization_summary: dict | None
    provider_checks: list


@dataclass
class CheckOutcome:
    checks: list[dict]
    facts: dict | None = None
    models: list = field(default_factory=list)
    providers: list = field(default_factory=list)
    llm_call_count: int = 0
    image_call_count: int = 0


def run_case(case: Case,
             output_dir: Path,
             *,
             base_url: str = DEFAULT_BASE_URL,
             timeout: int = DEFAULT_TIMEOUT,
             recipe: str = "text_intent",
             use_ui: bool = True,
             fetch_trace_after: bool = True,
             compute_func_checks: bool = True,
             feishu_revision_before: int | None = None) -> FuncResult:
    """End-to-end functional run for one case."""
    output_dir.mkdir(parents=True, exist_ok=True)
    state: dict = {}
    prepared = prepare_execution(
        case,
        base_url=base_url,
        timeout=timeout,
        provider_revision_before=feishu_revision_before,
    )
    started_at = prepared.started_at
    session_obj = prepared.session
    state["attachments"] = prepared.attachments
    feishu_before = prepared.provider_revision_before

    if use_ui:
        bridge_dir = output_dir / "_ui_bridge"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = bridge_dir / "prompt.md"
        prompt_path.write_text(case.prompt_text + "\n", encoding="utf-8")
        try:
            chat_info = _run_ui_bridge(
                case, base_url, session_obj.token, session_obj.user_id,
                timeout, output_dir,
                prompt_file=str(prompt_path),
            )
        finally:
            prompt_path.unlink(missing_ok=True)
    else:
        chat_info = _run_api(
            case, base_url, session_obj.token, session_obj.user_id,
            state["attachments"], timeout, recipe,
        )

    state["conversation_id"] = chat_info["conversation_id"]
    if not state["conversation_id"] and use_ui:
        raise RuntimeError(
            "UI test did not return the conversation created by this case; "
            "refusing to associate results with a recent existing conversation"
        )
    state["session_id"] = chat_info["session_id"]
    state["task_id"] = chat_info.get("task_id", "")
    state["writer_status"] = chat_info.get("writer_status", "")

    session_payload = resolve_workflow_session(
        base_url=base_url,
        token=session_obj.token,
        conversation_id=state["conversation_id"],
        observed_session_id=state["session_id"],
        initial_session=chat_info.get("session") or None,
    )

    final_artifact: Any = None
    final_revision = 0
    if session_payload:
        try:
            final_artifact = load_slot(
                base_url, session_obj.token, session_payload, "draft_document",
            )[0]
        except Exception:
            final_artifact = None
        for slot in session_payload.get("slots") or []:
            if (str(slot.get("slot_id") or "") == "draft_document"
                    and slot.get("selected", True)):
                final_revision = max(final_revision, int(slot.get("revision") or 0))
    final_artifact_text = (
        final_artifact if isinstance(final_artifact, str)
        else chat_info.get("final_text")
    )
    source_artifact: Any = None
    if session_payload:
        try:
            source_artifact = load_slot(
                base_url, session_obj.token, session_payload, "source_document",
            )[0]
        except Exception:
            source_artifact = _cross_ref_source(case)
    if source_artifact is None:
        source_artifact = _cross_ref_source(case)
    final_md_path = output_dir / "final.md"
    if final_artifact_text:
        final_md_path.write_text(final_artifact_text, encoding="utf-8")

    materialized_checks_required = any((
        (case.assertions or {}).get("cross_ref"),
        (case.assertions or {}).get("ref_integrity"),
        (case.assertions or {}).get("numbering"),
        (case.assertions or {}).get("ref_number_consistency"),
    ))
    materialized_artifact: str | None = None
    render_evidence: dict | None = None
    materialization_error: str | None = None
    use_provider_materialization = bool(
        (case.assertions or {}).get("provider_materialization"))
    if (materialized_checks_required and session_payload
            and state.get("session_id") and final_artifact is not None
            and not use_provider_materialization):
        try:
            materialized_artifact, rendered = _export_writer_document(
                base_url, session_obj.token, state["session_id"],
                timeout=min(timeout, 120),
            )
            # Persist only non-secret evidence.  media_urls may contain signed
            # access URLs and are deliberately excluded.
            render_evidence = {
                "representation": rendered.get("representation"),
                "numbering": rendered.get("numbering"),
            }
        except Exception as exc:
            materialization_error = _safe_error_message(exc)[:300]

    sse_capture_dict: dict | None = None
    sse_streams = []
    sse_text = ""
    if chat_info.get("sse_capture_dict"):
        sse_capture_dict = chat_info["sse_capture_dict"]
        sse_streams = chat_info.get("sse_streams_for_draft", [])
        sse_text = chat_info.get("sse_text", "")

    finished = time.time()
    trace_path = output_dir / "trace.json"
    trace_id = None
    trace_error = None
    trace_json = None
    if fetch_trace_after and state.get("conversation_id"):
        trace_evidence = collect_trace_evidence(
            conversation_id=state["conversation_id"],
            workflow_session_id=state.get("session_id") or None,
            started_at=started_at,
            finished_at=finished,
            output_path=trace_path,
            error_formatter=_safe_error_message,
        )
        trace_json = trace_evidence.trace
        trace_id = trace_evidence.trace_id
        trace_error = trace_evidence.error

    trace_error_spans: list[dict] = []
    if trace_json is not None:
        for obs in trace_json.get("observations") or []:
            if str(obs.get("level")) != "ERROR":
                continue
            attrs = ((obs.get("metadata") or {}).get("attributes") or {})
            raw_message = (
                attrs.get("lazyllm.error.message")
                or attrs.get("langfuse.observation.status_message")
                or attrs.get("lazyllm.io.output") or ""
            )
            trace_error_spans.append({
                "name": str(obs.get("name") or ""),
                "start": str(obs.get("startTime") or ""),
                "message": _safe_error_message(RuntimeError(str(raw_message)))[:600],
            })

    media_rule = (case.assertions or {}).get("media") or {}
    media_min_images = int(media_rule.get("min_images") or 0)
    media_assets = None
    if media_min_images > 0 and session_payload:
        try:
            media_assets = load_slot(
                base_url, session_obj.token, session_payload, "resolved_media_assets",
            )[0]
        except Exception:
            media_assets = None

    (feishu_after, feishu_materialization_summary,
     provider_checks, provider_error) = (
        _collect_provider_evidence(
            case, final_artifact, media_min_images, timeout,
        )
    )
    if provider_error:
        provider_error = _safe_error_message(RuntimeError(provider_error))
        trace_error = (
            f"{trace_error}; feishu fetch: {provider_error}"
            if trace_error else f"feishu fetch: {provider_error}"
        )

    evidence = ExecutionEvidence(
        case=case, output_dir=output_dir, base_url=base_url, timeout=timeout,
        use_ui=use_ui, compute_func_checks=compute_func_checks,
        started_at=started_at, finished_at=finished, state=state,
        chat_info=chat_info, session_payload=session_payload,
        final_artifact=final_artifact, final_artifact_text=final_artifact_text,
        final_revision=final_revision, final_md_path=final_md_path,
        source_artifact=source_artifact,
        materialized_artifact=materialized_artifact,
        materialized_checks_required=materialized_checks_required,
        use_provider_materialization=use_provider_materialization,
        render_evidence=render_evidence,
        materialization_error=materialization_error,
        sse_capture=sse_capture_dict, sse_streams=sse_streams,
        sse_text=sse_text, trace_json=trace_json, trace_id=trace_id,
        trace_path=trace_path, trace_error=trace_error,
        trace_error_spans=trace_error_spans,
        media_min_images=media_min_images, media_assets=media_assets,
        feishu_before=feishu_before, feishu_after=feishu_after,
        feishu_materialization_summary=feishu_materialization_summary,
        provider_checks=provider_checks,
    )
    outcome = _evaluate_execution(evidence, session_obj.token)
    return _persist_execution(evidence, outcome)


def _evaluate_execution(evidence: ExecutionEvidence, token: str) -> CheckOutcome:
    """Evaluate collected evidence without performing additional I/O."""
    case = evidence.case
    expected = case.assertions or {}
    checks: list = []
    outcome = CheckOutcome(checks=[])
    if evidence.compute_func_checks:
        analyze, run_common_checks, CheckContext, run_func_checks = _analyzers()
        analysis = analyze(evidence.trace_json or {"id": "", "observations": []})
        if evidence.trace_json is None and case.has_feishu_reference:
            analysis.provider = "feishu"
        _merge_session_slots(
            analysis, evidence.session_payload, evidence.base_url, token,
            required_rules=expected.get("slots_required") or {},
        )
        _merge_session_steps(analysis, evidence.session_payload)
        _merge_session_route(
            analysis, evidence.session_payload, evidence.base_url, token,
        )
        _merge_session_modify_types(
            analysis, evidence.session_payload, evidence.base_url, token,
        )
        outcome.facts = analysis.to_dict()
        outcome.models = list(analysis.models or [])
        outcome.providers = list(analysis.providers or [])
        outcome.llm_call_count = int(analysis.llm_call_count or 0)
        outcome.image_call_count = int(analysis.image_call_count or 0)
        if evidence.trace_json is not None:
            common_checks = run_common_checks(analysis, expected=expected)
        else:
            session_expected = {
                key: value for key, value in expected.items()
                if key in {
                    "route", "steps", "slots_required", "slots_forbidden",
                    "modify_types", "provider",
                }
            }
            session_expected["slots_required"] = {
                slot: {key: value for key, value in rule.items()
                       if key != "provider"}
                for slot, rule in (session_expected.get("slots_required") or {}).items()
            }
            common_checks = run_common_checks(analysis, expected=session_expected)
            common_checks.insert(0, Check(
                "evidence.trace", "WARN",
                evidence.trace_error
                or "trace unavailable; tool/workspace checks skipped",
            ))
        check_expected = dict(expected)
        if evidence.use_provider_materialization:
            for key in (
                    "cross_ref", "ref_integrity", "numbering",
                    "ref_number_consistency"):
                check_expected.pop(key, None)
        checks = run_func_checks(CheckContext(
            common_checks=common_checks,
            final_artifact=evidence.final_artifact,
            expected=check_expected,
            streams_for_draft=(
                evidence.sse_streams if evidence.sse_capture else None),
            sse_text=evidence.sse_text,
            materialized_artifact=evidence.materialized_artifact,
            require_materialized_artifact=evidence.materialized_checks_required,
            source_artifact=evidence.source_artifact,
            cross_ref_source=_cross_ref_source(case),
            provider=analysis.provider,
            feishu_before=evidence.feishu_before,
            feishu_after=evidence.feishu_after,
            write_back_calls=sum(analysis.write_back.values()),
            write_back_tool=dict(analysis.write_back),
            write_back_modes=dict(analysis.write_back_modes),
            media_assets=evidence.media_assets,
            final_revision=evidence.final_revision,
            trace_available=evidence.trace_json is not None,
        ))
        checks.extend(evidence.provider_checks)
        if evidence.trace_json is None and expected.get("write_back"):
            checks.append(Check(
                "write_back", "WARN",
                "trace unavailable; call/tool/mode checks skipped",
            ))
        if (evidence.materialized_checks_required
                and not evidence.use_provider_materialization):
            checks.append(Check(
                "final.materialized_export",
                "PASS" if evidence.materialized_artifact is not None else "FAIL",
                (
                    "server render/export article captured"
                    if evidence.materialized_artifact is not None
                    else "server render/export failed: "
                         f"{evidence.materialization_error or 'unknown error'}"
                ),
            ))

    _append_ui_checks(checks, evidence)
    outcome.checks = [
        check if isinstance(check, dict) else asdict(check) for check in checks
    ]
    return outcome


def _append_ui_checks(checks: list, evidence: ExecutionEvidence) -> None:
    """Add browser-only rendering checks to an existing check list."""
    if evidence.media_min_images > 0:
        if evidence.use_ui:
            rendered = int(evidence.chat_info.get("rendered_images") or 0)
            failed_images = list(evidence.chat_info.get("failed_images") or [])
            placeholders = list(evidence.chat_info.get("placeholder_images") or [])
            failed = len(failed_images)
            ok = (
                rendered >= evidence.media_min_images
                and failed == 0 and not placeholders
            )
            detail = (
                f"rendered={rendered}, failed={failed}, "
                f"placeholders={len(placeholders)}, "
                f"expected >= {evidence.media_min_images}"
            )
            if failed_images:
                detail += f", failed_images={failed_images[:3]}"
            checks.append(Check("media.display", "PASS" if ok else "FAIL", detail))
        else:
            checks.append(Check(
                "media.display", "WARN",
                "no-ui 模式无法验证浏览器渲染；需要 --ui",
            ))
    if evidence.use_ui:
        visible = bool(evidence.chat_info.get("final_panel_visible"))
        chars = len(str(evidence.chat_info.get("final_text") or "").strip())
        ui_diag = evidence.chat_info.get("final_ui_diagnostics") or {}
        diagnostic = (
            f"; tabs={ui_diag.get('tab_count', 0)}, "
            f"controls={ui_diag.get('tab_controls', [])}, "
            f"selected={ui_diag.get('selected_control', '')!r}, "
            f"selector={ui_diag.get('document_selector', '')!r}"
        )
        if ui_diag.get("click_error"):
            diagnostic += f", click_error={ui_diag['click_error']!r}"
        checks.append(Check(
            "final.ui_visible", "PASS" if visible else "FAIL",
            (
                "final draft tab selected; document visible and non-empty "
                if visible else "final draft document missing, hidden, or empty "
            ) + f"(chars={chars}){diagnostic}",
        ))


def _retry_evidence(evidence: ExecutionEvidence) -> tuple[dict, int, int]:
    """Build the retry evidence envelope from session, trace and UI facts."""
    steps: list[dict] = []
    session_steps = evidence.session_payload.get("steps") or []
    if session_steps:
        for step in session_steps:
            if not isinstance(step, dict):
                continue
            item = {
                key: step.get(key) for key in ("step_id", "status", "attempt")
                if key in step
            }
            if step.get("error"):
                item["error"] = _safe_error_message(
                    RuntimeError(str(step["error"])),
                )[:300]
            steps.append(item)
    elif evidence.trace_json is not None:
        workspace_steps = {
            "writer_prepare_workspace": "prepare",
            "writer_outline_workspace": "outline",
            "writer_draft_workspace": "write_document",
        }
        seen: dict[str, dict] = {}
        for observation in evidence.trace_json.get("observations") or []:
            step_id = workspace_steps.get(str(observation.get("name") or ""))
            if not step_id:
                continue
            item = seen.setdefault(
                step_id,
                {"step_id": step_id, "status": "completed", "attempt": 0},
            )
            item["attempt"] += 1
            if str(observation.get("level")) == "ERROR":
                item["status"] = "failed"
        steps = list(seen.values())

    retry_events = list(evidence.chat_info.get("retry_events") or [])
    step_retries = max(
        sum(max(0, int(item.get("attempt") or 0) - 1) for item in steps),
        sum(1 for item in retry_events if item.get("type") == "step_retried"),
    )
    panel_recoveries = sum(
        1 for item in retry_events if item.get("type") == "panel_recovered"
    )
    return ({
        "workflow_step_retry_count": step_retries,
        "panel_recovery_count": panel_recoveries,
        "internal_error_count": len(evidence.trace_error_spans),
        "retry_events": retry_events,
        "trace_error_spans": evidence.trace_error_spans,
        "session_steps": steps,
    }, step_retries, panel_recoveries)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _run_result_payload(result: FuncResult) -> dict:
    """Return the compact on-disk envelope without duplicated evidence."""
    payload = asdict(result)
    for key in (
        "final_artifact_text", "sse_capture", "checks", "retry_events",
    ):
        payload.pop(key, None)
    return payload


def _persist_execution(evidence: ExecutionEvidence,
                       outcome: CheckOutcome) -> FuncResult:
    """Persist the sanitized evidence package and return its run envelope."""
    if evidence.final_artifact is not None and not isinstance(
            evidence.final_artifact, str):
        _write_json(
            evidence.output_dir / "draft_document.json",
            evidence.final_artifact,
        )
    if evidence.materialized_artifact is not None:
        (evidence.output_dir / "export_document.md").write_text(
            evidence.materialized_artifact, encoding="utf-8",
        )

    retries, retry_count, panel_recovery_count = _retry_evidence(evidence)
    evidence_payload = {
        "schema_version": 1,
        "expected": evidence.case.assertions or {},
        "facts": outcome.facts or {"error": evidence.trace_error or "no trace"},
        "checks": outcome.checks,
        "retries": retries,
        "sse": evidence.sse_capture,
    }
    optional_evidence = {
        "render_numbering": evidence.render_evidence,
        "feishu_provider_materialization": (
            evidence.feishu_materialization_summary),
        "resolved_media_assets": (
            _sanitized_media_evidence(evidence.media_assets)
            if evidence.media_assets is not None else None),
        "render_error": (
            {"error": evidence.materialization_error}
            if evidence.materialization_error else None),
    }
    evidence_payload.update({
        key: value for key, value in optional_evidence.items()
        if value is not None
    })
    evidence_path = evidence.output_dir / "evidence.json"
    _write_json(evidence_path, evidence_payload)
    report_path = evidence.output_dir / "report.md"
    retry_events = list(evidence.chat_info.get("retry_events") or [])
    mechanical_verdict = _mechanical_verdict(
        evidence.state.get("writer_status") or "",
        outcome.checks,
        retry_count=retry_count,
        panel_recovery_count=panel_recovery_count,
    )
    result = FuncResult(
        conversation_id=evidence.state.get("conversation_id") or "",
        session_id=evidence.state.get("session_id") or "",
        task_id=evidence.state.get("task_id") or "",
        started_at=evidence.started_at,
        finished_at=evidence.finished_at,
        elapsed_s=round(evidence.finished_at - evidence.started_at, 3),
        finish_reason=evidence.chat_info.get("finish_reason", ""),
        writer_status=evidence.state.get("writer_status") or "",
        mechanical_verdict=mechanical_verdict,
        approvals=evidence.chat_info.get("approvals", 0),
        retry_events=retry_events,
        retry_count=retry_count,
        panel_recovery_count=panel_recovery_count,
        internal_error_count=len(evidence.trace_error_spans),
        models=outcome.models,
        providers=outcome.providers,
        llm_call_count=outcome.llm_call_count,
        image_call_count=outcome.image_call_count,
        final_artifact_path=(
            str(evidence.final_md_path) if evidence.final_artifact_text else None),
        final_artifact_text=evidence.final_artifact_text,
        trace_id=evidence.trace_id,
        trace_path=(
            str(evidence.trace_path) if evidence.trace_json is not None else None),
        sse_capture=evidence.sse_capture,
        checks=outcome.checks,
        mode="ui" if evidence.use_ui else "no-ui",
        report_path=str(report_path),
        evidence_path=str(evidence_path),
        feishu_before=evidence.feishu_before,
        feishu_after=evidence.feishu_after,
    )
    _write_json(evidence.output_dir / "run_N.json", _run_result_payload(result))
    _write_report(report_path, result)
    return result


def _mechanical_verdict(writer_status: str, checks: list[dict], *,
                        retry_count: int = 0,
                        panel_recovery_count: int = 0) -> str:
    """Return the deterministic harness verdict, separate from table judgment."""
    if writer_status != "completed":
        return "FAIL"
    statuses = {str(check.get("status") or "") for check in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if not checks or "WARN" in statuses:
        return "INCONCLUSIVE"
    if retry_count > 0 or panel_recovery_count > 0:
        return "PASS_WITH_RETRY"
    return "PASS"


def _collect_provider_evidence(case: Case, final_artifact: Any,
                               media_min_images: int, timeout: int):
    """Fetch Feishu terminal evidence and build provider checks once."""
    expected = case.assertions or {}
    revision_rule = ((expected.get("write_back") or {}).get("provider_revision"))
    if not revision_rule or "feishu" not in case.prompt_text:
        return None, None, [], None
    feishu_url = _extract_feishu_url(case.prompt_text)
    if not feishu_url:
        return None, None, [], "Feishu URL missing from resolved prompt"

    provider_materialization = bool(expected.get("provider_materialization"))
    try:
        if provider_materialization:
            document = fetch_feishu_document(
                feishu_url, timeout=min(timeout, 180),
                doc_format="xml", detail="with-ids",
            )
            revision = document.revision_id
        else:
            document = None
            revision = fetch_revision(
                feishu_url, timeout=min(timeout, 180),
            ).revision_id
    except Exception as exc:
        checks = []
        if provider_materialization:
            unavailable = "Feishu terminal document unavailable"
            checks = [
                Check("final.reference_integrity", "WARN", unavailable),
                Check("final.numbering", "WARN", unavailable),
                Check("final.provider_content", "WARN", unavailable),
                Check("final.provider_structure", "WARN", unavailable),
                Check("final.provider_materialization", "WARN", unavailable),
            ]
            if expected.get("cross_ref"):
                checks.append(Check("final.cross_references", "WARN", unavailable))
            if media_min_images > 0:
                checks.append(Check("final.provider_media", "WARN", unavailable))
        return None, None, checks, str(exc)[:300]

    if not provider_materialization:
        return revision, None, [], None

    from analyze_func import (
        check_feishu_content_consistency,
        check_feishu_materialization,
        check_ir_span_consistency,
    )
    integrity, numbering, summary = check_feishu_materialization(
        document.content,
        require_numbering=bool((expected.get("numbering") or {}).get("require")),
        check_images=(
            media_min_images > 0
            or bool((expected.get("cross_ref") or {}).get("min_figure_links"))
        ),
    )
    summary = {
        "provider": "feishu",
        "revision_id": revision,
        "credential_data_included": False,
        **summary,
    }
    content_check, content_summary = check_feishu_content_consistency(
        document.content, final_artifact,
    )
    summary["content_consistency"] = content_summary
    if content_check.status == "FAIL":
        diagnostic = check_ir_span_consistency(final_artifact)
        summary["diagnostics"] = {
            "ir_span_consistency": {
                "result": diagnostic.status,
                "detail": diagnostic.detail,
                "affects_functional_verdict": False,
            }
        }

    checks = [
        integrity,
        numbering,
        content_check,
        Check("final.provider_structure", integrity.status, integrity.detail),
    ]
    cross_ref_rule = expected.get("cross_ref") or {}
    if cross_ref_rule:
        links = summary["visible_internal_links"]
        link_count = int(links["count"])
        by_kind = links.get("by_kind") or {}
        min_links = int(cross_ref_rule.get("min_links") or 0)
        section_ok = int(by_kind.get("section") or 0) >= int(
            cross_ref_rule.get("min_section_links") or 0)
        figure_ok = int(by_kind.get("figure") or 0) >= int(
            cross_ref_rule.get("min_figure_links") or 0)
        links_ok = link_count >= min_links and section_ok and figure_ok
        checks.append(Check(
            "final.cross_references", "PASS" if links_ok else "FAIL",
            f"Feishu visible internal links={link_count}, by_kind={by_kind}, "
            f"expected total >= {min_links}, sections >= "
            f"{cross_ref_rule.get('min_section_links', 0)}, figures >= "
            f"{cross_ref_rule.get('min_figure_links', 0)}",
        ))
    if media_min_images > 0:
        provider_images = len(summary.get("image_caption_materialization") or [])
        checks.append(Check(
            "final.provider_media",
            "PASS" if provider_images >= media_min_images else "FAIL",
            f"Feishu images with native blocks={provider_images}, "
            f"expected >= {media_min_images}",
        ))
    checks.append(Check(
        "final.provider_materialization", "PASS",
        f"Feishu terminal revision={revision} native blocks captured",
    ))
    return revision, summary, checks, None


# ---------------------------------------------------------------------------
# Execution modes
# ---------------------------------------------------------------------------


def _run_api(case: Case, base_url: str, token: str, user_id: str,
             attachments: list, timeout: int, recipe: str) -> dict:
    """Pure API: send chat, then wait and consume task SSE concurrently."""
    chat = send_case(
        case,
        base_url=base_url,
        token=token,
        attachments=attachments,
        recipe=recipe,
    )
    if not chat.conversation_id:
        raise RuntimeError("chat returned no conversation_id")
    captured = _wait_with_sse(
        base_url, token, user_id, chat.conversation_id, timeout,
    )
    final_text = _fetch_final_text(base_url, token, captured["session"])
    sse_capture = captured["sse_capture_dict"]
    sse_for_draft = captured["sse_streams_for_draft"]
    sse_text = captured["sse_text"]
    return {
        "conversation_id": chat.conversation_id,
        "session_id": (captured["session"] or {}).get("session_id", ""),
        "session": captured["session"] or {},
        "task_id": captured["task_id"],
        "writer_status": (captured["session"] or {}).get("status", ""),
        "finish_reason": chat.finish_reason,
        "approvals": captured["approvals"],
        "final_text": final_text,
        "sse_capture_dict": sse_capture,
        "sse_streams_for_draft": sse_for_draft,
        "sse_text": sse_text,
    }


def _wait_with_sse(base_url: str, token: str, user_id: str,
                   conversation_id: str, timeout: int) -> dict:
    """Poll status every 10s and attach SSE while the task is still running.

    The first observed session starts its durable workflow event stream, whose
    snapshot and attempt.patch events expose every step task id. The main
    thread independently keeps the required 10-second status polling cadence.
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="writer-workflow-sse")
    stop_event = threading.Event()
    capture_future = None

    def start_capture(session: dict) -> None:
        nonlocal capture_future
        session_id = str(session.get("session_id") or "")
        if capture_future is None and session_id:
            capture_future = executor.submit(
                collect_workflow_task_streams,
                base_url, token, user_id, session_id,
                timeout_s=timeout, stop_event=stop_event,
            )

    workflow_capture = None
    try:
        outcome = wait_for_writer_completion(
            base_url, token, conversation_id,
            timeout_s=timeout, session_start_grace_s=SESSION_GRACE_S,
            poll_s=10, on_session=start_capture,
        )
        session = outcome.session or {}
    finally:
        stop_event.set()
        if capture_future is not None:
            try:
                workflow_capture = capture_future.result(timeout=35)
            except FutureTimeoutError:
                workflow_capture = None
        executor.shutdown(wait=False, cancel_futures=True)
    capture = workflow_capture.capture if workflow_capture else None
    task_ids = workflow_capture.task_ids if workflow_capture else []
    sse_capture_dict = task_capture_to_dict(
        capture, task_ids=task_ids,
        capture_started=bool(workflow_capture and workflow_capture.stream_connected),
    )
    sse_streams_for_draft = (
        select_streams(capture, slot="draft_document", content_type="text/markdown")
        if capture else []
    )
    sse_text = concatenated_text(sse_streams_for_draft)
    return {
        "session": session,
        "approvals": 0,
        "task_id": task_ids[-1] if task_ids else "",
        "sse_capture_dict": sse_capture_dict,
        "sse_streams_for_draft": sse_streams_for_draft,
        "sse_text": sse_text,
    }


def _fetch_final_text(base_url, token, session) -> str | None:
    if not session:
        return None
    try:
        from shared.api import save_final_markdown
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile("w", suffix=".md", delete=False) as tmp:
            tmp_path = tmp.name
        save_final_markdown(base_url, token, session, tmp_path)
        text = Path(tmp_path).read_text(encoding="utf-8")
        Path(tmp_path).unlink(missing_ok=True)
        return text
    except Exception:
        return None


def _run_ui_bridge(case: Case, base_url: str, token: str, user_id: str,
                   timeout: int, output_dir: Path,
                   prompt_file: str) -> dict:
    """Drive the chat UI through the Playwright node bridge."""
    if not PLAYWRIGHT_BRIDGE.is_file():
        raise RuntimeError(
            f"playwright bridge not found at {PLAYWRIGHT_BRIDGE}; "
            "either build it or pass --no-ui"
        )
    cmd = [
        "node", str(PLAYWRIGHT_BRIDGE),
        "--base-url", base_url,
        "--token", token,
        "--user-id", user_id,
        "--scenario", case.scenario,
        "--case", str(case.case_num),
        "--prompt-file", prompt_file,
    ]
    if case.has_attachment:
        cmd += ["--attachment", str(case.attachment_path)]
    cmd += ["--output-dir", str(output_dir / "_ui_bridge")]
    cmd += ["--timeout-ms", str(max(1, timeout - 5) * 1000)]
    cmd += ["--status-poll-ms", "15000"]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"playwright bridge failed: rc={proc.returncode}; "
            f"stderr={proc.stderr.strip()[:600]}"
        )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    if payload.get("writer_status") not in ("completed", "failed"):
        # 面板从未进入终态（如 writer 工作流未被触发、桥接等待超时），
        # 视为执行失败，避免误回填会话后产生假 FAIL 检查。
        raise RuntimeError(
            f"playwright bridge exited without terminal status "
            f"(status={payload.get('writer_status')!r}); "
            f"stderr={proc.stderr.strip()[:600]}"
        )
    sse_capture_dict, sse_streams_for_draft, sse_text = _sse_from_bridge_payload(
        payload,
    )
    return {
        "conversation_id": payload.get("conversation_id", ""),
        "session_id": payload.get("session_id", ""),
        "session": {},
        "task_id": payload.get("task_id", ""),
        "writer_status": payload.get("writer_status", ""),
        "finish_reason": "FINISH_REASON_STOP",
        "approvals": 0,
        "final_text": payload.get("final_text"),
        "rendered_images": payload.get("rendered_images") or 0,
        "failed_images": payload.get("failed_images") or [],
        "placeholder_images": payload.get("placeholder_images") or [],
        "final_panel_visible": bool(payload.get("final_panel_visible")),
        "final_ui_diagnostics": payload.get("final_ui_diagnostics") or {},
        "retry_events": payload.get("retry_events") or [],
        "conversation_id_sources": payload.get("conversation_id_sources") or {},
        "sse_capture_dict": sse_capture_dict,
        "sse_streams_for_draft": sse_streams_for_draft,
        "sse_text": sse_text,
    }


def _sse_from_bridge_payload(payload: dict):
    """Rebuild StreamRecord objects from the bridge's in-browser SSE capture.

    The bridge patches XMLHttpRequest and returns current-protocol task events.
    Python's single task-event normalizer builds the same evidence used by the
    no-UI path.
    """
    sse = payload.get("sse_capture") or {}
    events = sse.get("events") or []
    capture = TaskCapture(task_id=str(sse.get("task_id") or ""))
    for event in events:
        if isinstance(event, dict):
            _consume_event(capture, event)
    capture_dict = task_capture_to_dict(
        capture, task_ids=[capture.task_id] if capture and capture.task_id else [],
        capture_mode="browser_xhr",
    ) or {"capture_mode": "browser_xhr", "capture_started": True, "streams": {}}
    capture_dict["total_artifact_events"] = int(sse.get("total_artifact_events") or 0)
    capture_dict["diagnostics"] = sse.get("diagnostics") or {}
    capture_dict["transport"] = sse.get("transport") or []
    observer = payload.get("sse_observer_capture")
    if observer:
        # The independent subscriber is diagnostic evidence, not a source of
        # replacement browser deltas or a synthetic browser end event.
        observer_dict, _records, _text = _sse_from_bridge_payload({"sse_capture": observer})
        observer_dict["capture_mode"] = "independent_task_subscription"
        capture_dict["observer"] = observer_dict
    draft_streams = select_streams(
        capture, slot="draft_document", content_type="text/markdown",
    )
    return capture_dict, draft_streams, concatenated_text(draft_streams)


def _extract_feishu_url(prompt_text: str) -> str:
    import re
    # 排除中文标点与闭合符，避免把 URL 末尾的“。”带进 lark-cli 调用。
    m = re.search(r"https?://[^\s，。；！？、（）\[\]{}'\"<>]+", prompt_text)
    return m.group(0) if m else ""


def _cross_ref_source(case) -> str | None:
    """Source text for cross-reference preservation assertions (uploaded fixture)."""
    rule = (case.assertions or {}).get("cross_ref") or {}
    if not rule.get("preserve_from_source"):
        return None
    if case.attachment_path and case.attachment_path.is_file():
        try:
            return case.attachment_path.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _artifact_assertion_facts(artifact: Any) -> dict[str, Any]:
    """Extract only assertion-relevant facts; never persist artifact bodies."""
    value = artifact
    if isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
        value = value["data"]
    facts: dict[str, Any] = {}
    if isinstance(artifact, dict) and artifact.get("schema"):
        facts["schema"] = str(artifact["schema"])
    if isinstance(value, dict):
        for key in ("success", "task_type", "patch_type"):
            if key in value:
                facts[key] = value[key]
        for key in ("items", "blocks", "instructions", "patches", "revisions", "hunks"):
            items = value.get(key)
            if isinstance(items, list):
                facts["item_count"] = len(items)
                facts["item_count_source"] = key
                break
    elif isinstance(value, list):
        facts["item_count"] = len(value)
        facts["item_count_source"] = "root"
    return facts


def _artifact_provider(artifact: Any, slot_id: str) -> str:
    """Read source identity, not the draft-only Core write-back projection."""
    if not isinstance(artifact, dict):
        return ""
    value = artifact.get("data") if isinstance(artifact.get("data"), dict) else artifact
    if slot_id == "target_document":
        return str(value.get("adapter") or "")
    binding = value.get("provider_binding")
    return str(binding.get("provider") or "") if isinstance(binding, dict) else ""


def _merge_session_slots(analysis, session_payload, base_url, token,
                         required_rules: dict | None = None) -> None:
    """Merge authoritative core-session slot facts into the trace slot view.

    The encapsulated writer workflow saves artifacts inside workspace tools, so
    the trace no longer carries per-slot records.  The core session is the
    source of truth for slot existence, extension, revision and IR stage.
    """
    if not session_payload:
        return
    from analyze_common import SlotRecord

    slots = session_payload.get("slots") or []
    required_rules = required_rules or {}
    merged = dict(analysis.slot_view)
    grouped: dict[str, list[dict]] = {}
    for slot in slots:
        sid = str((slot or {}).get("slot_id") or "")
        if not sid:
            continue
        grouped.setdefault(sid, []).append(slot)

    for sid, records in grouped.items():
        selected = [item for item in records if item.get("selected", True)]
        active = selected or records
        # A list-cardinality slot has one selected row per list_index.  Revision
        # numbers are scoped to each list item, so equal revision=1 values must
        # not be collapsed into a single artifact.
        list_indices = {
            item.get("list_index")
            for item in active
            if item.get("list_index") is not None
        }
        slot = active[-1]
        raw = slot.get("artifact_value")
        descriptor = slot.get("document") or {}
        representation = str(
            descriptor.get("representation")
            if isinstance(descriptor, dict) else ""
        ).lower()
        schema = str(
            descriptor.get("schema")
            if isinstance(descriptor, dict) else ""
        ).lower()
        content_type = str(slot.get("content_type") or "").lower()
        if (representation == "markdown" or schema.startswith("text/markdown")
                or content_type.startswith("text/markdown")):
            extension = ".md"
        elif representation == "ir" or "lazymind.writer+json" in schema:
            extension = ".lmd"
        elif content_type == "application/json":
            extension = ".json"
        else:
            extension = ""
        if not extension and isinstance(raw, dict):
            # 部分槽位直接内联产物（如 outline 以 {"data": "<markdown>"} 存储）。
            data = raw.get("data")
            if isinstance(data, str) and re.search(r"(?m)^#", data):
                extension = ".md"
            elif isinstance(data, dict) and isinstance(data.get("blocks"), list):
                extension = ".lmd"
        rec = SlotRecord(
            slot=sid,
            extension=extension,
            selected=bool(slot.get("selected", True)),
            stage=None,
            provider=(str(slot.get("provider") or "")
                      if sid not in ("source_document", "target_document") else ""),
        )
        rec.revisions.extend(
            int(item.get("revision") or 0) for item in active
        )
        if list_indices:
            rec.properties.update({
                "item_count": len(list_indices),
                "item_count_source": "session.list_index",
            })
        must_load = extension == ".lmd" or any(
            key in (required_rules.get(sid) or {})
            for key in ("success", "task_type", "patch_type", "schema", "provider")
        )
        if must_load:
            try:
                artifact, _ct = load_slot(base_url, token, session_payload, sid)
                artifact_facts = _artifact_assertion_facts(artifact)
                rec.properties.update(artifact_facts)
                artifact_provider = _artifact_provider(artifact, sid)
                if artifact_provider and (sid in ("source_document", "target_document")
                                          or not rec.provider):
                    rec.provider = artifact_provider
                    rec.properties["provider_source"] = (
                        "artifact.adapter" if sid == "target_document"
                        else "artifact.provider_binding.provider")
                if list_indices:
                    # An IR section may itself contain many blocks.  min_items
                    # describes list-cardinality draft sections, not blocks
                    # inside the final selected section.
                    rec.properties.update({
                        "item_count": len(list_indices),
                        "item_count_source": "session.list_index",
                    })
                if isinstance(artifact, dict):
                    data = artifact.get("data") if isinstance(artifact.get("data"), dict) else artifact
                    stage = data.get("stage")
                    meta = data.get("metadata") or {}
                    if not stage:
                        stage = meta.get("stage")
                    if not stage and isinstance(meta, dict):
                        source_meta = ((meta.get("source") or {}).get("meta"))
                        if isinstance(source_meta, dict):
                            stage = source_meta.get("stage")
                    if stage:
                        rec.stage = str(stage)
            except Exception:
                pass
        merged[sid] = rec
    analysis.slot_view = merged
    final_provider = (merged.get("draft_document") or SlotRecord("")).provider
    providers = {rec.provider for rec in merged.values() if rec.provider}
    if final_provider or len(providers) == 1:
        analysis.provider = final_provider or providers.pop()


def _merge_session_steps(analysis, session_payload) -> None:
    """Use the durable Core session as the authoritative workflow step path."""
    if not session_payload:
        return
    ordered: list[str] = []
    for step in session_payload.get("steps") or []:
        sid = str((step or {}).get("step_id") or "")
        if sid and sid not in ("__start__", "__end__") and sid not in ordered:
            ordered.append(sid)
    if ordered:
        analysis.step_path = ordered


def _merge_session_route(analysis, session_payload, base_url, token) -> None:
    """Use the immutable writer_command instead of inferring route from spans."""
    if not session_payload:
        return
    try:
        command, _content_type = load_slot(
            base_url, token, session_payload, "writer_command",
        )
    except Exception:
        return
    while isinstance(command, dict) and "data" in command:
        command = command["data"]
    if isinstance(command, str):
        try:
            command = json.loads(command)
        except json.JSONDecodeError:
            return
    if not isinstance(command, dict):
        return
    action = str(command.get("action") or "")
    route = {
        "create": "create",
        "use_outline": "expand",
        "rewrite": "rewrite",
        "revise": "revise",
        "read": "read",
    }.get(action)
    if route:
        analysis.route = route


def _merge_session_modify_types(analysis, session_payload, base_url, token) -> None:
    """Fill modify_types from the session's document_modify_plan when the trace
    (workspace-encapsulated) carries no plan records."""
    if not session_payload or analysis.modify_types:
        return
    try:
        artifact, _ct = load_slot(base_url, token, session_payload, "document_modify_plan")
    except Exception:
        return
    data = (
        artifact.get("data")
        if isinstance(artifact, dict) and isinstance(artifact.get("data"), dict)
        else artifact
    )
    instructions = (data or {}).get("instructions") or []
    operations = sorted({
        str(item.get("modify_type") or "")
        for item in instructions if isinstance(item, dict)
    })
    if operations:
        analysis.modify_types = operations


def _safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"UI bridge subprocess timed out after {exc.timeout} seconds; command arguments omitted"
    text = str(exc)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+\-/=]+", "Bearer [REDACTED]", text)
    text = re.sub(r"([?&](?:token|signature|authorization|x-oss-signature)=)[^&\s]+",
                  r"\1[REDACTED]", text, flags=re.IGNORECASE)
    return text[:1200]


def _write_report(path: Path, result: FuncResult) -> None:
    passed = sum(1 for c in result.checks if c["status"] == "PASS")
    failed = sum(1 for c in result.checks if c["status"] == "FAIL")
    warned = sum(1 for c in result.checks if c["status"] == "WARN")
    lines = [
        f"# 功能测试报告：{result.conversation_id or result.session_id}",
        "",
        f"- mode: **{result.mode}**",
        f"- session_id: {result.session_id}",
        f"- writer_status: **{result.writer_status}**",
        f"- mechanical_verdict: **{result.mechanical_verdict}**",
        f"- elapsed: {result.elapsed_s}s",
        f"- trace: {result.trace_path or 'n/a'}",
        f"- evidence: {result.evidence_path or 'n/a'}",
        f"- feishu revision: {result.feishu_before} → {result.feishu_after}",
        f"- workflow step retries: {result.retry_count} | panel recoveries: "
        f"{result.panel_recovery_count} | internal error spans: "
        f"{result.internal_error_count}（自动取证见 evidence.json）",
        f"- models: {result.models or 'n/a'} | providers: {result.providers or 'n/a'} "
        f"| llm_calls: {result.llm_call_count} | image_calls: {result.image_call_count}",
        "",
        "## 机械检查（脚本比对）",
        "",
        f"汇总：PASS={passed} / FAIL={failed} / WARN={warned}"
        "（最终结论由 LLM 结合异常分析判定）",
        "",
        "| Status | Check | Detail |",
        "|---|---|---|",
    ]
    for c in result.checks:
        lines.append(f"| {c['status']} | {c['name']} | {c['detail']} |")
    lines.extend([
        "",
        "## 异常与说明（LLM 分析）",
        "",
        "<!-- 由 Agent 按 SKILL.md 填写：逐项说明 FAIL/WARN 的证据链与根因判断，"
        "区分可恢复偶发异常与真实缺陷。 -->",
        "",
        "## 最终结论（LLM 判定）",
        "",
        "<!-- PASS / PASS_WITH_RETRY / FAIL，并给出依据。 -->",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--recipe", choices=["text_intent", "explicit_mention"],
                   default="text_intent")
    p.add_argument("--no-ui", action="store_true",
                   help="skip the Playwright bridge; run pure API + SSE capture")
    p.add_argument("--no-trace", action="store_true")
    p.add_argument("--no-checks", action="store_true")
    p.add_argument("--feishu-revision-before", type=int, default=None)
    return p


def main() -> int:
    args = _parser().parse_args()
    case = load_writer_e2e_scenario(args.scenario)
    try:
        result = run_case(
            case, Path(args.output_dir),
            base_url=args.base_url,
            timeout=args.timeout,
            recipe=args.recipe,
            use_ui=not args.no_ui,
            fetch_trace_after=not args.no_trace,
            compute_func_checks=not args.no_checks,
            feishu_revision_before=args.feishu_revision_before,
        )
    except Exception as exc:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        failure = {
            "scenario": case.scenario,
            "writer_status": "error",
            "finish_reason": "execution_exception",
            "error_type": type(exc).__name__,
            "error": _safe_error_message(exc),
            "evidence_preserved": True,
        }
        (output_dir / "run_N.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(_run_result_payload(result), ensure_ascii=False, indent=2))
    # 机械判决用于自动化退出码；测试表中的最终业务判决仍由 Agent/人工
    # 结合 evidence 独立填写。
    return 0 if result.mechanical_verdict in {"PASS", "PASS_WITH_RETRY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
