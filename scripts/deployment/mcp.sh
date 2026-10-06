#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
MODEL_ENV_FILE="${MODEL_ENV_FILE:-env/.env.models}"
MCP_ENV_FILE="${MCP_ENV_FILE:-env/.env.mcp}"
API_ENV_FILE="${API_ENV_FILE:-env/.env.api}"
MCP_IMAGE="${HARBORRAG_MCP_IMAGE:-harborrag-mcp-mcp}"
MCP_STARTUP_TIMEOUT="${HARBORRAG_MCP_STARTUP_TIMEOUT:-120}"

usage() {
    cat <<'EOF'
Usage: scripts/deployment/mcp.sh [--build] [MODE] [OPTION...]

Modes (all run the MCP server in the harborrag-mcp Docker container):
  (default)          Stdio server for an MCP client; stdin/stdout must be pipes
  --check [OPTION...]
                     Perform an MCP handshake and print the advertised tools
  --http             Start the loopback HTTP server and status UI in the
                     background and wait until it is healthy
  down               Stop the background HTTP server
  logs [OPTION...]   Show the background HTTP server logs

Other options are forwarded to the server in stdio and check mode. HTTP host,
port, and path come from env/.env.mcp.

--build rebuilds the MCP image; a missing image is built automatically. Use it
after source, dependency, or baked configuration changes.

Environment file paths can be overridden with DATABASE_ENV_FILE,
MODEL_ENV_FILE, and MCP_ENV_FILE. The data services must already be running
('scripts/deployment/dev.sh data'); the container uses the host network to
reach their loopback ports.
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

prepare_compose() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    require_file "${MCP_ENV_FILE}" "MCP environment"
    # Compose would also refuse an empty value, but name the fix instead of
    # printing a raw variable-substitution error.
    local name
    for name in HARBORRAG_MCP_DB_PASSWORD HARBORRAG_MCP_OBJECT_STORE_SECRET_ACCESS_KEY; do
        grep -Eq "^${name}=.+$" "${ROOT_DIR}/${DATABASE_ENV_FILE}" ||
            fail "${name} is not set in ${DATABASE_ENV_FILE}. Run 'scripts/deployment/dev.sh bootstrap', then 'scripts/deployment/dev.sh mcp-role' with the data services running."
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
    mcp_compose=(
        docker compose
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}"
        --file "${ROOT_DIR}/deploy/compose/docker-compose.mcp.yml"
    )
}

# Build output goes to stderr so stdout stays reserved for the stdio protocol.
ensure_image() {
    local rebuild="$1"
    if ((rebuild)) || ! docker image inspect "${MCP_IMAGE}" >/dev/null 2>&1; then
        echo "Building HarborRAG MCP image..." >&2
        "${mcp_compose[@]}" build mcp >&2
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
        "${mcp_compose[@]}" down
        ;;
    logs)
        shift
        ((rebuild_image == 0)) || fail "--build is not supported with logs."
        prepare_compose
        "${mcp_compose[@]}" logs "$@" mcp
        ;;
    --http)
        shift
        [[ "$#" -eq 0 ]] ||
            fail "--http accepts no options; set HARBORRAG_MCP_HOST/PORT/PATH in ${MCP_ENV_FILE}."
        [[ "${MCP_STARTUP_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] ||
            fail "HARBORRAG_MCP_STARTUP_TIMEOUT must be a positive integer."
        prepare_compose
        "${mcp_compose[@]}" config --quiet
        ensure_image "${rebuild_image}"
        echo "Starting HarborRAG MCP HTTP server..."
        "${mcp_compose[@]}" up \
            --no-build \
            --detach \
            --wait \
            --wait-timeout "${MCP_STARTUP_TIMEOUT}" \
            mcp
        mcp_port="$(
            sed -n 's/^HARBORRAG_MCP_PORT=//p' \
                "${ROOT_DIR}/${MCP_ENV_FILE}" | tail -n 1
        )"
        mcp_port="${mcp_port:-8010}"
        echo "HarborRAG MCP status UI: http://127.0.0.1:${mcp_port}/"
        echo "Stop it with 'scripts/deployment/mcp.sh down'."
        ;;
    *)
        # The container never gets a TTY, so reject a terminal here instead of
        # letting the stdio server wait silently for a client that never comes.
        if [[ " $* " != *" --check "* && -t 0 ]]; then
            fail "The stdio server must be launched by an MCP client; run with --check to verify it from a terminal."
        fi
        prepare_compose
        "${mcp_compose[@]}" config --quiet >&2
        ensure_image "${rebuild_image}"
        # Compose's default command is --http; stdio is selected explicitly and
        # any forwarded option (including --check) still applies.
        exec "${mcp_compose[@]}" run --rm --no-deps -T mcp --transport stdio "$@"
        ;;
esac
