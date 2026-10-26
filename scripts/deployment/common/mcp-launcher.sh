# Shared launcher for the MCP server entrypoints (scripts/deployment/mcp*.sh).
# Sourced, never executed. Env files are passed to Docker Compose, never
# sourced, so their values are not evaluated as shell code.
#
# The sourcing script sets ROOT_DIR and, before calling mcp_launcher_main:
#   MCP_LAUNCHER      its own path for messages, e.g. scripts/deployment/mcp.sh
#   MCP_SERVICE       the Compose service to run
#   MCP_COMPOSE_FILE  that service's Compose file under deploy/compose/
#   MCP_SERVER_LABEL  a name for progress messages
#   MCP_HTTP_SETTINGS the env/.env.mcp settings that configure --http
# and defines three functions:
#   usage                  print help
#   mcp_build_images N     build missing images, or all of them when N is 1
#   mcp_print_endpoint     print where the started HTTP server listens

DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
MODEL_ENV_FILE="${MODEL_ENV_FILE:-env/.env.models}"
MCP_ENV_FILE="${MCP_ENV_FILE:-env/.env.mcp}"
API_ENV_FILE="${API_ENV_FILE:-env/.env.api}"
MCP_IMAGE="${HARBORRAG_MCP_IMAGE:-harborrag-mcp-mcp}"
MCP_STARTUP_TIMEOUT="${HARBORRAG_MCP_STARTUP_TIMEOUT:-120}"

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

# The last NAME=value of the MCP env file, without evaluating it.
mcp_env_value() {
    sed -n "s/^$1=//p" "${ROOT_DIR}/${MCP_ENV_FILE}" | tail -n 1
}

# A docker compose command for one file under deploy/compose/.
compose_command() {
    printf '%s\n' docker compose \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/$1"
}

prepare_compose() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    require_file "${MCP_ENV_FILE}" "MCP environment"
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
    mapfile -t service_compose < <(compose_command "${MCP_COMPOSE_FILE}")
}

# Build one image when asked to or when it is missing. Build output goes to
# stderr so stdout stays reserved for the stdio protocol.
build_image() {
    local rebuild="$1" image="$2" label="$3" compose_file="$4" service="$5"
    if ((rebuild)) || ! docker image inspect "${image}" >/dev/null 2>&1; then
        echo "Building HarborRAG ${label} image..." >&2
        local -a compose
        mapfile -t compose < <(compose_command "${compose_file}")
        "${compose[@]}" build "${service}" >&2
    fi
}

mcp_launcher_main() {
    local rebuild_image=0
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
            "${service_compose[@]}" down
            ;;
        logs)
            shift
            ((rebuild_image == 0)) || fail "--build is not supported with logs."
            prepare_compose
            "${service_compose[@]}" logs "$@" "${MCP_SERVICE}"
            ;;
        --http)
            shift
            [[ "$#" -eq 0 ]] ||
                fail "--http accepts no options; set ${MCP_HTTP_SETTINGS} in ${MCP_ENV_FILE}."
            [[ "${MCP_STARTUP_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] ||
                fail "HARBORRAG_MCP_STARTUP_TIMEOUT must be a positive integer."
            prepare_compose
            "${service_compose[@]}" config --quiet
            mcp_build_images "${rebuild_image}"
            echo "Starting HarborRAG ${MCP_SERVER_LABEL}..."
            "${service_compose[@]}" up \
                --no-build \
                --detach \
                --wait \
                --wait-timeout "${MCP_STARTUP_TIMEOUT}" \
                "${MCP_SERVICE}"
            mcp_print_endpoint
            echo "Stop it with '${MCP_LAUNCHER} down'."
            ;;
        *)
            # The container never gets a TTY, so reject a terminal here instead of
            # letting the stdio server wait silently for a client that never comes.
            if [[ " $* " != *" --check "* && -t 0 ]]; then
                fail "The stdio server must be launched by an MCP client; run with --check to verify it from a terminal."
            fi
            prepare_compose
            "${service_compose[@]}" config --quiet >&2
            mcp_build_images "${rebuild_image}"
            # Compose's default command is --http; stdio is selected explicitly and
            # any forwarded option (including --check) still applies.
            exec "${service_compose[@]}" run --rm --no-deps -T "${MCP_SERVICE}" --transport stdio "$@"
            ;;
    esac
}
