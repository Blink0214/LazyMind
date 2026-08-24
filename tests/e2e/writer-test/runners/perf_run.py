"""Perf-mode runner: API-only path that runs one case and emits trace + stats.

This is the Writer side of the unified test suite in performance mode.
It uses ``tests/e2e/shared/*`` for auth/upload/chat/wait/feishu/trace and
``writer-test/analyzers/*`` for structural analysis + perf stats. The output
layout mirrors ``writer-benchmark/scripts/full_run.sh`` so the existing
report renderer can be reused.

Examples
--------
Run a single case by index and scenario, with trace + perf stats + document
metrics:

    python3 tests/e2e/writer-test/runners/perf_run.py \
        --cases-root tests/e2e/writer-test/cases \
        --scenario P01 --case 1 \
        --output-dir tests/e2e/writer-test/reports/perf_smoke

Skip trace fetch (output only run_N.json + final Markdown):

    python3 tests/e2e/writer-test/runners/perf_run.py \
        --cases-root ... --scenario P01 --case 1 \
        --output-dir ... --no-trace --no-stats
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


# Make sibling modules importable when invoked as a script.
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tests/e2e"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))


from shared.api import (
    ChatResult,
    Session,
    save_final_markdown,
    send_chat,
    upload_attachment,
    wait_for_writer_completion,
)
from shared.case_loader import Case, load_case, load_perf_case
from shared.feishu import reset_feishu_document
from shared.observability import fetch_trace
from analyze_common import analyze, run_checks
from analyze_perf import avg_traces, document_metrics
from shared.document_metrics import document_stats


SESSION_START_GRACE_S = 20
DEFAULT_BASE_URL = os.environ.get("LAZYMIND_BASE_URL", "http://localhost:8090")
DEFAULT_TIMEOUT = 1800


@dataclass
class RunResult:
    conversation_id: str
    started_at: float
    finished_at: float
    elapsed_s: float
    finish_reason: str
    attachments: list
    writer_session_id: str
    writer_status: str
    approvals: int
    final_artifact: dict | None
    final_artifact_error: str | None
    trace_id: str | None
    trace_path: str | None
    analysis: dict | None
    trace_error: str | None


def run_case(case: Case,
             output_dir: Path,
             *,
             base_url: str = DEFAULT_BASE_URL,
             timeout: int = DEFAULT_TIMEOUT,
             fetch_trace_after: bool = True,
             compute_stats: bool = True,
             recipe: str = "text_intent") -> RunResult:
    """End-to-end perf run for one case; writes run_N.json + trace.json + final.md."""
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    trace_path = output_dir / "trace.json"
    final_md_path = output_dir / "final.md"
    run_path = output_dir / "run_N.json"

    state: dict = {
        "token": None, "conversation_id": None, "session": None,
        "attachments": [], "approvals": 0,
    }

    # 1) Login
    session_obj = Session.login(base_url=base_url)
    state["token"] = session_obj.token

    # 2) Feishu baseline reset: prompt placeholder resolved by load_case into
    #    extras (URL + history/baseline from the cases YAML registry).
    if case.has_feishu_reference:
        extras = case.extras or {}
        try:
            reset_feishu_document(
                extras["feishu_reference"],
                str(extras["feishu_history_version_id"]),
                int(extras["feishu_baseline_revision_id"]),
                timeout=min(timeout, 180),
            )
            print(f"[feishu baseline reset] {extras['feishu_reference']}", file=sys.stderr)
        except Exception as exc:
            print(f"[feishu reset skipped] {exc}", file=sys.stderr)

    # 3) Upload attachment
    if case.has_attachment:
        uploaded = upload_attachment(base_url, session_obj.token, str(case.attachment_path))
        state["attachments"].append(uploaded.stored_path)

    # 4) Send chat
    chat_result: ChatResult = _send(case, state, base_url, session_obj.token,
                                    recipe=recipe)

    # 5) Wait + approve
    try:
        writer_session = _wait_and_approve(
            base_url, session_obj.token, chat_result.conversation_id, state,
            timeout=timeout,
        )
    except Exception as exc:
        finished = time.time()
        return _write_failure(run_path, started_at, finished, state, exc,
                              chat_result.finish_reason)

    state["session"] = writer_session
    if writer_session.get("status") != "completed":
        finished = time.time()
        return _write_failure(run_path, started_at, finished, state,
                              RuntimeError(
                                  f"writer did not complete: status={writer_session.get('status')}"
                              ),
                              chat_result.finish_reason)

    # 6) Save final markdown
    final_artifact = None
    final_artifact_error = None
    doc_stats_dict = None
    try:
        final_artifact = save_final_markdown(
            base_url, session_obj.token, writer_session, str(final_md_path),
        )
        doc_stats_dict = _compute_document_stats(case, final_md_path, output_dir)
    except Exception as exc:
        final_artifact_error = str(exc)

    # 7) Fetch + analyze trace
    finished = time.time()
    trace_id = None
    trace_error = None
    trace_json = None
    if fetch_trace_after:
        # Langfuse 偶发返回 422/5xx；有界重试后再放弃。
        for _attempt in range(3):
            try:
                trace_json = fetch_trace(
                    chat_result.conversation_id,
                    workflow_session_id=writer_session.get("session_id"),
                    started_at=_epoch_to_iso(started_at),
                    finished_at=_epoch_to_iso(finished),
                    backend=os.environ.get("LAZYLLM_TRACE_CONSUME_BACKEND", "langfuse"),
                )
                break
            except Exception as exc:
                trace_error = f"trace fetch failed: {exc}"
                time.sleep(3)
        if trace_json is not None:
            trace_path.write_text(json.dumps(trace_json, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            trace_id = trace_json.get("id")

    analysis_dict = None
    if compute_stats and trace_json is not None:
        try:
            analysis = analyze(trace_json)
            stats = avg_traces(
                [trace_json], scenario=case.scenario,
                doc_stats=[doc_stats_dict] if doc_stats_dict else None,
            )
            analysis_dict = {
                "analysis": analysis.to_dict(),
                "stats": stats,
                "checks": [c.to_dict() for c in run_checks(analysis, expected={})],
            }
        except Exception as exc:
            trace_error = (trace_error + "; stats: " + str(exc)) if trace_error else f"stats: {exc}"

    result = RunResult(
        conversation_id=chat_result.conversation_id,
        started_at=started_at,
        finished_at=finished,
        elapsed_s=round(finished - started_at, 3),
        finish_reason=chat_result.finish_reason,
        attachments=state["attachments"],
        writer_session_id=writer_session.get("session_id"),
        writer_status=writer_session.get("status"),
        approvals=state["approvals"],
        final_artifact=final_artifact,
        final_artifact_error=final_artifact_error,
        trace_id=trace_id,
        trace_path=str(trace_path) if trace_json is not None else None,
        analysis=analysis_dict,
        trace_error=trace_error,
    )
    run_path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return result


# --- internal helpers ------------------------------------------------------


def _send(case: Case, state: dict, base_url: str, token: str, recipe: str) -> ChatResult:
    """Send the initial chat, store conversation_id on state."""
    result = send_chat(
        base_url=base_url,
        token=token,
        prompt_text=case.prompt_text,
        recipe=recipe,
        attachment_paths=state["attachments"],
        on_conversation_id=lambda cid: state.update(conversation_id=cid),
    )
    state["conversation_id"] = result.conversation_id
    return result


def _compute_document_stats(case: Case, final_md_path: Path,
                            output_dir: Path) -> dict | None:
    """计算与数据表“生成/修改文章字数”对齐的文档度量，并落盘 document_stats.json。

    写作类只出“生成字数”（最终可见字符）；修订类额外出“修改字数”
    （相对原文的变更字符数，原文取附件或飞书基线版本，best-effort）。
    """
    if not final_md_path.is_file():
        return None
    try:
        final_md = final_md_path.read_text(encoding="utf-8")
        original_md = None
        if str(case.extras.get("constraints") or "") == "revise":
            if case.has_attachment and case.attachment_path and case.attachment_path.is_file():
                original_md = case.attachment_path.read_text(encoding="utf-8")
            elif case.has_feishu_reference:
                try:
                    from shared.feishu import fetch_feishu_document
                    baseline = int(case.extras.get("feishu_baseline_revision_id") or -1)
                    original_md = fetch_feishu_document(
                        str(case.extras["feishu_reference"]),
                        revision_id=baseline,
                    ).content
                except Exception:
                    original_md = None
        stats = document_stats(final_md, original_md)
        path = output_dir / "document_stats.json"
        path.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return stats
    except Exception:
        return None


def _wait_and_approve(base_url, token, conversation_id, state, *, timeout) -> dict:
    """Wait for the writer session to reach a terminal state.

    ``waiting`` is transient and auto-advancing in the current runtime, so no
    explicit "继续" approval is sent.
    """
    outcome = wait_for_writer_completion(
        base_url, token, conversation_id,
        timeout_s=timeout,
        session_start_grace_s=SESSION_START_GRACE_S,
    )
    return outcome.session or {}


def _write_failure(run_path, started_at, finished, state, exc, finish_reason) -> RunResult:
    result = RunResult(
        conversation_id=state.get("conversation_id") or "",
        started_at=started_at,
        finished_at=finished,
        elapsed_s=round(finished - started_at, 3),
        finish_reason=finish_reason or "UNKNOWN",
        attachments=state.get("attachments") or [],
        writer_session_id=(state.get("session") or {}).get("session_id", ""),
        writer_status="harness_failed",
        approvals=state.get("approvals", 0),
        final_artifact=None,
        final_artifact_error=None,
        trace_id=None,
        trace_path=None,
        analysis=None,
        trace_error=f"{type(exc).__name__}: {exc}",
    )
    run_path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return result


def _epoch_to_iso(value: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", required=True,
                        help="tests/e2e/writer-test/cases directory")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--case", type=int, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", required=True,
                        help="per-case directory; run_N.json + trace.json + final.md")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--recipe", choices=["text_intent", "explicit_mention"],
                        default="text_intent")
    parser.add_argument("--no-trace", action="store_true",
                        help="skip trace fetch (use in CI when trace backend is down)")
    parser.add_argument("--no-stats", action="store_true",
                        help="skip perf stats computation (only emit run_N.json + final.md)")
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    if re.fullmatch(r"[A-Z]\d{2}", args.scenario.upper()):
        # P0X: performance scenarios from writer_perf_cases.yaml.
        case = load_perf_case(args.scenario, args.case)
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
        fetch_trace_after=not args.no_trace,
        compute_stats=not args.no_stats,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.writer_status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
