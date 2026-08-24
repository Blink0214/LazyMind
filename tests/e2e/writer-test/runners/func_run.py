"""Functional-mode runner: execute + collect evidence + mechanical checks.

Default mode is ``--ui`` which invokes the Node-side Playwright bridge
(``frontend/tests/e2e/writer_bridge.mjs``) to drive the chat UI; ``--no-ui``
runs the chat through ``shared.api`` and captures the live SSE stream via
``shared.observability``.

The runner executes one scenario (writer-e2e C0X/I0X, or a cases/ directory
case), collects the evidence package and runs the deterministic checks:

  case_<N>/
    run_N.json         runner envelope (status, feishu before/after, ...)
    final.md           markdown artifact pulled from the session
    trace.json         normalized Langfuse/local trace (when fetch succeeds)
    checks.json        mechanical check results (PASS/FAIL/WARN)
    evidence/          expected.json + facts.json + draft_document +
                       resolved_media_assets + sse.json
    report.md          mechanical table + LLM anomaly/final-verdict sections
    _ui_bridge/        Playwright artifacts (UI mode)

The exit code only reflects whether the execution completed
(``writer_status == "completed"``); the PASS/FAIL verdict is produced by the
Agent reading SKILL.md + evidence, per the hybrid judgment model.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tests/e2e"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))


from shared.api import (
    ChatResult,
    Session,
    fetch_latest_session,
    load_slot,
    send_chat,
    upload_attachment,
    wait_for_writer_completion,
)
from shared.case_loader import Case, load_case, load_writer_e2e_scenario
from shared.feishu import fetch_revision, reset_feishu_document
from shared.observability import (
    StreamRecord,
    TaskCapture,
    collect as collect_sse,
    concatenated_text,
    fetch_trace,
    select_complete_streams,
)
from analyze_common import Check  # noqa: E402


DEFAULT_BASE_URL = os.environ.get("LAZYMIND_BASE_URL", "http://localhost:8090")
DEFAULT_TIMEOUT = 1800
SESSION_GRACE_S = 20
PLAYWRIGHT_BRIDGE = REPO_ROOT / "frontend/tests/e2e/writer_bridge.mjs"


def _analyzers():
    """Lazily import the analyzers so the module loads without trace deps."""
    from analyze_common import analyze, run_checks
    from analyze_func import run_func_checks
    return analyze, run_checks, run_func_checks


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
    approvals: int
    final_artifact_path: str | None
    final_artifact_text: str | None
    trace_id: str | None
    trace_path: str | None
    sse_capture: dict | None
    checks: list[dict] = field(default_factory=list)
    mode: str = "ui"
    facts_path: str | None = None
    checks_path: str | None = None
    report_path: str | None = None
    evidence_dir: str | None = None
    feishu_before: int | None = None
    feishu_after: int | None = None
    retry_events: list = field(default_factory=list)
    retry_count: int = 0
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
    started_at = time.time()
    state: dict = {}

    session_obj = Session.login(base_url=base_url)
    state["token"] = session_obj.token
    state["user_id"] = session_obj.user_id

    if case.has_attachment:
        uploaded = upload_attachment(base_url, session_obj.token, str(case.attachment_path))
        state["attachments"] = [uploaded.stored_path]
    else:
        state["attachments"] = []

    # writer-e2e scenarios carry a Feishu baseline; reset it before the run
    # unless the caller already supplied a before-revision.
    feishu_before = feishu_revision_before
    if feishu_before is None and case.has_feishu_reference:
        extras = case.extras or {}
        try:
            feishu_before = reset_feishu_document(
                extras["feishu_reference"],
                extras["feishu_history_version_id"],
                int(extras["feishu_baseline_revision_id"]),
                timeout=min(timeout, 180),
            ).revision_id
        except Exception as exc:
            print(f"[feishu reset skipped] {exc}", file=sys.stderr)

    prompt_file = str(case.prompt_path) if case.prompt_path else None
    if prompt_file is None and case.prompt_text:
        bridge_dir = output_dir / "_ui_bridge"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = str(bridge_dir / "prompt.md")
        Path(prompt_file).write_text(case.prompt_text + "\n", encoding="utf-8")

    if use_ui:
        chat_info = _run_ui_bridge(
            case, base_url, state["token"], started_at, timeout, output_dir,
            prompt_file=prompt_file,
        )
    else:
        chat_info = _run_api(case, base_url, state["token"], state["attachments"], started_at, timeout)

    state["conversation_id"] = chat_info["conversation_id"]
    if not state["conversation_id"] and use_ui:
        # UI 页面 URL 不含会话 ID；按创建时间从 core 会话列表回填本次运行
        # 新建的会话（取 create_time 最接近本次启动时刻者）。
        try:
            state["conversation_id"] = _resolve_conversation_id(
                base_url, session_obj.token, started_at,
            )
        except Exception as exc:
            print(f"[conversation id resolve skipped] {exc}", file=sys.stderr)
    state["session_id"] = chat_info["session_id"]
    state["task_id"] = chat_info.get("task_id", "")
    state["writer_status"] = chat_info.get("writer_status", "")

    session_payload = chat_info.get("session") or {}
    if not session_payload and state.get("conversation_id"):
        # 工作流刚完成时 latest-session 可能短暂不可见/未落盘，做有界重试
        # （UI 面板 completed 到 core 会话可查询之间存在延迟）。
        for _attempt in range(10):
            try:
                session_payload = fetch_latest_session(
                    base_url, session_obj.token, state["conversation_id"],
                )
                break
            except Exception as exc:
                if _attempt == 0:
                    print(f"[latest session retry] {exc}", file=sys.stderr)
                session_payload = {}
                time.sleep(5)

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
    final_md_path = output_dir / "final.md"
    if final_artifact_text:
        final_md_path.write_text(final_artifact_text, encoding="utf-8")

    sse_capture_dict: dict | None = None
    sse_streams = []
    sse_text = ""
    if chat_info.get("sse_capture_dict"):
        sse_capture_dict = chat_info["sse_capture_dict"]
        sse_streams = chat_info.get("sse_streams_for_draft", [])
        sse_text = chat_info.get("sse_text", "")

    finished = time.time()
    trace_id = None
    trace_error = None
    trace_json = None
    trace_path = output_dir / "trace.json"
    if fetch_trace_after and state.get("conversation_id"):
        # Langfuse 偶发返回 422/5xx；有界重试后再放弃，避免单次瞬态错误
        # 直接导致整份分析缺失（如 C05 实测 422）。
        for _attempt in range(3):
            try:
                trace_json = fetch_trace(
                    state["conversation_id"],
                    workflow_session_id=state.get("session_id") or None,
                    started_at=_iso(started_at),
                    finished_at=_iso(finished),
                    backend=os.environ.get("LAZYLLM_TRACE_CONSUME_BACKEND", "langfuse"),
                )
                break
            except Exception as exc:
                trace_error = f"trace fetch failed: {exc}"
                time.sleep(3)
        if trace_json is not None:
            trace_path.write_text(
                json.dumps(trace_json, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            trace_id = trace_json.get("id")

    retry_events = list(chat_info.get("retry_events") or [])
    trace_error_spans: list[dict] = []
    if trace_json is not None:
        for obs in trace_json.get("observations") or []:
            if str(obs.get("level")) != "ERROR":
                continue
            attrs = ((obs.get("metadata") or {}).get("attributes") or {})
            trace_error_spans.append({
                "name": str(obs.get("name") or ""),
                "start": str(obs.get("startTime") or ""),
                "message": str(
                    attrs.get("lazyllm.error.message")
                    or attrs.get("langfuse.observation.status_message")
                    or attrs.get("lazyllm.io.output") or ""
                )[:600],
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

    checks: list = []
    facts_data = None
    run_models: list = []
    run_providers: list = []
    run_llm_calls = 0
    run_image_calls = 0
    if compute_func_checks and trace_json is not None:
        analyze, run_common_checks, run_func_checks = _analyzers()
        analysis = analyze(trace_json)
        # 新工作流把产物保存封装在 workspace 内，trace 不再包含逐槽位记录；
        # 用 core 会话的权威槽位（revision/extension/stage）补全槽位视图。
        _merge_session_slots(analysis, session_payload, base_url, session_obj.token)
        # 修改计划同样只在会话槽位中可见（trace 无 save_artifacts 记录）。
        _merge_session_modify_types(analysis, session_payload, base_url, session_obj.token)
        facts_data = analysis.to_dict()
        run_models = list(analysis.models or [])
        run_providers = list(analysis.providers or [])
        run_llm_calls = int(analysis.llm_call_count or 0)
        run_image_calls = int(analysis.image_call_count or 0)
        common_checks = run_common_checks(analysis, expected=case.assertions or {})
        write_back_calls = sum(analysis.write_back.values())
        checks = run_func_checks(
            common_checks=common_checks,
            streams_for_draft=sse_streams if sse_capture_dict else None,
            final_artifact=final_artifact,
            sse_text=sse_text,
            cross_ref_rule=(case.assertions or {}).get("cross_ref"),
            cross_ref_source=_cross_ref_source(case),
            ref_integrity=(case.assertions or {}).get("ref_integrity", False),
            numbering_rule=(case.assertions or {}).get("numbering"),
            ref_number_consistency=bool(
                (case.assertions or {}).get("ref_number_consistency")),
            provider=analysis.provider,
            feishu_before=feishu_before,
            feishu_after=None,  # populated below
            provider_revision_rule=((case.assertions or {}).get("write_back", {}) or {}).get("provider_revision"),
            write_back_calls=write_back_calls,
            write_back_tool=dict(analysis.write_back),
            write_back_calls_expected=((case.assertions or {}).get("write_back") or {}).get("calls"),
            write_back_tool_expected=str(((case.assertions or {}).get("write_back") or {}).get("tool") or ""),
            media_assets=media_assets,
            media_min_images=media_min_images,
            ui_editable_expected=((case.assertions or {}).get("ui_editable")),
            new_revision_expected=((case.assertions or {}).get("new_revision")),
            final_revision=final_revision,
        )

    # Display verification is browser-only; the Playwright bridge reports the
    # number of draft images that actually rendered.
    if media_min_images > 0:
        if use_ui:
            rendered = int(chat_info.get("rendered_images") or 0)
            failed_images = list(chat_info.get("failed_images") or [])
            failed = len(failed_images)
            if rendered >= media_min_images and failed == 0:
                checks.append(Check(
                    "media.display", "PASS",
                    f"rendered={rendered}, failed=0, expected >= {media_min_images}",
                ))
            elif rendered >= media_min_images and failed > 0:
                checks.append(Check(
                    "media.display", "FAIL",
                    f"rendered={rendered}, failed={failed}: {failed_images[:3]}, "
                    f"expected >= {media_min_images}",
                ))
            else:
                checks.append(Check(
                    "media.display", "FAIL",
                    f"rendered={rendered}, failed={failed}, "
                    f"expected >= {media_min_images}",
                ))
        else:
            checks.append(Check(
                "media.display", "WARN",
                "no-ui 模式无法验证浏览器渲染；需要 --ui",
            ))
    # 统一转为可序列化的 dict，供 checks.json / 飞书补丁循环使用。
    checks = [c if isinstance(c, dict) else asdict(c) for c in checks]

    # Optional: feishu after-revision fetch.
    feishu_after = None
    if ((case.assertions or {}).get("write_back") or {}).get("provider_revision") and "feishu" in case.prompt_text:
        feishu_url = _extract_feishu_url(case.prompt_text)
        if feishu_url:
            try:
                feishu_after = fetch_revision(feishu_url, timeout=min(timeout, 180)).revision_id
            except Exception as exc:
                trace_error = (trace_error + f"; feishu fetch: {exc}") if trace_error else f"feishu fetch: {exc}"
        # Patch the existing check with the after revision.
        if compute_func_checks and trace_json is not None:
            feishu_rule = str(((case.assertions or {}).get("write_back") or {}).get("provider_revision") or "any")
            for c in checks:
                if c.get("name") == "feishu.revision":
                    from analyze_func import check_feishu_revision
                    updated = check_feishu_revision(
                        provider=analysis.provider,
                        before=feishu_before,
                        after=feishu_after,
                        rule=feishu_rule,
                    )
                    c["status"] = updated.status
                    c["detail"] = updated.detail

    # 证据包：机械比对结果 + 大模型判定的输入材料。
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "expected.json").write_text(
        json.dumps(case.assertions or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if isinstance(final_artifact, str):
        (evidence_dir / "draft_document.md").write_text(final_artifact, encoding="utf-8")
    elif final_artifact is not None:
        (evidence_dir / "draft_document.json").write_text(
            json.dumps(final_artifact, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if media_assets is not None:
        (evidence_dir / "resolved_media_assets.json").write_text(
            json.dumps(media_assets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    steps_summary: list[dict] = []
    # workflow-sessions:latest 载荷不含 steps 字段（实测），若会话层没有步骤
    # 状态，则从 trace 的 workspace span 推导（ERROR→failed，DEFAULT→completed），
    # 保证 retries.json 的步骤级证据可用。
    session_steps = session_payload.get("steps") or []
    if session_steps:
        for st in session_steps:
            if not isinstance(st, dict):
                continue
            item = {k: st.get(k) for k in ("step_id", "status", "attempt") if k in st}
            if st.get("error"):
                item["error"] = str(st.get("error"))[:300]
            steps_summary.append(item)
    elif trace_json is not None:
        step_by_workspace = {
            "writer_prepare_workspace": "prepare",
            "writer_outline_workspace": "outline",
            "writer_draft_workspace": "write_document",
        }
        seen_steps: dict[str, dict] = {}
        for obs in trace_json.get("observations") or []:
            step_id = step_by_workspace.get(str(obs.get("name") or ""))
            if not step_id:
                continue
            rec = seen_steps.setdefault(
                step_id, {"step_id": step_id, "status": "completed", "attempt": 0},
            )
            rec["attempt"] = int(rec.get("attempt") or 0) + 1
            if str(obs.get("level")) == "ERROR":
                rec["status"] = "failed"
        steps_summary = list(seen_steps.values())
    (evidence_dir / "retries.json").write_text(
        json.dumps({
            "retry_events": retry_events,
            "trace_error_spans": trace_error_spans,
            "session_steps": steps_summary,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if sse_capture_dict:
        (evidence_dir / "sse.json").write_text(
            json.dumps(sse_capture_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    facts_path = evidence_dir / "facts.json"
    facts_path.write_text(
        json.dumps(facts_data or {"error": trace_error or "no trace"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    checks_path = output_dir / "checks.json"
    checks_path.write_text(
        json.dumps(checks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_path = output_dir / "report.md"

    result = FuncResult(
        conversation_id=state.get("conversation_id") or "",
        session_id=state.get("session_id") or "",
        task_id=state.get("task_id") or "",
        started_at=started_at,
        finished_at=finished,
        elapsed_s=round(finished - started_at, 3),
        finish_reason=chat_info.get("finish_reason", ""),
        writer_status=state.get("writer_status") or "",
        approvals=chat_info.get("approvals", 0),
        retry_events=retry_events,
        retry_count=len(retry_events),
        models=run_models,
        providers=run_providers,
        llm_call_count=run_llm_calls,
        image_call_count=run_image_calls,
        final_artifact_path=str(final_md_path) if final_artifact_text else None,
        final_artifact_text=final_artifact_text,
        trace_id=trace_id,
        trace_path=str(trace_path) if trace_json is not None else None,
        sse_capture=sse_capture_dict,
        checks=[c if isinstance(c, dict) else asdict(c) for c in checks],
        mode="ui" if use_ui else "no-ui",
        facts_path=str(facts_path),
        checks_path=str(checks_path),
        report_path=str(report_path),
        evidence_dir=str(evidence_dir),
        feishu_before=feishu_before,
        feishu_after=feishu_after,
    )
    (output_dir / "run_N.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_report(report_path, result)
    return result


# ---------------------------------------------------------------------------
# Execution modes
# ---------------------------------------------------------------------------


def _run_api(case: Case, base_url: str, token: str, attachments: list,
             started_at: float, timeout: int) -> dict:
    """Pure API: send_chat + wait + save_final + capture SSE if task_id known."""
    chat = send_chat(
        base_url=base_url, token=token,
        prompt_text=case.prompt_text, recipe="text_intent",
        attachment_paths=attachments,
    )
    if not chat.conversation_id:
        raise RuntimeError("chat returned no conversation_id")
    captured = _wait_with_sse(base_url, token, chat.conversation_id, started_at, timeout)
    final_text = _fetch_final_text(base_url, token, captured["session"])
    sse_capture = captured["sse_capture_dict"]
    sse_for_draft = captured["sse_streams_for_draft"]
    sse_text = captured["sse_text"]
    return {
        "conversation_id": chat.conversation_id,
        "session_id": (captured["session"] or {}).get("session_id", ""),
        "session": captured["session"] or {},
        "task_id": "",
        "writer_status": (captured["session"] or {}).get("status", ""),
        "finish_reason": chat.finish_reason,
        "approvals": captured["approvals"],
        "final_text": final_text,
        "sse_capture_dict": sse_capture,
        "sse_streams_for_draft": sse_for_draft,
        "sse_text": sse_text,
    }


def _wait_with_sse(base_url: str, token: str, conversation_id: str,
                   started_at: float, timeout: int) -> dict:
    """Run the regular writer-completion wait loop; if a task_id is known
    (after the first wait), also capture SSE in parallel."""
    outcome = wait_for_writer_completion(
        base_url, token, conversation_id,
        timeout_s=timeout, session_start_grace_s=SESSION_GRACE_S,
    )
    session = outcome.session or {}
    task_id = _find_task_id(base_url, token, conversation_id)
    capture: TaskCapture | None = None
    if task_id:
        capture = _collect_sse_uncoupled(base_url, token, task_id, timeout)
    sse_capture_dict = asdict(capture) if capture else None
    sse_streams_for_draft = (
        select_complete_streams(capture, slot="draft_document", content_type="text/markdown")
        if capture else []
    )
    sse_text = concatenated_text(sse_streams_for_draft)
    return {
        "session": session,
        "approvals": 0,
        "task_id": task_id,
        "sse_capture_dict": sse_capture_dict,
        "sse_streams_for_draft": sse_streams_for_draft,
        "sse_text": sse_text,
    }


def _collect_sse_uncoupled(base_url: str, token: str, task_id: str, timeout: int):
    """Best-effort SSE collect; the task may have already terminated."""
    try:
        return collect_sse(base_url, token, task_id, timeout_s=min(timeout, 300))
    except Exception:
        return None


def _find_task_id(base_url, token, conversation_id) -> str:
    """Look up the latest writer task id for a conversation, if exposed."""
    try:
        outcome = wait_for_writer_completion(
            base_url, token, conversation_id, timeout_s=2,
            session_start_grace_s=2,
        )
        session = outcome.session or {}
        return str(session.get("latest_task_id") or session.get("task_id") or "")
    except Exception:
        return ""


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


def _run_ui_bridge(case: Case, base_url: str, token: str,
                   started_at: float, timeout: int, output_dir: Path,
                   prompt_file: str | None = None) -> dict:
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
        "--scenario", case.scenario,
        "--case", str(case.case_num),
        "--prompt-file", prompt_file or str(case.prompt_path),
    ]
    if case.has_attachment:
        cmd += ["--attachment", str(case.attachment_path)]
    cmd += ["--output-dir", str(output_dir / "_ui_bridge")]

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
        "retry_events": payload.get("retry_events") or [],
        "sse_capture_dict": sse_capture_dict,
        "sse_streams_for_draft": sse_streams_for_draft,
        "sse_text": sse_text,
    }


def _sse_from_bridge_payload(payload: dict):
    """Rebuild StreamRecord objects from the bridge's in-browser SSE capture.

    The bridge patches XMLHttpRequest and captures the live task stream
    (``artifact_stream_start / artifact_stream / _end / _abort``), normalized
    to the same start/delta/end shape the Python side already consumes, so the
    existing sse.protocol / sse.streaming / sse.reconstruction checks apply to
    UI mode as well.
    """
    sse = payload.get("sse_capture") or {}
    raw_streams = sse.get("streams") or []
    if not raw_streams:
        return None, None, ""
    streams = []
    for item in raw_streams:
        streams.append(StreamRecord(
            stream_id=str(item.get("stream_id") or ""),
            events=list(item.get("events") or []),
            text=str(item.get("text") or ""),
            slots=set(item.get("slots") or []),
            content_types=set(item.get("content_types") or []),
            aborted=bool(item.get("aborted")),
        ))
    capture = TaskCapture(
        task_id=str(sse.get("task_id") or ""),
        streams={rec.stream_id: rec for rec in streams},
    )
    capture_dict = {
        "task_id": capture.task_id,
        "total_artifact_events": int(sse.get("total_artifact_events") or 0),
        "streams": [{
            "stream_id": rec.stream_id,
            "events": rec.events,
            "text": rec.text,
            "slots": sorted(rec.slots),
            "content_types": sorted(rec.content_types),
            "aborted": rec.aborted,
        } for rec in streams],
    }
    draft_streams = select_complete_streams(
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


def _resolve_conversation_id(base_url: str, token: str,
                             started_at: float, window_s: int = 120) -> str:
    """Resolve the conversation created by this run via the core list API.

    The chat UI page never carries ``conversation_id`` in its URL, so UI-mode
    runs fall back to listing recent conversations and picking the one whose
    ``create_time`` is closest to the run start (within ``window_s``).
    """
    import json
    import urllib.request
    from datetime import datetime, timezone

    start = datetime.fromtimestamp(started_at, tz=timezone.utc)
    req = urllib.request.Request(
        f"{base_url}/api/core/conversations?page_size=50&page_token=",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    candidates: list[tuple[float, str]] = []
    for conv in data.get("conversations") or []:
        created = str(conv.get("create_time") or "")
        try:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        delta = abs((parsed - start).total_seconds())
        if delta <= window_s:
            candidates.append((delta, str(conv.get("conversation_id") or "")))
    if not candidates:
        raise RuntimeError("no conversation created within the run window")
    candidates.sort()
    return candidates[0][1]


def _merge_session_slots(analysis, session_payload, base_url, token) -> None:
    """Merge authoritative core-session slot facts into the trace slot view.

    The encapsulated writer workflow saves artifacts inside workspace tools, so
    the trace no longer carries per-slot records.  The core session is the
    source of truth for slot existence, extension, revision and IR stage.
    """
    if not session_payload:
        return
    from analyze_common import SlotRecord, _stage_for

    slots = session_payload.get("slots") or []
    merged = dict(analysis.slot_view)
    for slot in slots:
        sid = str(slot.get("slot_id") or "")
        if not sid:
            continue
        revision = int(slot.get("revision") or 0)
        existing = merged.get(sid)
        if existing is not None and existing.max_revision >= revision:
            continue
        raw = slot.get("artifact_value")
        path = ""
        if isinstance(raw, str):
            path = raw
        elif isinstance(raw, dict):
            path = str(raw.get("path") or raw.get("url") or "")
        m = re.search(r"\.(md|lmd|json|markdown)\b", path)
        extension = ("." + m.group(1)) if m else ""
        if not extension and isinstance(raw, dict):
            # 部分槽位直接内联产物（如 outline 以 {"data": "<markdown>"} 存储）。
            data = raw.get("data")
            if isinstance(data, str) and re.search(r"(?m)^#", data):
                extension = ".md"
            elif isinstance(data, dict) and isinstance(data.get("blocks"), list):
                extension = ".lmd"
        if not extension and str(slot.get("content_type") or "") == "application/json":
            extension = ".json"
        rec = SlotRecord(
            slot=sid,
            extension=extension,
            step_id=None,
            selected=bool(slot.get("selected", True)),
            stage=_stage_for(sid, []),
        )
        rec.revisions.append(revision)
        if extension == ".lmd":
            try:
                artifact, _ct = load_slot(base_url, token, session_payload, sid)
                if isinstance(artifact, dict):
                    data = artifact.get("data") if isinstance(artifact.get("data"), dict) else artifact
                    stage = data.get("stage")
                    if not stage:
                        meta = data.get("metadata") or {}
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


def _iso(epoch_s: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


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
        f"- elapsed: {result.elapsed_s}s",
        f"- trace: {result.trace_path or 'n/a'}",
        f"- facts: {result.facts_path or 'n/a'}",
        f"- checks: {result.checks_path or 'n/a'}",
        f"- evidence: {result.evidence_dir or 'n/a'}",
        f"- feishu revision: {result.feishu_before} → {result.feishu_after}",
        f"- retries: {result.retry_count} event(s)（自动取证见 evidence/retries.json）",
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
    p.add_argument("--cases-root", required=True)
    p.add_argument("--scenario", required=True)
    p.add_argument("--case", type=int, default=0,
                   help="case number for cases/ directories; ignored for C0X/I0X scenarios")
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
    if re.fullmatch(r"[A-Z]\d{2}", args.scenario.upper()):
        # C0X / I0X: functional case set from the legacy writer-e2e YAML.
        case = load_writer_e2e_scenario(args.scenario)
    else:
        case = load_case(args.cases_root, args.scenario, args.case)
    if case.prompt_text is None:
        print(f"missing prompt: {case.prompt_path}", file=sys.stderr)
        return 2
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
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    # 混合模式：runner 退出码只反映“执行是否完成”；PASS/FAIL 结论由
    # LLM 读取 checks.json + evidence 后按 SKILL.md 判定。
    return 0 if result.writer_status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
