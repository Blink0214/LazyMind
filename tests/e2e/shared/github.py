"""Read repository files using gh; resolve ref once to avoid branch races."""
from __future__ import annotations

import base64
from pathlib import PurePosixPath
from urllib.parse import quote
from .readback_common import CliReader, ReadbackDocument


def fetch_github_document(repository: str, path: str, *, ref: str,
                          timeout: int = 180) -> ReadbackDocument:
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('repository must be owner/repo')
    if not ref.strip() or not path or path.startswith('/') or '..' in PurePosixPath(path).parts:
        raise ValueError('Explicit ref and repository-relative file path required')
    cli = CliReader(timeout)
    commit = cli.json(['gh', 'api', '--hostname', 'github.com',
                       f'repos/{repository}/commits/{quote(ref, safe="")}'])
    sha = commit.get('sha', '')
    if not re.fullmatch(r'[0-9a-fA-F]{40,64}', sha):
        raise RuntimeError('GitHub returned no valid commit SHA')
    payload = cli.json(['gh', 'api', '--hostname', 'github.com',
                        f'repos/{repository}/contents/{quote(path, safe="/")}?ref={sha}'])
    if not isinstance(payload, dict) or payload.get('type') != 'file' or payload.get('encoding') != 'base64':
        raise RuntimeError('GitHub target must be a base64-encoded file (large files unsupported)')
    try:
        content = base64.b64decode(''.join(payload['content'].split()), validate=True).decode('utf-8')
    except (KeyError, ValueError, UnicodeError) as exc:
        raise RuntimeError('GitHub file is not valid UTF-8/base64') from exc
    return ReadbackDocument('github', f'{repository}/{path}', sha, content,
                            PurePosixPath(path).name,
                            f'https://github.com/{repository}/blob/{sha}/{quote(path, safe="/")}',
                            {'repository': repository, 'path': path, 'requested_ref': ref,
                             'commit_sha': sha, 'blob_sha': payload.get('sha'), 'size': payload.get('size')})
