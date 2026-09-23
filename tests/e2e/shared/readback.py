"""Run with PYTHONPATH=tests/e2e python -m shared.readback PROVIDER ..."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from .notion import fetch_notion_document
from .github import fetch_github_document
from .obsidian import fetch_obsidian_document
from .wechat import fetch_wechat_document


def load_wechat_env(env_path: Path | None = None):
    """Load only E2E WeChat credentials; process environment takes precedence."""
    path = env_path if env_path is not None else Path(__file__).resolve().parents[3] / '.env'
    if not path.is_file():
        return
    from dotenv import dotenv_values
    values = dotenv_values(path, interpolate=False)
    for key in ('LAZYMIND_E2E_WECHAT_ACCESS_TOKEN', 'LAZYMIND_E2E_WECHAT_APP_ID',
                'LAZYMIND_E2E_WECHAT_APP_SECRET'):
        if values.get(key) is not None:
            os.environ.setdefault(key, values[key])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=180)
    commands = parser.add_subparsers(dest='provider', required=True)
    commands.add_parser('notion').add_argument('reference')
    github = commands.add_parser('github')
    github.add_argument('--repo', required=True)
    github.add_argument('--path', required=True)
    github.add_argument('--ref', required=True)
    obsidian = commands.add_parser('obsidian')
    obsidian.add_argument('--vault', required=True)
    obsidian.add_argument('--path', required=True)
    wechat = commands.add_parser('wechat')
    wechat.add_argument('--media-id', required=True)
    wechat.add_argument('--article-index', type=int, default=0)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; choose a new evidence path')
    try:
        if args.provider == 'notion':
            doc = fetch_notion_document(args.reference, timeout=args.timeout)
        elif args.provider == 'github':
            doc = fetch_github_document(args.repo, args.path, ref=args.ref, timeout=args.timeout)
        elif args.provider == 'wechat':
            load_wechat_env()
            doc = fetch_wechat_document(args.media_id, article_index=args.article_index, timeout=args.timeout)
        else:
            doc = fetch_obsidian_document(args.vault, args.path)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(doc.to_dict(), stream, ensure_ascii=False, indent=2)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'Readback failed: {exc}\n')
    print(f'Readback saved: {args.output}')


if __name__ == '__main__':
    main()
