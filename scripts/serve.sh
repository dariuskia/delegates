#!/usr/bin/env bash
# The virtualenv is kept outside the repo: this folder is a synced/mounted
# directory on some machines, where uv cannot rewrite an in-tree .venv.
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/delegates}"
exec uv run uvicorn delegates.web.app:app --reload --port "${PORT:-8000}"
