#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
MODEL_ENV_FILE="${MODEL_ENV_FILE:-env/.env.models}"
API_ENV_FILE="${API_ENV_FILE:-env/.env.api}"
MCP_ENV_FILE="${MCP_ENV_FILE:-env/.env.mcp}"

usage() {
    cat <<'EOF'
Usage: scripts/deployment/mcp.sh [--check | --http [HTTP_OPTIONS]]

Start HarborRAG MCP over stdio, or over authenticated local Streamable HTTP.
This command does not start Docker services, the API, or a Temporal worker.

Options:
  --check  Validate the installation and print the registered tool names
  --http   Listen on http://127.0.0.1:8010/mcp and serve a status UI at /
  -h       Show this help

HTTP options following --http are forwarded to the server: --host, --port,
--path, and --config. HTTP requires HARBORRAG_MCP_BEARER_TOKEN (at least 32
bytes). HARBORRAG_MCP_CONFIG_PATH selects the configuration for either mode.

Environment file paths can be overridden with DATABASE_ENV_FILE,
MODEL_ENV_FILE, API_ENV_FILE, and MCP_ENV_FILE. The MCP environment file stores
the local HTTP bearer token and MCP-specific overrides. HARBORRAG_MCP_PYTHON_BIN
selects the Python interpreter.
EOF
}

fail() {
    echo "$1" >&2
    exit 2
}

require_file() {
    local path="$1"
    local label="$2"
    [[ -f "${ROOT_DIR}/${path}" ]] ||
        fail "Missing ${label}: ${path}. Run 'scripts/deployment/dev.sh bootstrap'."
}

load_environment_file() {
    local path="$1"
    set -a
    # shellcheck disable=SC1090 -- protected environment paths are configurable.
    source "${ROOT_DIR}/${path}"
    set +a
}

python_executable() {
    if [[ -n "${HARBORRAG_MCP_PYTHON_BIN:-}" ]]; then
        echo "${HARBORRAG_MCP_PYTHON_BIN}"
    elif [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
        echo "${ROOT_DIR}/.venv/bin/python"
    else
        command -v python3 || fail "Python 3 is required to start the MCP server."
    fi
}

check_only=0
http_mode=0
case "${1:-}" in
    "")
        ;;
    --check)
        check_only=1
        shift
        ;;
    --http)
        http_mode=1
        shift
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    --*)
        usage >&2
        fail "Unknown option: $1"
        ;;
esac
if ((http_mode)); then
    http_arguments=("$@")
    set --
else
    [[ "$#" -eq 0 ]] || fail "Unknown option: $1"
    http_arguments=()
fi

require_file "${DATABASE_ENV_FILE}" "database environment"
require_file "${MODEL_ENV_FILE}" "model environment"
require_file "${API_ENV_FILE}" "API environment"
load_environment_file "${DATABASE_ENV_FILE}"
load_environment_file "${MODEL_ENV_FILE}"
load_environment_file "${API_ENV_FILE}"
if [[ -f "${ROOT_DIR}/${MCP_ENV_FILE}" ]]; then
    load_environment_file "${MCP_ENV_FILE}"
elif ((http_mode)); then
    fail "Missing MCP environment: ${MCP_ENV_FILE}. Run 'scripts/deployment/dev.sh bootstrap'."
fi

mcp_python="$(python_executable)"
"${mcp_python}" -c \
    "import importlib.util, sys; sys.exit(any(importlib.util.find_spec(name) is None for name in ('fastmcp', 'harborrag_mcp_server', 'harborrag_runtime')))" \
    >/dev/null 2>&1 ||
    fail "MCP dependencies are missing. Run 'uv sync --package harborrag-mcp-server --extra mcp'."

[[ -n "${POSTGRES_USER:-}" && -n "${POSTGRES_PASSWORD:-}" && -n "${POSTGRES_DB:-}" ]] ||
    fail "Database environment must define POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB."
export HARBORRAG_CONTROL_DB_URL="${HARBORRAG_CONTROL_DB_URL:-$(
    "${mcp_python}" -c '
import os
from urllib.parse import quote

user = quote(os.environ["POSTGRES_USER"], safe="")
password = quote(os.environ["POSTGRES_PASSWORD"], safe="")
database = quote(os.environ["POSTGRES_DB"], safe="")
port = os.environ.get("POSTGRES_PORT", "5432")
print(f"postgresql+asyncpg://{user}:{password}@localhost:{port}/{database}")
'
)}"
export HARBORRAG_OBJECT_STORE_ENDPOINT_URL="${HARBORRAG_OBJECT_STORE_ENDPOINT_URL:-http://localhost:${MINIO_API_PORT:-9000}}"
export HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID="${HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID:-${MINIO_ROOT_USER:-}}"
export HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY="${HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:-}}"
export HARBORRAG_QDRANT_URL="${HARBORRAG_QDRANT_URL:-http://localhost:${QDRANT_HTTP_PORT:-6333}}"
export HARBORRAG_FALKORDB_HOST="${HARBORRAG_FALKORDB_HOST:-localhost}"
export HARBORRAG_FALKORDB_PORT="${HARBORRAG_FALKORDB_PORT:-${FALKORDB_PORT:-6379}}"
export HARBORRAG_MODEL_CONFIG_PATH="${HARBORRAG_MODEL_CONFIG_PATH:-${ROOT_DIR}/config/models.yaml}"
[[ -f "${HARBORRAG_MODEL_CONFIG_PATH}" ]] ||
    fail "Model configuration does not exist: ${HARBORRAG_MODEL_CONFIG_PATH}"

mcp_arguments=()
if ((check_only)); then
    mcp_arguments+=(--check)
elif ((http_mode)); then
    mcp_arguments+=(--transport http "${http_arguments[@]}")
    echo "Starting HarborRAG MCP server over authenticated local HTTP..." >&2
else
    echo "Starting HarborRAG MCP server over local stdio..." >&2
fi

cd "${ROOT_DIR}"
exec "${mcp_python}" -m harborrag_mcp_server "${mcp_arguments[@]}"
