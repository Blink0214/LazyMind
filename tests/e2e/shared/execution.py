"""Shared execution lifecycle for Writer functional and performance runners.

Transport-specific waiting and suite-specific assertions stay in their runners;
the common setup, request, session correlation, and trace persistence live here.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared.api import (
    ChatResult,
    Session,
    fetch_latest_session,
    fetch_workflow_session,
    send_chat,
    upload_attachment,
)
from shared.observability import fetch_trace
from shared.preflight import reset_provider_baseline


@dataclass
class PreparedExecution:
    started_at: float
    session: Session
    attachments: list[str]
    provider_revision_before: int | None


@dataclass
class TraceEvidence:
    trace: dict | None
    trace_id: str | None
    path: Path
    error: str | None


def prepare_execution(case: Any, *, base_url: str, timeout: int,
                      provider_revision_before: int | None = None) -> PreparedExecution:
    """Login, fail-fast provider preflight, then upload the optional attachment."""
    started_at = time.time()
    session = Session.login(base_url=base_url)
    revision = provider_revision_before
    if revision is None:
        revision = reset_provider_baseline(case, timeout=timeout)

    attachments: list[str] = []
    if case.has_attachment:
        uploaded = upload_attachment(
            base_url, session.token, str(case.attachment_path),
        )
        attachments.append(uploaded.stored_path)
    return PreparedExecution(
        started_at=started_at,
        session=session,
        attachments=attachments,
        provider_revision_before=revision,
    )


def send_case(case: Any, *, base_url: str, token: str,
              attachments: list[str], recipe: str,
              on_conversation_id=None) -> ChatResult:
    """Send the common initial Writer request for an API-mode run."""
    return send_chat(
        base_url=base_url,
        token=token,
        prompt_text=case.prompt_text,
        recipe=recipe,
        attachment_paths=attachments,
        on_conversation_id=on_conversation_id,
    )


def correlate_workflow_session(state: dict, session_payload: dict, *,
                               observed_source: str = "UI") -> None:
    """Require the observed workflow panel/session to match Core exactly."""
    core_session_id = str(session_payload.get("session_id") or "")
    observed_session_id = str(state.get("session_id") or "")
    if not observed_session_id:
        raise RuntimeError(
            f"{observed_source} workflow session id missing from this run"
        )
    if not core_session_id:
        raise RuntimeError(
            "Core workflow session id missing for the captured conversation"
        )
    if core_session_id != observed_session_id:
        raise RuntimeError(
            f"{observed_source}/Core workflow session mismatch for the captured "
            f"conversation: observed_session_id={observed_session_id}, "
            f"core_session_id={core_session_id}"
        )


def resolve_workflow_session(*, base_url: str, token: str,
                             conversation_id: str, observed_session_id: str,
                             initial_session: dict | None = None,
                             attempts: int = 10, retry_delay_s: float = 5,
                             observed_source: str = "UI") -> dict:
    """Fetch the authoritative Core session and correlate it with this run."""
    session_payload = dict(initial_session or {})
    last_error: Exception | None = None
    if not session_payload:
        for attempt in range(attempts):
            try:
                session_payload = fetch_latest_session(
                    base_url, token, conversation_id,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(retry_delay_s)
    if not session_payload:
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(
            f"Core workflow session missing for conversation {conversation_id}{detail}"
        )

    session_id = str(session_payload.get("session_id") or "")
    if session_id:
        try:
            session_payload = fetch_workflow_session(base_url, token, session_id)
        except Exception:
            # The latest-session payload still carries the authoritative ID and
            # slots; detailed step attempts are supplemental evidence.
            pass
    correlate_workflow_session(
        {"session_id": observed_session_id}, session_payload,
        observed_source=observed_source,
    )
    return session_payload


def collect_trace_evidence(*, conversation_id: str,
                           workflow_session_id: str | None,
                           started_at: float, finished_at: float,
                           output_path: Path, retries: int = 3,
                           retry_delay_s: float = 3,
                           error_formatter: Callable[[BaseException], str] = str,
                           backend: str | None = None) -> TraceEvidence:
    """Fetch trace with bounded retries and persist it when available."""
    trace_json = None
    error = None
    for attempt in range(retries):
        try:
            trace_json = fetch_trace(
                conversation_id,
                workflow_session_id=workflow_session_id,
                started_at=_to_iso(started_at),
                finished_at=_to_iso(finished_at),
                backend=backend or os.environ.get(
                    "LAZYLLM_TRACE_CONSUME_BACKEND", "auto",
                ),
            )
            error = None
            break
        except Exception as exc:
            error = f"trace fetch failed: {error_formatter(exc)}"
            if attempt + 1 < retries:
                time.sleep(retry_delay_s)

    trace_id = None
    if trace_json is not None:
        trace_json = {**trace_json, "metadata": {
            **(trace_json.get("metadata") or {}),
            # Current LazyLLM dynamic supplier retains usage across forward calls.
            # Declare this contract; the analyzer must not guess from monotonicity.
            "llm_usage_semantics": os.environ.get(
                "E2E_LLM_USAGE_SEMANTICS", "cumulative_entity"),
            "run_context": {"conversation_id": conversation_id,
                            "workflow_session_id": workflow_session_id},
        }}
        output_path.write_text(
            json.dumps(trace_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        trace_id = trace_json.get("id")
    return TraceEvidence(
        trace=trace_json,
        trace_id=trace_id,
        path=output_path,
        error=error,
    )


def _to_iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()
