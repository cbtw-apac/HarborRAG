#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE:-env/.env.temporal}"
CONNECTOR_ENV_FILE="${CONNECTOR_ENV_FILE:-env/.env.connector}"
PARSER_ENV_FILE="${PARSER_ENV_FILE:-env/.env.parser}"
MODEL_ENV_FILE="${MODEL_ENV_FILE:-env/.env.models}"
API_ENV_FILE="${API_ENV_FILE:-env/.env.api}"
MCP_ENV_FILE="${MCP_ENV_FILE:-env/.env.mcp}"
API_STARTUP_TIMEOUT="${HARBORRAG_API_STARTUP_TIMEOUT:-120}"
API_IMAGE="${HARBORRAG_API_IMAGE:-harborrag-api-api}"
TEMPORAL_WORKER_IMAGE="${HARBORRAG_TEMPORAL_WORKER_IMAGE:-harborrag-temporal-temporal-worker}"

usage() {
    cat <<'EOF'
Usage: scripts/deployment/dev.sh [--build] COMMAND [OPTION...]

Commands:
  bootstrap          Create missing protected env files, then stop for review
  up [--no-worker] [--build]
                     Start data, Temporal, worker (default), and API
  down [--volumes]   Stop API, Temporal/worker, and data services
  data               Start only PostgreSQL, Qdrant, FalkorDB, Redis, and MinIO
  temporal           Start only Temporal server services; never starts a worker
  worker [--build]   Start only the ingestion worker; reuse its image by default
  api [--build]      Start only the API; reuse its image and never start a worker

Environment file paths can be overridden with DATABASE_ENV_FILE,
TEMPORAL_ENV_FILE, CONNECTOR_ENV_FILE, PARSER_ENV_FILE, MODEL_ENV_FILE,
API_ENV_FILE, and MCP_ENV_FILE.
Use --build after source, dependency, or baked worker configuration changes.
For up, worker, and api, --build may appear before or after the command.
If a local API or worker image is missing, the first start builds it automatically.
EOF
}

fail() {
    echo "$1" >&2
    exit 2
}

require_file() {
    local path="$1"
    local label="$2"
    [[ -f "${ROOT_DIR}/${path}" ]] || fail "Missing ${label}: ${path}. Run '$0 bootstrap'."
}

ensure_environment_file() {
    local target="$1"
    local template="$2"

    if [[ -f "${ROOT_DIR}/${target}" ]]; then
        chmod 600 "${ROOT_DIR}/${target}"
        return
    fi
    umask 077
    mkdir -p "$(dirname "${ROOT_DIR}/${target}")"
    cp "${ROOT_DIR}/${template}" "${ROOT_DIR}/${target}"
    chmod 600 "${ROOT_DIR}/${target}"
    echo "Created ${target} from ${template}."
    created_environment=1
}

ensure_mcp_environment_file() {
    local target="$1"
    local template="$2"
    local target_path="${ROOT_DIR}/${target}"
    local bearer_token

    ensure_environment_file "${target}" "${template}"
    if grep -Eq '^HARBORRAG_MCP_BEARER_TOKEN=.+$' "${target_path}"; then
        return
    fi
    command -v openssl >/dev/null ||
        fail "OpenSSL is required to generate the local MCP bearer token."
    bearer_token="$(openssl rand -hex 32)"
    sed -i "s/^HARBORRAG_MCP_BEARER_TOKEN=.*/HARBORRAG_MCP_BEARER_TOKEN=${bearer_token}/" "${target_path}"
    chmod 600 "${target_path}"
    echo "Generated a protected MCP bearer token in ${target}."
}

bootstrap_environment() {
    created_environment=0
    ensure_environment_file "${DATABASE_ENV_FILE}" "env-example/.env.database.example"
    ensure_environment_file "${TEMPORAL_ENV_FILE}" "env-example/.env.temporal.example"
    ensure_environment_file "${CONNECTOR_ENV_FILE}" "env-example/.env.connector.example"
    ensure_environment_file "${PARSER_ENV_FILE}" "env-example/.env.parser.example"
    ensure_environment_file "${MODEL_ENV_FILE}" "env-example/.env.models.example"
    ensure_environment_file "${API_ENV_FILE}" "env-example/.env.api.example"
    ensure_mcp_environment_file "${MCP_ENV_FILE}" "env-example/.env.mcp.example"
}

require_data_network() {
    docker network inspect harborrag-data-network >/dev/null 2>&1 ||
        fail "harborrag-data-network is not running. Start it with '$0 data'."
}

require_temporal_server() {
    if ! temporal_compose run --rm --no-deps temporal-namespace >/dev/null; then
        fail "Temporal is not healthy or its namespace is unavailable. Start it with '$0 temporal'."
    fi
}

data_compose() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    docker compose \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/docker-compose.database.yml" \
        "$@"
}

temporal_compose() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    require_file "${TEMPORAL_ENV_FILE}" "Temporal environment"
    docker compose \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/docker-compose.temporal.yml" \
        "$@"
}

api_compose() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    require_file "${TEMPORAL_ENV_FILE}" "Temporal environment"
    require_file "${CONNECTOR_ENV_FILE}" "connector environment"
    require_file "${MODEL_ENV_FILE}" "model environment"
    require_file "${API_ENV_FILE}" "API environment"
    export HARBORRAG_API_ENV_FILE="${ROOT_DIR}/${API_ENV_FILE}"
    export HARBORRAG_MODEL_ENV_FILE="${ROOT_DIR}/${MODEL_ENV_FILE}"
    docker compose \
        --project-name harborrag-api \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" \
        --env-file "${ROOT_DIR}/${CONNECTOR_ENV_FILE}" \
        --env-file "${ROOT_DIR}/${MODEL_ENV_FILE}" \
        --env-file "${ROOT_DIR}/${API_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/docker-compose.yml" \
        "$@"
}

prepare_worker_mount() {
    require_file "${CONNECTOR_ENV_FILE}" "connector environment"
    require_file "${PARSER_ENV_FILE}" "parser environment"
    require_file "${MODEL_ENV_FILE}" "model environment"

    local local_source_path
    local_source_path="$(
        sed -n 's/^LOCAL_SOURCE_PATH=//p' \
            "${ROOT_DIR}/${CONNECTOR_ENV_FILE}" | tail -n 1
    )"
    local_source_path="${local_source_path:-docs}"
    if [[ "${local_source_path}" == /* ]]; then
        HARBORRAG_LOCAL_SOURCE_DIR="${local_source_path}"
    else
        HARBORRAG_LOCAL_SOURCE_DIR="${ROOT_DIR}/${local_source_path#./}"
    fi
    [[ -d "${HARBORRAG_LOCAL_SOURCE_DIR}" ]] ||
        fail "Local connector source directory does not exist: ${HARBORRAG_LOCAL_SOURCE_DIR}"
    export HARBORRAG_LOCAL_SOURCE_DIR

    if ! docker volume inspect harborrag-model-cache >/dev/null 2>&1; then
        docker volume create harborrag-model-cache >/dev/null
    fi
}

worker_replicas() {
    local replicas
    if [[ -v HARBORRAG_TEMPORAL_WORKER_REPLICAS ]]; then
        replicas="${HARBORRAG_TEMPORAL_WORKER_REPLICAS}"
    else
        replicas="$(
            sed -n 's/^HARBORRAG_TEMPORAL_WORKER_REPLICAS=//p' \
                "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" | tail -n 1
        )"
    fi
    replicas="${replicas:-2}"
    [[ "${replicas}" =~ ^[1-9][0-9]*$ ]] ||
        fail "HARBORRAG_TEMPORAL_WORKER_REPLICAS must be a positive integer."
    echo "${replicas}"
}

start_data() {
    echo "Starting HarborRAG data services..."
    data_compose config --quiet
    data_compose up --detach
}

start_temporal() {
    require_data_network
    echo "Starting Temporal server services (without worker)..."
    temporal_compose config --quiet
    temporal_compose up --detach temporal-schema temporal
    require_temporal_server
    temporal_compose up --detach temporal-namespace temporal-ui
}

start_worker() {
    local rebuild="${1:-0}"
    require_data_network
    require_temporal_server
    prepare_worker_mount
    local replicas
    replicas="$(worker_replicas)"
    local -a build_args=(--no-build)
    if ((rebuild)) || ! docker image inspect "${TEMPORAL_WORKER_IMAGE}" >/dev/null 2>&1; then
        build_args=(--build)
        echo "Building Temporal ingestion worker image..."
    else
        echo "Reusing local worker image ${TEMPORAL_WORKER_IMAGE}."
    fi
    echo "Starting Temporal ingestion worker (${replicas} replica(s))..."
    temporal_compose --profile worker config --quiet
    temporal_compose --profile worker up \
        "${build_args[@]}" \
        --detach \
        --no-deps \
        --scale "temporal-worker=${replicas}" \
        temporal-worker
}

start_api() {
    local rebuild="${1:-0}"
    require_data_network
    require_temporal_server
    [[ "${API_STARTUP_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] ||
        fail "HARBORRAG_API_STARTUP_TIMEOUT must be a positive integer."

    api_compose config --quiet
    mapfile -t compose_services < <(api_compose config --services)
    if [[ "${#compose_services[@]}" -ne 1 || "${compose_services[0]:-}" != "api" ]]; then
        fail "API Compose configuration must contain only the api service."
    fi
    local -a build_args=(--no-build)
    if ((rebuild)) || ! docker image inspect "${API_IMAGE}" >/dev/null 2>&1; then
        build_args=(--build)
        echo "Building HarborRAG API image..."
    else
        echo "Reusing local API image ${API_IMAGE}."
    fi
    echo "Starting HarborRAG API (without dependencies)..."
    api_compose up \
        "${build_args[@]}" \
        --detach \
        --no-deps \
        --wait \
        --wait-timeout "${API_STARTUP_TIMEOUT}" \
        api

    local api_port
    api_port="$(
        sed -n 's/^HARBORRAG_API_PORT=//p' \
            "${ROOT_DIR}/${API_ENV_FILE}" | tail -n 1
    )"
    api_port="${api_port:-8000}"
    echo "HarborRAG API is healthy at http://127.0.0.1:${api_port}/api/v1/health"
    echo "Prometheus metrics: http://127.0.0.1:${api_port}/metrics"
    echo "Swagger UI: http://127.0.0.1:${api_port}/docs (when enabled)"
}

stop_stack() {
    local -a down_args=(down)
    if [[ "${1:-}" == "--volumes" ]]; then
        down_args+=(--volumes)
        shift
    fi
    [[ "$#" -eq 0 ]] || fail "Unknown down option: $1"

    echo "Stopping HarborRAG API..."
    if [[ -f "${ROOT_DIR}/${DATABASE_ENV_FILE}" && -f "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" && -f "${ROOT_DIR}/${CONNECTOR_ENV_FILE}" && -f "${ROOT_DIR}/${API_ENV_FILE}" ]]; then
        api_compose "${down_args[@]}"
    else
        echo "Skipping API teardown because environment files are missing." >&2
    fi

    echo "Stopping Temporal and worker services..."
    if [[ -f "${ROOT_DIR}/${DATABASE_ENV_FILE}" && -f "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" ]]; then
        temporal_compose --profile worker "${down_args[@]}"
    else
        echo "Skipping Temporal teardown because environment files are missing." >&2
    fi

    echo "Stopping HarborRAG data services..."
    if [[ -f "${ROOT_DIR}/${DATABASE_ENV_FILE}" ]]; then
        data_compose "${down_args[@]}"
    else
        echo "Skipping data-service teardown because the environment file is missing." >&2
    fi
}

global_rebuild=0
while [[ "${1:-}" == "--build" ]]; do
    global_rebuild=1
    shift
done

command="${1:-}"
[[ -n "${command}" ]] || {
    usage
    exit 2
}
shift

if ((global_rebuild)) && [[ ! "${command}" =~ ^(up|worker|api)$ ]]; then
    fail "--build is supported only with up, worker, or api."
fi

case "${command}" in
    bootstrap)
        [[ "$#" -eq 0 ]] || fail "bootstrap accepts no options."
        bootstrap_environment
        if ((created_environment)); then
            echo "Review the new environment files and replace placeholder credentials."
        else
            echo "Development environment files already exist."
        fi
        ;;
    up)
        start_worker_flag=1
        rebuild_images="${global_rebuild}"
        while [[ "$#" -gt 0 ]]; do
            case "$1" in
                --no-worker) start_worker_flag=0 ;;
                --build) rebuild_images=1 ;;
                *) fail "Unknown up option: $1" ;;
            esac
            shift
        done
        bootstrap_environment
        if ((created_environment)); then
            fail "Review the new environment files, then run '$0 up' again."
        fi
        start_data
        start_temporal
        if ((start_worker_flag)); then
            start_worker "${rebuild_images}"
        fi
        start_api "${rebuild_images}"
        ;;
    down)
        stop_stack "$@"
        ;;
    data)
        [[ "$#" -eq 0 ]] || fail "data accepts no options."
        start_data
        ;;
    temporal)
        [[ "$#" -eq 0 ]] || fail "temporal accepts no options."
        start_temporal
        ;;
    worker)
        rebuild_image="${global_rebuild}"
        if [[ "${1:-}" == "--build" ]]; then
            rebuild_image=1
            shift
        fi
        [[ "$#" -eq 0 ]] || fail "Unknown worker option: $1"
        start_worker "${rebuild_image}"
        ;;
    api)
        rebuild_image="${global_rebuild}"
        if [[ "${1:-}" == "--build" ]]; then
            rebuild_image=1
            shift
        fi
        [[ "$#" -eq 0 ]] || fail "Unknown api option: $1"
        start_api "${rebuild_image}"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        fail "Unknown command: ${command}"
        ;;
esac
