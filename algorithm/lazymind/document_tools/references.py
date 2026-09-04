"""Cross-reference normalization shared by MD and LMD writing flows."""

from __future__ import annotations

from typing import Any

from .toolkits import _bind_document_cross_reference_targets


def bind_cross_reference_targets(instructions: list[Any]) -> None:
    """Attach the complete reference-target set to each section instruction."""
    _bind_document_cross_reference_targets(instructions)


__all__ = ['bind_cross_reference_targets']
