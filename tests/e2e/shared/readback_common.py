"""Independent, read-only provider evidence helpers (no LazyMind imports)."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit, urlunsplit


@dataclass
class ReadbackDocument:
    provider: str
    document_id: str
    revision_id: str
    content: str
    title: str = ''
    url: str = ''
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return sanitize(asdict(self))


def sanitize(value):
    """Remove URL query credentials from structured evidence, including text."""
    import re
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()
                if k.lower() not in {'access_token', 'token', 'authorization', 'secret'}}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        def clean(match):
            url = urlsplit(match.group())
            # Signed file URLs are evidence references, not reusable download URLs.
            query = url.query.lower()
            if url.username or any(key in query for key in
                                  ('signature=', 'token=', 'x-amz-', 'credential=', 'sig=')):
                return urlunsplit((url.scheme, url.hostname or '', url.path, '', ''))
            return match.group()
        return re.sub(r'https?://[^\s<>"\)]+', clean, value)
    return value


class CliReader:
    def __init__(self, timeout: int):
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        self.deadline = time.monotonic() + timeout

    def json(self, args: list[str]):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Provider readback timed out')
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=remaining, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f'{args[0]} unavailable or timed out') from exc
        if result.returncode:
            # CLI stderr can contain credentials/URLs; do not persist it.
            raise RuntimeError(f'{args[0]} readback failed (exit {result.returncode}); check login and target access')
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError(f'{args[0]} returned invalid JSON') from exc
