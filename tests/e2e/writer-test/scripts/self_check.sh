#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$TEST_DIR/../../.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "缺少 Python 虚拟环境: $PYTHON" >&2
    exit 2
fi

PYTHONPATH="$REPO_ROOT/tests/e2e" "$PYTHON" -m unittest discover \
    -s "$TEST_DIR/tests"

node --test "$REPO_ROOT/frontend/tests/e2e/writer_bridge.unit.mjs"

cd "$REPO_ROOT/frontend"
pnpm exec tsc -p tests/e2e/tsconfig.json --noEmit
