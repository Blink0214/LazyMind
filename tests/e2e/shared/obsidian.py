"""Read actual Markdown in a local Vault, never LazyMind preview/cache files."""
from __future__ import annotations

import hashlib
from pathlib import Path
from .readback_common import ReadbackDocument


def fetch_obsidian_document(vault: str | Path, path: str | Path) -> ReadbackDocument:
    root = Path(vault).expanduser().resolve(strict=True)
    relative = Path(path)
    if not root.is_dir() or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Expected a Vault directory and a relative note path')
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root) or target.suffix.lower() != '.md' or not target.is_file():
        raise ValueError('Target must be a Markdown file inside the Vault')
    data = target.read_bytes()
    content = data.decode('utf-8-sig')
    from urllib.parse import quote
    return ReadbackDocument('obsidian', str(target.relative_to(root)),
                            hashlib.sha256(data).hexdigest(), content, target.stem,
                            'obsidian://open?path=' + quote(str(target), safe=''),
                            {'vault': str(root), 'local_path': str(target), 'size': len(data)})
