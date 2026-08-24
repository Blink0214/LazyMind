"""Feishu document baseline operations for Writer cases (host-side, lark-cli).

The unified stack restores Feishu baselines through the locally authenticated
``lark-cli``:

* ``fetch_revision`` / ``fetch_feishu_document`` — read the current revision
  (or a pinned revision) of a document.
* ``reset_feishu_document`` — compare the current body with the pinned
  baseline revision; only revert through history when they differ, then
  verify the restored body matches.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass


@dataclass
class FeishuDocument:
    revision_id: int
    content: str


def _lark_json(arguments: list[str], timeout: int) -> dict:
    """Run lark-cli and return its JSON envelope."""
    cli = shutil.which("lark-cli")
    if not cli:
        raise RuntimeError("lark-cli is required for Feishu scenarios")
    env = {**os.environ,
           "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
           "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}
    try:
        result = subprocess.run(
            [cli, *arguments], capture_output=True, text=True, check=False,
            timeout=max(1, timeout), env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("lark-cli timed out") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"lark-cli failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("lark-cli returned non-JSON output") from exc
    if envelope.get("ok") is not True:
        raise RuntimeError(f"lark-cli operation failed: {envelope}")
    return envelope


def fetch_revision(reference: str, *, timeout: int = 180) -> FeishuDocument:
    """Return the current revision id + Markdown body of a Feishu document."""
    return fetch_feishu_document(reference, timeout=timeout)


def fetch_feishu_document(reference: str, timeout: int = 180,
                          revision_id: int = -1) -> FeishuDocument:
    """Read a Feishu document (optionally at a fixed revision) via lark-cli."""
    arguments = [
        "docs", "+fetch", "--doc", reference,
        "--doc-format", "markdown",
        "--detail", "simple", "--format", "json", "--as", "user",
    ]
    if revision_id >= 0:
        arguments.extend(["--revision-id", str(revision_id)])
    envelope = _lark_json(arguments, timeout)
    document = ((envelope.get("data") or {}).get("document") or {})
    if not isinstance(document.get("revision_id"), int) or not isinstance(document.get("content"), str):
        raise RuntimeError("Feishu response has no revision/content")
    return FeishuDocument(revision_id=document["revision_id"],
                          content=document["content"])


def reset_feishu_document(reference: str, history_version_id: str,
                          baseline_revision_id: int,
                          timeout: int = 180) -> FeishuDocument:
    """Restore a disposable Feishu document to a baseline revision.

    Compare the current body with the pinned baseline; only revert through
    history when they differ, then verify the restored body matches.
    """
    baseline = fetch_feishu_document(reference, timeout, baseline_revision_id)
    current = fetch_feishu_document(reference, timeout)
    if current.content == baseline.content:
        return current

    deadline = time.monotonic() + max(1, timeout)
    task = (_lark_json([
        "docs", "+history-revert", "--doc", reference,
        "--history-version-id", history_version_id, "--wait-timeout-ms", "30000",
        "--format", "json", "--as", "user",
    ], min(timeout, 120)).get("data") or {})
    while task.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(2)
        task = (_lark_json([
            "docs", "+history-revert-status", "--doc", reference,
            "--task-id", str(task.get("task_id") or ""), "--format", "json",
            "--as", "user",
        ], 30).get("data") or {})
    if task.get("status") != "done":
        raise RuntimeError(f"Feishu reset failed: {task}")
    restored = fetch_feishu_document(reference, timeout)
    if restored.content != baseline.content:
        raise RuntimeError("Feishu content differs from baseline after reset")
    return restored
