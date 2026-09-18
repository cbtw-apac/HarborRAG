#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python_satisfies_requirement() {
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1
}

if [[ -n "${HARBORRAG_MCP_PYTHON_BIN:-}" ]]; then
    mcp_python="${HARBORRAG_MCP_PYTHON_BIN}"
    python_satisfies_requirement "${mcp_python}" || {
        echo "HARBORRAG_MCP_PYTHON_BIN must point to Python >=3.12." >&2
        exit 2
    }
elif [[ -x "${ROOT_DIR}/.venv/bin/python" ]] &&
    python_satisfies_requirement "${ROOT_DIR}/.venv/bin/python"; then
    mcp_python="${ROOT_DIR}/.venv/bin/python"
else
    mcp_python=""
    for candidate in python3 python; do
        if command -v "${candidate}" >/dev/null 2>&1 &&
            python_satisfies_requirement "${candidate}"; then
            mcp_python="$(command -v "${candidate}")"
            break
        fi
    done
    if [[ -z "${mcp_python}" ]]; then
        echo "Python >=3.12 is required to start the MCP server." >&2
        exit 2
    fi
fi

# Keep the MCP URL path intact when Git Bash launches native Windows Python.
export MSYS2_ENV_CONV_EXCL="${MSYS2_ENV_CONV_EXCL:+${MSYS2_ENV_CONV_EXCL};}HARBORRAG_MCP_PATH"
cd "${ROOT_DIR}"
exec "${mcp_python}" -m harborrag_mcp_server --local-stack-root "${ROOT_DIR}" "$@"
