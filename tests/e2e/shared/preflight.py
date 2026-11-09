"""Shared provider preflight checks for Writer E2E runs."""
from __future__ import annotations

from typing import Any

from shared.feishu import reset_feishu_document


def reset_provider_baseline(case: Any, *, timeout: int) -> int | None:
    """Reset a case's mutable provider document before sending the Writer request.

    Provider-backed cases are unsafe to execute from an unknown baseline.  A
    reset failure is therefore a harness failure, not a skippable warning.
    """
    if not case.has_feishu_reference:
        return None

    extras = case.extras or {}
    scenario = str(getattr(case, "scenario", "unknown"))
    try:
        reference = str(extras["feishu_reference"])
        history_version_id = str(extras["feishu_history_version_id"])
        baseline_revision_id = int(extras["feishu_baseline_revision_id"])
        return reset_feishu_document(
            reference,
            history_version_id,
            baseline_revision_id,
            timeout=min(timeout, 180),
        ).revision_id
    except Exception as exc:
        raise RuntimeError(
            f"failed to reset provider baseline for scenario {scenario}; "
            "aborting before the Writer request is sent"
        ) from exc
