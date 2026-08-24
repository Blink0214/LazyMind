"""Observe one Writer run: trace fetch + live SSE capture.

This is the canonical trace-fetch implementation shared by the two test
suites plus the task SSE capture used by func ``--no-ui`` mode:

* ``fetch_trace`` / ``find_langfuse_trace`` / ``find_local_trace`` — Langfuse
  or local OTel JSONL/zip trace resolution.
* ``collect`` / ``StreamRecord`` / ``TaskCapture`` — artifact-stream SSE
  capture (start/delta/end, slots, content types, abort).

Local tracing writes one OTel span per JSONL row (also readable from
``.zip`` archives).  Rows are normalized to the small Langfuse-like shape
consumed by ``compute_stats.py`` / ``analyze_common.py`` so route checks and
optional performance analysis use the same code path for both backends.

Merged from the former trace.py / stream_capture.py.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_LANGFUSE_BASE = "http://localhost:3000"
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Config / env
# ---------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def config_from_env(env_file: str | None) -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    path = Path(env_file or os.environ.get("WRITER_ENV_FILE", root / ".env"))
    values = load_env(path)
    # Process environment wins, as it does for docker compose.
    values.update({k: v for k, v in os.environ.items() if k.startswith(("LAZYLLM_", "LANGFUSE_"))})
    return values


# ---------------------------------------------------------------------------
# Langfuse backend
# ---------------------------------------------------------------------------


def langfuse_get(url: str, public_key: str, secret_key: str, timeout: int = 30):
    raw = f"{public_key}:{secret_key}"
    auth = "Basic " + base64.b64encode(raw.encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _list_langfuse_traces(base_url: str, public_key: str, secret_key: str) -> list[dict]:
    """分页拉取 trace，避免安静丢掉超过首页 100 条的长时运行。"""
    result, page = [], 1
    while True:
        query = urllib.parse.urlencode({"limit": 100, "page": page})
        data = langfuse_get(f"{base_url.rstrip('/')}/api/public/traces?{query}", public_key, secret_key)
        rows = data.get("data") or []
        result.extend(rows)
        meta = data.get("meta") or {}
        total_pages = int(meta.get("totalPages") or meta.get("total_pages") or 1)
        if page >= total_pages or not rows:
            return result
        page += 1


def _trace_matches(t: dict, conversation_id: str, workflow_session_id: str | None) -> bool:
    session_id = str(t.get("sessionId") or "")
    if session_id.startswith(conversation_id):
        return True
    return bool(workflow_session_id and session_id.startswith(workflow_session_id))


def _stable_signature(traces: list[dict]) -> tuple:
    return tuple(sorted(
        (str(t.get("id")), tuple(sorted(str(o.get("id")) for o in t.get("observations") or [])))
        for t in traces
    ))


def _mentions_prompt(trace: dict, prompt_text: str | None) -> bool:
    """仅纳入能用本次原始请求明确关联的无 sessionId trace。"""
    if not prompt_text:
        return False
    marker = " ".join(prompt_text.split())[:160]
    haystack = " ".join(json.dumps(trace, ensure_ascii=False, default=str).split())
    return bool(marker and marker in haystack)


def find_langfuse_trace(conversation_id: str, base_url: str, public_key: str,
                        secret_key: str, max_wait: int, workflow_session_id: str | None = None,
                        prompt_text: str | None = None, after: float | None = None,
                        before: float | None = None):
    deadline = time.time() + max_wait
    previous_signature = None
    stable_polls = 0
    while time.time() < deadline:
        rows = _list_langfuse_traces(base_url, public_key, secret_key)
        seen = {}
        for t in rows:
            if _trace_matches(t, conversation_id, workflow_session_id):
                seen.setdefault(t["id"], t)
        if seen:
            matches = sorted(seen.values(), key=lambda t: str(t.get("timestamp") or ""))
            session_traces = [
                langfuse_get(f"{base_url.rstrip('/')}/api/public/traces/{m['id']}",
                             public_key, secret_key, timeout=60)
                for m in matches
            ]
            explicit_extras = []
            for candidate in rows:
                if candidate.get("id") in seen or candidate.get("sessionId"):
                    continue
                timestamp = _epoch_iso(candidate.get("timestamp"))
                if after is not None and timestamp is not None and timestamp < after:
                    continue
                if before is not None and timestamp is not None and timestamp > before:
                    continue
                if _mentions_prompt(candidate, prompt_text):
                    explicit_extras.append(langfuse_get(
                        f"{base_url.rstrip('/')}/api/public/traces/{candidate['id']}",
                        public_key, secret_key, timeout=60,
                    ))
            session_traces.extend(explicit_extras)
            signature = _stable_signature(session_traces)
            # Langfuse 导出异步刷新：至少连续三次内容稳定后再生成报告。
            stable_polls = stable_polls + 1 if signature == previous_signature else 1
            if stable_polls >= 3:
                return _merge_langfuse_traces(session_traces, conversation_id)
            previous_signature = signature
        time.sleep(3)
    raise RuntimeError(f"{max_wait}s 内未找到 {conversation_id} 的 Langfuse trace")


def _obs_span(o: dict):
    start = _epoch_iso(o.get("startTime"))
    end = _epoch_iso(o.get("endTime"))
    if start is not None and end is None and o.get("latency") is not None:
        end = start + float(o.get("latency") or 0)
    return start, end


def _merge_langfuse_traces(traces: list[dict], conversation_id: str) -> dict:
    """合并同一会话的多条 Langfuse trace（一次 Writer 运行可能拆成多条 ReactAgent trace）。"""
    if not traces:
        raise RuntimeError("Langfuse 未返回任何 trace")
    if len(traces) == 1:
        return traces[0]
    observations, seen = [], set()
    starts, ends, session_ids, sources = [], [], set(), []
    for trace in traces:
        sources.append(str(trace.get("id")))
        sid = trace.get("sessionId")
        if sid:
            session_ids.add(str(sid))
        for o in trace.get("observations") or []:
            oid = o.get("id")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            observations.append(o)
            start = _epoch_iso(o.get("startTime"))
            end = _epoch_iso(o.get("endTime"))
            if start is not None:
                starts.append(start)
            if end is not None:
                ends.append(end)
    observations.sort(key=lambda item: str(item.get("startTime") or ""))
    timestamp = (
        datetime.fromtimestamp(min(starts), timezone.utc).isoformat()
        if starts else str(traces[0].get("timestamp") or "")
    )
    latency = (
        max(0.0, max(ends) - min(starts)) if starts and ends
        else sum(float(t.get("latency") or 0) for t in traces)
    )
    return {
        "id": traces[0].get("id", ""),
        "name": "writer_conversation",
        "sessionId": next(iter(session_ids), conversation_id),
        "timestamp": timestamp,
        "latency": latency,
        "observations": observations,
        "metadata": {"backend": "langfuse", "sources": sources, "merged_traces": len(traces)},
    }


# ---------------------------------------------------------------------------
# Local (OTel JSONL / zip) backend
# ---------------------------------------------------------------------------


def _trace_id(row: dict) -> str:
    context = row.get("context") or {}
    value = str(context.get("trace_id") or "")
    value = value.removeprefix("0x")
    return value if TRACE_ID_RE.fullmatch(value) else ""


def _span_id(value) -> str:
    value = str(value or "").removeprefix("0x")
    return value


def _epoch_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _local_storage_dir(config: dict[str, str], explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = config.get("LAZYLLM_TRACE_LOCAL_STORAGE_DIR", "")
    candidate = Path(configured).expanduser() if configured else Path("data/traces")
    if candidate.exists():
        return candidate.resolve()
    # Docker's default path is mounted at data/traces in this repository.
    root = Path(__file__).resolve().parents[3]
    if str(candidate) == "/var/lib/lazymind/traces":
        return (root / "data/traces").resolve()
    return candidate.resolve()


def _read_local_files(storage_dir: Path):
    for path in sorted(storage_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"无法读取 local trace {path}: {exc}") from exc
        yield path.name, rows
    for archive in sorted(storage_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with zipfile.ZipFile(archive) as zf:
                for name in sorted(zf.namelist(), reverse=True):
                    if not name.endswith(".jsonl"):
                        continue
                    rows = [json.loads(line) for line in zf.read(name).decode("utf-8").splitlines() if line.strip()]
                    yield f"{archive.name}:{name}", rows
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            raise RuntimeError(f"无法读取 local trace archive {archive}: {exc}") from exc


def _local_matches(rows: list[dict], conversation_id: str, workflow_session_id: str | None = None) -> bool:
    identities = [conversation_id] + ([workflow_session_id] if workflow_session_id else [])
    for row in rows:
        attrs = row.get("attributes") or {}
        if any(str(attrs.get("session.id") or "").startswith(identity) for identity in identities):
            return True
        # Older local spans may only retain trace metadata or IO payloads.
        for key, value in attrs.items():
            if key.startswith("lazyllm.trace.metadata.") and any(identity in str(value) for identity in identities):
                return True
            if key in {"lazyllm.io.input", "lazyllm.io.output"} and any(identity in str(value) for identity in identities):
                return True
    return False


def _normalize_local(source: str, rows: list[dict], conversation_id: str) -> dict:
    if not rows:
        raise RuntimeError(f"local trace {source} 为空")
    trace_id = _trace_id(rows[0])
    if not trace_id or any(_trace_id(row) != trace_id for row in rows):
        raise RuntimeError(f"local trace {source} 包含无效或不一致的 trace_id")
    observations = []
    starts, ends = [], []
    session_id = conversation_id
    trace_name = ""
    for row in rows:
        attrs = row.get("attributes") or {}
        span_id = _span_id((row.get("context") or {}).get("span_id"))
        if not span_id:
            continue
        start = _epoch_iso(row.get("start_time"))
        end = _epoch_iso(row.get("end_time"))
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
        if attrs.get("session.id"):
            session_id = str(attrs["session.id"])
        if attrs.get("lazyllm.trace.name"):
            trace_name = str(attrs["lazyllm.trace.name"])
        semantic = str(attrs.get("lazyllm.semantic_type") or "").lower()
        name = str(row.get("name") or attrs.get("lazyllm.entity.name") or "")
        usage = {}
        if attrs.get("gen_ai.usage.input_tokens") is not None:
            usage["input"] = int(attrs["gen_ai.usage.input_tokens"])
        if attrs.get("gen_ai.usage.output_tokens") is not None:
            usage["output"] = int(attrs["gen_ai.usage.output_tokens"])
        observations.append({
            "id": span_id,
            "name": name,
            "type": "GENERATION" if semantic == "llm" or name == "llm" else "SPAN",
            "parentObservationId": _span_id(row.get("parent_id")) or None,
            "startTime": row.get("start_time") or "",
            "endTime": row.get("end_time") or "",
            "latency": max(0.0, (end - start)) if start is not None and end is not None else 0.0,
            "level": "ERROR" if attrs.get("lazyllm.status") == "error" else "DEFAULT",
            "usageDetails": usage,
            "metadata": attrs,
        })
    if not observations:
        raise RuntimeError(f"local trace {source} 没有有效 span")
    return {
        "id": trace_id,
        "name": trace_name or trace_id,
        "sessionId": session_id,
        "timestamp": datetime.fromtimestamp(min(starts), timezone.utc).isoformat() if starts else "",
        "latency": max(0.0, max(ends) - min(starts)) if starts and ends else 0.0,
        "observations": observations,
        "metadata": {"backend": "local", "source": source},
    }


def _merge_local_traces(traces: list[dict], conversation_id: str) -> dict:
    if len(traces) == 1:
        return traces[0]
    observations = []
    starts, ends = [], []
    sources = []
    for trace in traces:
        observations.extend(trace.get("observations", []))
        if trace.get("timestamp"):
            parsed = _epoch_iso(trace["timestamp"])
            if parsed is not None:
                starts.append(parsed)
        latency = float(trace.get("latency") or 0)
        if trace.get("timestamp") and latency:
            parsed = _epoch_iso(trace["timestamp"])
            if parsed is not None:
                ends.append(parsed + latency)
        source = (trace.get("metadata") or {}).get("source")
        if source:
            sources.append(source)
    observations.sort(key=lambda item: item.get("startTime") or "")
    return {
        "id": traces[0].get("id", ""),
        "name": "writer_conversation",
        "sessionId": conversation_id,
        "timestamp": min((t.get("timestamp", "") for t in traces), default=""),
        "latency": max(0.0, max(ends) - min(starts)) if starts and ends else sum(
            float(t.get("latency") or 0) for t in traces
        ),
        "observations": observations,
        "metadata": {"backend": "local", "sources": sources, "merged_traces": len(traces)},
    }


def find_local_trace(conversation_id: str, storage_dir: Path, max_wait: int,
                     after: float | None = None, workflow_session_id: str | None = None):
    deadline = time.time() + max_wait
    previous_signature = None
    stable_polls = 0
    while time.time() < deadline:
        matches = [(source, rows) for source, rows in _read_local_files(storage_dir)
                   if _local_matches(rows, conversation_id, workflow_session_id)]
        if matches:
            signature = tuple(sorted((source, len(rows)) for source, rows in matches))
            stable_polls = stable_polls + 1 if signature == previous_signature else 1
            previous_signature = signature
            if stable_polls >= 3:
                return _merge_local_traces(
                    [_normalize_local(source, rows, conversation_id) for source, rows in matches],
                    conversation_id,
                )
        elif after is not None:
            candidates = []
            for source, rows in _read_local_files(storage_dir):
                source_path = source.split(":", 1)[0]
                if source_path.endswith(".jsonl"):
                    path = storage_dir / source_path
                    if path.stat().st_mtime >= after:
                        candidates.append((source, rows))
            if len(candidates) == 1:
                return _normalize_local(*candidates[0], conversation_id)
            if len(candidates) > 1:
                names = ", ".join(source for source, _ in candidates[:8])
                raise RuntimeError(
                    f"local trace 未写入 session.id，{conversation_id} 对应多个候选文件: {names}"
                )
        time.sleep(1)
    raise RuntimeError(f"{max_wait}s 内未找到 {conversation_id} 的 local trace（目录：{storage_dir}）")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fetch_trace(conversation_id: str, *,
                workflow_session_id: str | None = None,
                started_at: str | None = None,
                finished_at: str | None = None,
                backend: str = "auto",
                trace_dir: str | None = None,
                env_file: str | None = None,
                prompt_text: str | None = None,
                timeout: int = 60) -> dict:
    """Resolve one Writer conversation to a (possibly merged) trace dict.

    ``backend`` accepts ``"auto"`` (default, resolved via env), ``"langfuse"``
    or ``"local"``.  ``started_at`` / ``finished_at`` are ISO-8601 timestamps
    used to filter unrelated traces.  Raises ``RuntimeError`` when no stable
    trace appears within ``timeout`` seconds.
    """
    config = config_from_env(env_file)
    resolved = backend.strip().lower()
    if resolved == "auto":
        resolved = (
            config.get("LAZYLLM_TRACE_CONSUME_BACKEND")
            or config.get("LAZYLLM_TRACE_BACKEND")
            or "langfuse"
        ).strip().lower()

    after = _epoch_iso(started_at) if started_at else None
    before = _epoch_iso(finished_at) if finished_at else None

    if resolved == "local":
        return find_local_trace(
            conversation_id,
            _local_storage_dir(config, trace_dir),
            timeout,
            after,
            workflow_session_id,
        )

    base = (
        config.get("LANGFUSE_BASE_URL")
        or config.get("LANGFUSE_HOST")
        or DEFAULT_LANGFUSE_BASE
    )
    public = config.get("LANGFUSE_PUBLIC_KEY", "")
    secret = config.get("LANGFUSE_SECRET_KEY", "")
    if not public or not secret:
        raise RuntimeError("Langfuse 模式缺少 LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY")
    return find_langfuse_trace(
        conversation_id, base, public, secret, timeout,
        workflow_session_id, prompt_text, after, before,
    )


# ---------------------------------------------------------------------------
# CLI (shared by the benchmark wrapper and ad-hoc debugging)
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="按 conversation_id 获取 Writer trace")
    parser.add_argument("conversation_id")
    parser.add_argument("--out", default=None)
    parser.add_argument("--backend", choices=["auto", "local", "langfuse"], default="auto")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--trace-dir", default=None, help="local trace 宿主机目录")
    parser.add_argument("--base-url", default=None, help="Langfuse 地址")
    parser.add_argument("--max-wait", type=int, default=60)
    parser.add_argument("--workflow-session-id", default=None,
                        help="Writer workflow session id，用于关联 SubAgent/独立 trace")
    parser.add_argument("--prompt-file", default=None,
                        help="原始提示词，仅用于安全关联无 sessionId trace")
    parser.add_argument("--after", type=float, default=None,
                        help="仅在 local 缺少 session.id 时使用，请传请求开始时间戳")
    parser.add_argument("--before", type=float, default=None,
                        help="关联 Langfuse 无 sessionId trace 时使用，请传本次运行结束时间戳")
    args = parser.parse_args()

    prompt_text = None
    if args.prompt_file:
        prompt_text = Path(args.prompt_file).read_text(encoding="utf-8")
    started_at = (
        datetime.fromtimestamp(args.after, tz=timezone.utc).isoformat()
        if args.after is not None else None
    )
    finished_at = (
        datetime.fromtimestamp(args.before, tz=timezone.utc).isoformat()
        if args.before is not None else None
    )
    try:
        trace = fetch_trace(
            args.conversation_id,
            workflow_session_id=args.workflow_session_id,
            started_at=started_at,
            finished_at=finished_at,
            backend=args.backend,
            trace_dir=args.trace_dir,
            env_file=args.env_file,
            prompt_text=prompt_text,
            timeout=args.max_wait,
        )
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    output = json.dumps(trace, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"已保存到 {args.out}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Capture artifact-stream events emitted on the Writer task SSE channel.

The Writer backend pushes ``artifact_stream_start`` / ``_delta`` / ``_end``
events over a Server-Sent Events channel rooted at ``/api/core/tasks/{tid}:stream``.
``shared.observability`` subscribes to that channel and reconstructs the
per-stream ordering plus the concatenated chunk text.  Functional-mode checks
later compare the last complete ``draft_document`` stream's text against the
final markdown artifact, which is the strongest contract that the streaming
layer and the artifact store deliver identical bytes to the panel.
"""

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class StreamRecord:
    stream_id: str
    events: list[str] = field(default_factory=list)
    text: str = ""
    slots: set = field(default_factory=set)
    content_types: set = field(default_factory=set)
    aborted: bool = False

    @property
    def is_complete(self) -> bool:
        return (self.events
                and self.events[0] == "artifact_stream_start"
                and self.events[-1] == "artifact_stream_end"
                and "artifact_stream_delta" in self.events
                and not self.aborted)


@dataclass
class TaskCapture:
    task_id: str
    streams: dict[str, StreamRecord] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    terminal: dict | None = None
    content_type: str = ""


def collect(base_url: str, token: str, task_id: str, *,
            timeout_s: int = 1800) -> TaskCapture:
    """Open the task SSE stream and capture every event until the task terminates.

    The Writer backend closes the SSE connection when the task reaches
    ``completed`` / ``failed`` / ``stopped``; ``collect`` returns once that
    happens or after ``timeout_s`` elapses (in which case ``terminal`` is None).
    """
    capture = TaskCapture(task_id=task_id)
    req = urllib.request.Request(
        f"{base_url}/api/core/tasks/{task_id}:stream",
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    deadline = time.monotonic() + timeout_s
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        capture.content_type = resp.headers.get_content_type()
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            _consume_event(capture, event)
            etype = str(event.get("type") or "")
            if etype in {"task_completed", "task_failed", "task_stopped"}:
                capture.terminal = event
                return capture
            if etype == "task_terminated":
                capture.terminal = event
                return capture
            if time.monotonic() > deadline:
                return capture
    return capture


def _consume_event(capture: TaskCapture, event: dict) -> None:
    etype = str(event.get("type") or "")
    if etype == "_connection":
        return
    if etype == "artifact_stream_start":
        sid = str(event.get("stream_id") or "")
        if not sid:
            return
        rec = capture.streams.get(sid) or StreamRecord(stream_id=sid)
        rec.events.append("artifact_stream_start")
        if event.get("aborted"):
            rec.aborted = True
        slots = event.get("slots") or []
        if isinstance(slots, list):
            rec.slots.update(str(s) for s in slots)
        ctypes = event.get("content_types") or []
        if isinstance(ctypes, list):
            rec.content_types.update(str(c) for c in ctypes)
        capture.streams[sid] = rec
        return
    if etype in ("artifact_stream_delta", "artifact_stream"):
        # 兼容新旧两种事件名：旧协议 artifact_stream_delta，当前后端
        # artifact_stream；统一归一到 delta 事件形态。
        sid = str(event.get("stream_id") or "")
        if not sid:
            return
        rec = capture.streams.get(sid) or StreamRecord(stream_id=sid)
        rec.events.append("artifact_stream_delta")
        chunk = event.get("chunk")
        if chunk is not None:
            rec.text += str(chunk)
        capture.streams[sid] = rec
        return
    if etype in ("artifact_stream_end", "artifact_stream_abort"):
        sid = str(event.get("stream_id") or "")
        if not sid:
            return
        rec = capture.streams.get(sid) or StreamRecord(stream_id=sid)
        rec.events.append("artifact_stream_end")
        if etype == "artifact_stream_abort" or event.get("aborted"):
            rec.aborted = True
        capture.streams[sid] = rec
        return
    if etype == "artifact_update":
        artifact = event.get("artifact")
        if artifact:
            capture.artifacts.append(artifact)
        return
    if etype.startswith("tool_call"):
        capture.tool_calls.append(event)
        return
    if etype.startswith("tool_result"):
        capture.results.append(event)
        return


def select_complete_streams(capture: TaskCapture, *, slot: str | None = None,
                            content_type: str | None = None) -> list[StreamRecord]:
    """Yield complete streams, optionally filtered by slot/content_type."""
    out: list[StreamRecord] = []
    for rec in capture.streams.values():
        if not rec.is_complete:
            continue
        if slot is not None and slot not in rec.slots:
            continue
        if content_type is not None and content_type not in rec.content_types:
            continue
        out.append(rec)
    return out


def concatenated_text(records: Iterable[StreamRecord]) -> str:
    """Sum the chunk text from a list of stream records (chronology-preserving)."""
    return "".join(r.text for r in records)
