"""MD/LMD artifact decoding and serialization boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .toolkits import (
    _document_value,
    _json_dumps,
    _json_loads,
    _primary_data,
    _read_artifact_data,
    _result_data,
    _set_document_editable,
    _temp_root,
    _write_document_input,
    _write_input_artifact,
    writer_schema,
)


def decode_document_value(value: str) -> Any:
    """Decode JSON/LMD envelopes while preserving plain Markdown text."""
    return _document_value(value)


def load_artifact_data(path: str | Path) -> Any:
    """Load Markdown or an artifact-envelope JSON file from ``path``."""
    return _read_artifact_data(str(path))


# Internal compatibility exports used while the monolithic toolkit is split.
json_dumps = _json_dumps
json_loads = _json_loads
primary_result_data = _primary_data
result_artifact_data = _result_data
set_document_editable = _set_document_editable
temporary_artifact_store = _temp_root
write_document_input = _write_document_input
write_input_artifact = _write_input_artifact

__all__ = [
    'decode_document_value',
    'json_dumps',
    'json_loads',
    'load_artifact_data',
    'primary_result_data',
    'result_artifact_data',
    'set_document_editable',
    'temporary_artifact_store',
    'write_document_input',
    'write_input_artifact',
    'writer_schema',
]
