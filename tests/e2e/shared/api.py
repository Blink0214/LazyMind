"""Core API orchestration for Writer test runs.

One module for the backend interaction every runner needs:

* ``Session`` / ``decode_jwt_user_id`` — authservice login + JWT user_id.
* ``send_chat`` — stream a conversation turn (text_intent / explicit_mention).
* ``upload_attachment`` — chunked temp/uploads upload.
* ``wait_for_writer_completion`` / ``save_final_markdown`` /
  ``fetch_latest_session`` / ``load_slot`` — session polling and artifact reads.

Merged from the former auth.py / chat.py / upload.py / wait.py.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from dataclasses import dataclass


USERNAME = os.environ.get("LAZYMIND_E2E_USERNAME") or "admin"
PASSWORD = os.environ.get("LAZYMIND_E2E_PASSWORD") or "admin"


@dataclass
class Session:
    token: str
    user_id: str

    @classmethod
    def login(cls, base_url: str, *, username=None, password=None, timeout: int = 15) -> "Session":
        token = _login(base_url, username or USERNAME, password or PASSWORD, timeout)
        return cls(token=token, user_id=decode_jwt_user_id(token))


def _login(base_url: str, username: str, password: str, timeout: int) -> str:
    url = f"{base_url}/api/authservice/auth/login"
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["data"]["access_token"]


def decode_jwt_user_id(token: str) -> str:
    """Extract ``user_id`` (or ``sub``) from an unstamped JWT.

    Mirrors the parser used by ``writer-benchmark/scripts/full_run.sh`` so downstream callers (e.g. container-side feishu reset) see the same id.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return str(claims.get("user_id") or claims.get("sub") or "")



"""Conversations chat endpoint used by both perf and func runners.

A single ``send_chat`` posts to ``/api/core/conversations:chat`` with one of two payload recipes.  The recipes are the dual signaling channel Writer supports:

- ``text_intent`` (default): send a workflow-mode payload whose mention of "Writer / 工作流" is intended to drive the router to pick writer-plugin via its workflow catalog. No explicit mention is attached.
- ``explicit_mention``: send a workflow-mode payload with ``mentions=[{"type": "workflow", "resource_id": "builtin:writer-workflow"}]`` so the chat composer unmistakably routes to the writer workflow.

Both flavors were exercised by the existing test suites (writer-benchmark historically used ``text_intent``, writer-e2e Playwright UI used ``explicit_mention``). 
A clean refactor of the test stack has to keep both available so we can compare the two paths when investigating routing bugs.

Continuation / approval sends reuse the same send_chat entry point with a ``workflow_context`` block, mirroring what a Playwright click on the panel's "继续" button does through the UI.
"""

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


RECIPES = ("text_intent", "explicit_mention")


@dataclass
class ChatResult:
    conversation_id: str
    elapsed_s: float
    finish_reason: str


def send_chat(base_url: str, token: str, prompt_text: str, *,
              recipe: str = "text_intent",
              attachment_paths=(),
              conversation_id: str = "",
              workflow_context: dict | None = None,
              on_conversation_id: Callable[[str], None] | None = None,
              timeout: int = 3600) -> ChatResult:
    if recipe not in RECIPES:
        raise ValueError(f"recipe must be one of {RECIPES}, got {recipe!r}")

    inputs = [{"input_type": "text", "text": prompt_text}]
    inputs += [{"input_type": "file", "uri": p} for p in attachment_paths]
    payload: dict = {
        "input": inputs,
        "stream": True,
        "thinking_depth": "high",
        "mode": "auto",
        "create_time": datetime.now(timezone.utc).isoformat(),
        "environment_context": {
            "locale": "zh-CN",
            "time": {"now": datetime.now(timezone.utc).isoformat(),
                     "timezone": "Asia/Shanghai"},
        },
    }

    if conversation_id:
        payload["conversation_id"] = conversation_id
    else:
        payload["initial_workflow_settings"] = {
                "enable_workflow": True,
                "enable_subagent": True,
                "workflow_mode": "dynamic",
            }
        if recipe == "explicit_mention":
            payload["mentions"] = [{
                "type": "workflow",
                "resource_id": "builtin:writer-workflow",
            }]

    if workflow_context:
        payload["workflow_context"] = workflow_context

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/api/core/conversations:chat", data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    return _consume_sse(req, timeout, on_conversation_id)


# --- internal helpers ------------------------------------------------------


def _consume_sse(req, timeout, on_conversation_id):
    import time
    conversation_id = ""
    finish_reason = ""
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            result = evt.get("result", evt)
            if not isinstance(result, dict):
                continue
            cid = result.get("conversation_id")
            if cid and not conversation_id:
                conversation_id = str(cid)
                if on_conversation_id:
                    on_conversation_id(conversation_id)
            fr = str(result.get("finish_reason") or "")
            if fr and fr != "FINISH_REASON_UNSPECIFIED":
                finish_reason = fr
            if "FINISH_REASON_STOP" in finish_reason:
                break
    return ChatResult(
        conversation_id=conversation_id,
        elapsed_s=time.time() - started,
        finish_reason=finish_reason,
    )


"""Chunked attachment upload using Core's temp/uploads API.

Mirrors the three-step init / part / complete flow used by the Writer frontend so callers can upload a single local file 
and reuse it as ``file`` input in a later chat message, without bypassing the real product code path.

Default chunk size matches ``run_case.CHUNK_SIZE`` (5 MB) so dual-runs of the legacy ``writer-benchmark/scripts/run_case.py`` 
and the new ``shared`` modules hit byte-identical upload paths.
"""

import json
import mimetypes
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CHUNK_SIZE = 5 * 1024 * 1024   # 5 MB; matches the frontend's part_size cap.


@dataclass
class UploadedFile:
    stored_path: str   # value to use as `{"input_type": "file", "uri": stored_path}`


def upload_attachment(base_url: str, token: str, file_path, *,
                      part_size: int = DEFAULT_CHUNK_SIZE,
                      timeout: int = 120) -> UploadedFile:
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"attachment not found: {file_path}")
    headers = {"Authorization": f"Bearer {token}"}

    # 1) init
    init_body = json.dumps({
        "filename": path.name,
        "file_size": path.stat().st_size,
        "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "part_size": part_size,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/api/core/temp/uploads:initUpload", data=init_body,
        headers={**headers, "Content-Type": "application/json"}, method="POST",
    )
    init = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    upload_id = init["upload_id"]
    negotiated_part_size = init.get("part_size") or part_size

    # 2) parts
    with path.open("rb") as f:
        part_number = 1
        while True:
            chunk = f.read(negotiated_part_size)
            if not chunk:
                break
            req = urllib.request.Request(
                f"{base_url}/api/core/temp/uploads/{upload_id}/parts/{part_number}",
                data=chunk,
                headers={**headers, "Content-Type": "application/octet-stream"},
                method="PUT",
            )
            urllib.request.urlopen(req, timeout=timeout).read()
            part_number += 1

    # 3) complete
    req = urllib.request.Request(
        f"{base_url}/api/core/temp/uploads/{upload_id}:complete",
        data=b'{"auto_start":false}',
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    completed = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    return UploadedFile(stored_path=completed["stored_path"])
"""Wait for a Writer session to reach a terminal state, then save the final artifact.

The waiter polls ``/api/core/conversations/{cid}/workflow-sessions:latest``
every 2 seconds (matching the cadence used by ``writer-benchmark`` and
``writer-e2e``).  It returns once the session hits a terminal status
(``completed``, ``failed`` or ``stopped``).  ``waiting`` is a transient
workflow state in the current runtime — steps advance automatically — so the
waiter keeps polling instead of asking the caller to send a "继续" approval.

A second responsibility is saving the final Markdown so perf analyzers can
diff it against a baseline and so func analyzers can run SSE-reconstruction
checks.  Both modes need the same artifact path so the saved file format is
identical to what users currently download from the panel.
"""

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


TERMINAL_STATUSES = {"completed", "failed", "stopped"}
DEFAULT_SESSION_START_GRACE_S = 20  # first session must appear within this window.
DEFAULT_POLL_S = 2


@dataclass
class WaitOutcome:
    conversation_id: str
    session: dict           # the latest writer_session payload (or {} when None)
    timed_out: bool = False


def wait_for_writer_completion(base_url: str, token: str,
                              conversation_id: str, *,
                              timeout_s: int = 1800,
                              session_start_grace_s: int = DEFAULT_SESSION_START_GRACE_S,
                              poll_s: float = DEFAULT_POLL_S,
                              on_session=None) -> WaitOutcome:
    """Poll the workflow-sessions endpoint until a terminal status is reached.

    ``waiting`` is treated as transient (auto-advancing) and is not returned;
    the caller must not send an explicit "继续" approval.

    A timeout used to return ``None`` for the failure case where the first
    Writer session never appears within ``session_start_grace_s``.  In the new
    shape we still resolve ``None`` to ``{}`` and raise only on a true
    timeout (no terminal status reached within ``timeout_s``).
    """
    started = time.monotonic()
    deadline = started + timeout_s
    grace_deadline = started + session_start_grace_s
    session: dict = {}
    last_status = "not_started"

    while time.monotonic() < deadline:
        try:
            session = _latest_session(base_url, token, conversation_id) or {}
        except Exception:
            session = {}

        if session:
            if on_session:
                on_session(session)
            last_status = session.get("status") or "not_started"
            if last_status in TERMINAL_STATUSES:
                return WaitOutcome(
                    conversation_id=conversation_id,
                    session=session,
                )
        elif time.monotonic() >= grace_deadline:
            raise RuntimeError(
                f"Writer workflow session did not appear within {session_start_grace_s}s "
                f"after the first SSE turn; check trigger_writer_workflow / tool routing"
            )

        time.sleep(poll_s)

    raise TimeoutError(
        f"Writer workflow did not reach a terminal state within {timeout_s}s; "
        f"last_status={last_status}"
    )


def save_final_markdown(base_url: str, token: str, session: dict,
                        output_path, *, selected_final_slots_fn=None) -> dict:
    """Persist the panel-grade Markdown for the given session's selected slots.

    ``selected_final_slots_fn`` defaults to ``document_metrics.selected_final_slots``
    when available; the optional parameter exists so this module does not import
    the perf analyzer just to call it.
    """
    if selected_final_slots_fn is None:
        from shared.document_metrics import selected_final_slots
        selected_final_slots_fn = selected_final_slots

    errors = []
    for slot in selected_final_slots_fn(session):
        raw = slot.get("artifact_value")
        try:
            markdown = _to_markdown(base_url, token, raw)
            if markdown and markdown.strip():
                path = Path(output_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
                return {"path": str(path),
                        "slot": slot.get("slot"),
                        "revision": slot.get("revision")}
        except Exception as exc:
            errors.append(f"{slot.get('slot')}: {exc}")
    raise RuntimeError(f"no Markdown saved across {len(errors)} slots: {errors}")


def fetch_latest_session(base_url: str, token: str, conversation_id: str) -> dict:
    """Return the latest workflow session for a conversation (or raise)."""
    session = _latest_session(base_url, token, conversation_id) or {}
    if not session:
        raise RuntimeError(f"no workflow session for conversation: {conversation_id}")
    return session


def load_slot(base_url: str, token: str, session: dict, slot_id: str):
    """Load the latest selected artifact of a writer slot (writer-e2e port).

    Mirrors ``writer-e2e/writer_e2e.py::load_slot``: pick the newest selected
    revision of ``slot_id`` from the session, resolve its artifact through
    Core's signed static-files route and return ``(parsed, content_type)`` —
    JSON when the payload parses, otherwise the raw text.
    """
    slot = None
    for item in session.get("slots") or []:
        if str(item.get("slot_id") or "") == slot_id and item.get("selected", True):
            slot = item
    if slot is None:
        raise RuntimeError(f"required slot is missing: {slot_id}")

    raw = slot.get("artifact_value")
    if raw is None:
        raise RuntimeError(f"slot has no artifact value: {slot_id}")
    if isinstance(raw, dict):
        # 新形态：内联 Markdown 产物（后端 writer_write_back_state.go 支持
        # schema=text/markdown + data 直接内联），直接返回文本而非包装 dict。
        if str(raw.get("schema") or "") == "text/markdown" \
                and isinstance(raw.get("data"), str):
            return raw["data"], "text/markdown"
        if "data" in raw:
            return raw, "application/json"

    text = None
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate.startswith(("{", "[")):
            text = raw
        else:
            text = _read_artifact(base_url, token, raw)
    else:
        text = _read_artifact(base_url, token, raw)
    if text is None:
        raise RuntimeError(f"failed to read artifact for slot: {slot_id}")
    try:
        return json.loads(text), "application/json"
    except json.JSONDecodeError:
        return text, "text/plain"


# --- internal helpers ------------------------------------------------------


def _latest_session(base_url, token, conversation_id) -> dict | None:
    req = urllib.request.Request(
        f"{base_url}/api/core/conversations/{conversation_id}/workflow-sessions:latest",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        response = json.loads(resp.read().decode())
    return (response.get("session")
            or response.get("data", {}).get("session"))


def _to_markdown(base_url, token, raw) -> str | None:
    from shared.document_metrics import inline_artifact_to_markdown
    markdown = inline_artifact_to_markdown(raw)
    if markdown is not None:
        return markdown
    loaded = _read_artifact(base_url, token, raw)
    return inline_artifact_to_markdown(loaded)


def _read_artifact(base_url, token, raw) -> str | None:
    """Resolve and read an artifact through Core's signed static-files route."""
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("url") or raw.get("path") or "").strip()
    if not source:
        return None
    if source.startswith("http://") or source.startswith("https://"):
        url = source
    else:
        signed = _post_json(base_url, token, "/api/core/static-files:sign",
                            {"paths": [source]})
        source = (signed.get("urls") or {}).get(source, source)
        url = f"{base_url}/api/core{source}" if source.startswith("/static-files/") else source
        if source.startswith("/api/core/"):
            url = f"{base_url}{source}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def _post_json(base_url, token, path, payload) -> dict:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}
