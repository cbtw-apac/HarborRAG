# shellcheck shell=bash
# Shared launcher for the MCP server entrypoints (scripts/deployment/mcp*.sh).
# Sourced, never executed. Env files are passed to Docker Compose, never
# sourced, so their values are not evaluated as shell code.
#
# The sourcing script sets ROOT_DIR and, before calling mcp_launcher_main:
#   MCP_LAUNCHER      its own path for messages, e.g. scripts/deployment/mcp.sh
#   MCP_SERVICE       the Compose service to run
#   MCP_SERVICE_IMAGE that service's image, which --check runs without Compose
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
# The Docker network of the data services. The servers join it to reach them.
DATA_NETWORK="harborrag-data-network"
# What the images are built from. A change under these paths makes an image stale.
IMAGE_SOURCE_PATHS=(packages uv.lock pyproject.toml deploy/docker)

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

# Only the data stack creates the network, so a server cannot run before it has.
require_data_network() {
    docker network inspect "${DATA_NETWORK}" >/dev/null 2>&1 ||
        fail "Docker network ${DATA_NETWORK} does not exist. Start the data services with 'scripts/deployment/dev.sh data'."
}

# The last NAME=value of the MCP env file, without evaluating it.
mcp_env_value() {
    sed -n "s/^$1=//p" "${ROOT_DIR}/${MCP_ENV_FILE}" | tail -n 1
}

# A port from the MCP env file, read the way Compose reads it: without a carriage
# return, a trailing comment, surrounding blanks or quotes. Empty when unset.
mcp_env_port() {
    local value
    value="$(mcp_env_value "$1")"
    value="${value%$'\r'}"
    value="${value%%[[:space:]]#*}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value#[\"\']}"
    value="${value%[\"\']}"
    [[ -z "${value}" || "${value}" =~ ^[0-9]+$ ]] ||
        fail "$1 in ${MCP_ENV_FILE} must be a port number."
    printf '%s\n' "${value}"
}

# The settings env files assign, one name per line, without evaluating the files.
env_file_keys() {
    sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$@" | LC_ALL=C sort -u
}

# Name the variables the model configuration reads that no env file sets. A
# setting that has a default is not reported: left out, it uses that default.
report_unset_model_variables() {
    local model_config="config/models/models.yaml"
    local missing
    local -a model_sources=("${ROOT_DIR}/${MCP_ENV_FILE}")
    [[ -f "${ROOT_DIR}/${model_config}" ]] || return 0
    [[ ! -f "${ROOT_DIR}/${MODEL_ENV_FILE}" ]] || model_sources+=("${ROOT_DIR}/${MODEL_ENV_FILE}")
    # shellcheck disable=SC2016  # the patterns match a literal ${NAME} in the catalog
    missing="$(LC_ALL=C comm -23 \
        <(grep -v '^[[:space:]]*#' "${ROOT_DIR}/${model_config}" |
            grep -o '\${[A-Za-z_][A-Za-z0-9_]*}' | tr -d '${}' | LC_ALL=C sort -u) \
        <(env_file_keys "${model_sources[@]}") | paste -sd ' ' -)"
    [[ -z "${missing}" ]] ||
        echo "Note: ${model_config} reads ${missing}, which ${MODEL_ENV_FILE} does not set. The current names are in env-example/.env.models.example." >&2
}

# The commit the image source is at, marked "-dirty" when it has uncommitted changes.
source_revision() {
    local revision
    revision="$(git -C "${ROOT_DIR}" rev-parse HEAD 2>/dev/null)" || {
        echo unknown
        return 0
    }
    git -C "${ROOT_DIR}" diff --quiet HEAD -- "${IMAGE_SOURCE_PATHS[@]}" 2>/dev/null ||
        revision+="-dirty"
    echo "${revision}"
}

# Say so when an existing image was built from other source than the checkout holds
# now. An image built from uncommitted changes cannot be compared and is left alone.
# An image built before images were stamped carries its base image's revision, which
# is no commit of this checkout.
report_stale_image() {
    local image="$1" revision
    git -C "${ROOT_DIR}" rev-parse HEAD >/dev/null 2>&1 || return 0
    revision="$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "${image}" 2>/dev/null || true)"
    [[ "${revision}" != *-dirty ]] || return 0
    if ! git -C "${ROOT_DIR}" cat-file -e "${revision}^{commit}" 2>/dev/null; then
        echo "Note: image ${image} does not record a commit of this checkout. Run '${MCP_LAUNCHER} --build' once so later runs can tell when it is out of date." >&2
        return 0
    fi
    git -C "${ROOT_DIR}" diff --quiet "${revision}" -- "${IMAGE_SOURCE_PATHS[@]}" 2>/dev/null ||
        echo "Note: image ${image} was built from ${revision:0:8} and the source has changed since. Rebuild it with '${MCP_LAUNCHER} --build'." >&2
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
    # Compose publishes each server on the host port the MCP env file names; inside
    # its container the server keeps its default port.
    HARBORRAG_MCP_PORT="$(mcp_env_port HARBORRAG_MCP_PORT)" || exit 2
    HARBORRAG_MCP_UI_PORT="$(mcp_env_port HARBORRAG_MCP_UI_PORT)" || exit 2
    export HARBORRAG_MCP_PORT HARBORRAG_MCP_UI_PORT
    export HARBORRAG_MODEL_ENV_FILE="${ROOT_DIR}/${MODEL_ENV_FILE}"
    export HARBORRAG_MCP_IMAGE="${MCP_IMAGE}"
    service_compose=()
    local argument
    while IFS= read -r argument; do
        service_compose+=("${argument}")
    done < <(compose_command "${MCP_COMPOSE_FILE}")
}

# Build one image when asked to or when it is missing, and stamp it with the
# commit it was built from. An image that is reused is checked against the
# checkout instead. Build output goes to stderr so stdout stays reserved for the
# stdio protocol.
build_image() {
    local rebuild="$1" image="$2" label="$3" compose_file="$4" service="$5"
    if ((rebuild)) || ! docker image inspect "${image}" >/dev/null 2>&1; then
        echo "Building HarborRAG ${label} image..." >&2
        local -a compose=()
        local argument
        while IFS= read -r argument; do
            compose+=("${argument}")
        done < <(compose_command "${compose_file}")
        HARBORRAG_GIT_REVISION="$(source_revision)" "${compose[@]}" build "${service}" >&2
    else
        report_stale_image "${image}"
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
            require_data_network
            mcp_build_images "${rebuild_image}"
            report_unset_model_variables
            # The same start-up the server runs, once and in the foreground, so a
            # missing setting is one line here and not a restart loop in the background.
            echo "Checking HarborRAG ${MCP_SERVER_LABEL} configuration..."
            "${service_compose[@]}" run --rm --no-deps -T "${MCP_SERVICE}" --http --preflight ||
                fail "HarborRAG ${MCP_SERVER_LABEL} was not started. The line above says why: a missing setting belongs in ${MCP_ENV_FILE} or ${MODEL_ENV_FILE}, and an unreachable backend means the data services are not up yet ('scripts/deployment/dev.sh data')."
            echo "Starting HarborRAG ${MCP_SERVER_LABEL}..."
            if ! "${service_compose[@]}" up \
                --no-build \
                --detach \
                --wait \
                --wait-timeout "${MCP_STARTUP_TIMEOUT}" \
                "${MCP_SERVICE}"; then
                "${service_compose[@]}" logs --tail 20 "${MCP_SERVICE}" >&2 || true
                "${service_compose[@]}" down >&2 || true
                fail "HarborRAG ${MCP_SERVER_LABEL} did not become healthy within ${MCP_STARTUP_TIMEOUT}s and was stopped. Its last log lines are above."
            fi
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
            if [[ " $* " == *" --check "* ]]; then
                # The handshake connects no backend, so it runs without any network,
                # and without the data network the Compose service joins.
                mcp_build_images "${rebuild_image}"
                exec docker run --rm --network none \
                    --volume "${ROOT_DIR}/config:/app/config:ro" \
                    --env HARBORRAG_MCP_CONFIG_PATH=/app/config/mcp/server.yaml \
                    "${MCP_SERVICE_IMAGE}" --transport stdio "$@"
            fi
            require_data_network
            mcp_build_images "${rebuild_image}"
            report_unset_model_variables
            # Compose's default command is --http; stdio is selected explicitly and
            # any forwarded option still applies.
            exec "${service_compose[@]}" run --rm --no-deps -T "${MCP_SERVICE}" --transport stdio "$@"
            ;;
    esac
}
