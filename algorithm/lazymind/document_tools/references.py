"""Cross-reference discovery, binding, and refresh helpers."""

from __future__ import annotations
from typing import Any


def _bind_document_cross_reference_targets(instructions: list[Any]) -> None:
    targets = list(
        dict.fromkeys(
            str(target)
            for instruction in instructions
            if isinstance(instruction, dict)
            for target in [
                (instruction.get('meta') or {}).get('outline_node_id'),
                *[
                    item.get('target')
                    for item in (instruction.get('meta') or {}).get('cross_references')
                    or []
                    if isinstance(item, dict)
                ],
            ]
            if target
        )
    )
    for instruction in instructions:
        if isinstance(instruction, dict):
            instruction.setdefault('meta', {})['cross_reference_targets'] = targets


def bind_cross_reference_targets(instructions: list[Any]) -> None:
    _bind_document_cross_reference_targets(instructions)


__all__ = ['bind_cross_reference_targets']
