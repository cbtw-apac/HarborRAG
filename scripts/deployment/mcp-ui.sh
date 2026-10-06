#!/usr/bin/env bash
set -euo pipefail

# Optional launcher for the HarborRAG Explorer MCP UI server (harborrag-mcp-ui).
# The reader MCP server (scripts/deployment/mcp.sh) never needs it.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
MODEL_ENV_FILE="${MODEL_ENV_FILE:-env/.env.models}"
MCP_ENV_FILE="${MCP_ENV_FILE:-env/.env.mcp}"
API_ENV_FILE="${API_ENV_FILE:-env/.env.api}"
MCP_IMAGE="${HARBORRAG_MCP_IMAGE:-harborrag-mcp-mcp}"
MCP_UI_IMAGE="${HARBORRAG_MCP_UI_IMAGE:-harborrag-mcp-ui}"
MCP_STARTUP_TIMEOUT="${HARBORRAG_MCP_STARTUP_TIMEOUT:-120}"

usage() {
    cat <<'USAGE'
Usage: scripts/deployment/mcp-ui.sh [--build] [MODE] [OPTION...]

Optional: runs the HarborRAG Explorer MCP UI server, an MCP App for searching,
reading and tracing the corpus, in the harborrag-mcp-ui Docker container. The
reader MCP server (scripts/deployment/mcp.sh) works without it.

Modes:
  (default)          Stdio server for an MCP client; stdin/stdout must be pipes
  --check [OPTION...]
                     Perform an MCP handshake and print the Explorer tools
  --http             Start the loopback HTTP server in the background and wait
                     until it is healthy (port HARBORRAG_MCP_UI_PORT, default 8011)
  down               Stop the background HTTP server
  logs [OPTION...]   Show the background HTTP server logs

Other options are forwarded to harborrag-mcp-ui in stdio and check mode. Host,
path, bearer token and tool configuration are shared with the reader server
through env/.env.mcp and config/mcp.yaml.

--build rebuilds the reader MCP image and the Explorer layer on top of it; a
missing image is built automatically. Use it after source, dependency, or baked
configuration changes.

Environment file paths can be overridden with DATABASE_ENV_FILE,
MODEL_ENV_FILE, and MCP_ENV_FILE. The data services must already be running
('scripts/deployment/dev.sh data') and the MCP database role provisioned once
('scripts/deployment/dev.sh mcp-role').
USAGE
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

env_value() {
    sed -n "s/^$1=//p" "${ROOT_DIR}/${MCP_ENV_FILE}" | tail -n 1
}

prepare_compose() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    require_file "${MCP_ENV_FILE}" "MCP environment"
    # Same precheck as mcp.sh: name the fix instead of Compose's raw ':?' error.
    local name
    for name in HARBORRAG_MCP_DB_PASSWORD HARBORRAG_MCP_OBJECT_STORE_SECRET_ACCESS_KEY; do
        grep -Eq "^${name}=.+$" "${ROOT_DIR}/${DATABASE_ENV_FILE}" ||
            fail "${name} is not set in ${DATABASE_ENV_FILE}. Run 'scripts/deployment/dev.sh bootstrap', then 'scripts/deployment/dev.sh mcp-role' once the API has started."
    done
    # Reader keys carry the environment they were issued in, and the CLI issues
    # them with the API's HARBORRAG_ENV (env/.env.api). Give the MCP server the
    # same value so a key the API's environment issued is not refused here.
    if [[ -z "${HARBORRAG_ENV:-}" && -f "${ROOT_DIR}/${API_ENV_FILE}" ]]; then
        HARBORRAG_ENV="$(sed -n 's/^HARBORRAG_ENV=//p' "${ROOT_DIR}/${API_ENV_FILE}" | tail -n 1)"
        export HARBORRAG_ENV="${HARBORRAG_ENV:-dev}"
    fi
    export HARBORRAG_MCP_ENV_FILE="${ROOT_DIR}/${MCP_ENV_FILE}"
    export HARBORRAG_MODEL_ENV_FILE="${ROOT_DIR}/${MODEL_ENV_FILE}"
    export HARBORRAG_MCP_IMAGE="${MCP_IMAGE}"
    export HARBORRAG_MCP_UI_IMAGE="${MCP_UI_IMAGE}"
    base_compose=(
        docker compose
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}"
        --file "${ROOT_DIR}/deploy/compose/docker-compose.mcp.yml"
    )
    ui_compose=(
        docker compose
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}"
        --file "${ROOT_DIR}/deploy/compose/docker-compose.mcp-ui.yml"
    )
}

# Build output goes to stderr so stdout stays reserved for the stdio protocol.
# The Explorer image is a layer over the reader image, so that builds first.
ensure_images() {
    local rebuild="$1"
    if ((rebuild)) || ! docker image inspect "${MCP_IMAGE}" >/dev/null 2>&1; then
        echo "Building HarborRAG MCP image..." >&2
        "${base_compose[@]}" build mcp >&2
    fi
    if ((rebuild)) || ! docker image inspect "${MCP_UI_IMAGE}" >/dev/null 2>&1; then
        echo "Building HarborRAG Explorer MCP UI image..." >&2
        "${ui_compose[@]}" build mcp-ui >&2
    fi
}

rebuild_image=0
while [[ "${1:-}" == "--build" ]]; do
    rebuild_image=1
    shift
done

case "${1:-}" in
    -h|--help|help)
        usage
        ;;
    down)
        shift
        [[ "$#" -eq 0 ]] || fail "down accepts no options."
        ((rebuild_image == 0)) || fail "--build is not supported with down."
        prepare_compose
        "${ui_compose[@]}" down
        ;;
    logs)
        shift
        ((rebuild_image == 0)) || fail "--build is not supported with logs."
        prepare_compose
        "${ui_compose[@]}" logs "$@" mcp-ui
        ;;
    --http)
        shift
        [[ "$#" -eq 0 ]] ||
            fail "--http accepts no options; set HARBORRAG_MCP_HOST/UI_PORT/PATH in ${MCP_ENV_FILE}."
        [[ "${MCP_STARTUP_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] ||
            fail "HARBORRAG_MCP_STARTUP_TIMEOUT must be a positive integer."
        prepare_compose
        "${ui_compose[@]}" config --quiet
        ensure_images "${rebuild_image}"
        echo "Starting HarborRAG Explorer MCP UI server..."
        "${ui_compose[@]}" up \
            --no-build \
            --detach \
            --wait \
            --wait-timeout "${MCP_STARTUP_TIMEOUT}" \
            mcp-ui
        ui_port="$(env_value HARBORRAG_MCP_UI_PORT)"
        ui_path="$(env_value HARBORRAG_MCP_PATH)"
        echo "HarborRAG Explorer MCP endpoint: http://127.0.0.1:${ui_port:-8011}${ui_path:-/mcp}"
        echo "Stop it with 'scripts/deployment/mcp-ui.sh down'."
        ;;
    *)
        # The container never gets a TTY, so reject a terminal here instead of
        # letting the stdio server wait silently for a client that never comes.
        if [[ " $* " != *" --check "* && -t 0 ]]; then
            fail "The stdio server must be launched by an MCP client; run with --check to verify it from a terminal."
        fi
        prepare_compose
        "${ui_compose[@]}" config --quiet >&2
        ensure_images "${rebuild_image}"
        exec "${ui_compose[@]}" run --rm --no-deps -T mcp-ui --transport stdio "$@"
        ;;
esac
