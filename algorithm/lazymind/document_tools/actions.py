"""Registry for provider-neutral Workflow artifact actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

DocumentActionPhase = Literal["preview", "apply", "execute"]
DocumentAction = Callable[..., Any]

_DOCUMENT_ACTIONS: dict[str, dict[DocumentActionPhase, DocumentAction]] = {}


def register_document_action(
    name: str,
    phase: DocumentActionPhase,
    handler: DocumentAction,
) -> None:
    """Register one built-in document action phase.

    Registration is explicit so Workflow-owned actions can continue to take
    precedence at the routing layer.
    """
    action = name.strip()
    if not action:
        raise ValueError("document action name must not be empty")
    if not callable(handler):
        raise TypeError("document action handler must be callable")
    phases = _DOCUMENT_ACTIONS.setdefault(action, {})
    if phase in phases:
        raise ValueError(
            f"document action {action!r} phase {phase!r} is already registered"
        )
    phases[phase] = handler


def get_document_action(name: str, phase: DocumentActionPhase) -> DocumentAction | None:
    """Return a registered built-in action handler, if available."""
    return _DOCUMENT_ACTIONS.get(name.strip(), {}).get(phase)


def document_action_names() -> tuple[str, ...]:
    """Return registered action names in deterministic order."""
    return tuple(sorted(_DOCUMENT_ACTIONS))


__all__ = [
    "DocumentAction",
    "DocumentActionPhase",
    "document_action_names",
    "get_document_action",
    "register_document_action",
]
