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
WORKER_DEVICE="cpu"
# Secrets the data stack requires. bootstrap fills any that are empty or absent
# with `openssl rand -hex 32` and never replaces a value that is set; the
# launchers refuse empty or placeholder values. Hex keeps every generated value
# safe inside connection URLs, sed, and Compose interpolation.
DATABASE_GENERATED_SECRETS=(
    POSTGRES_PASSWORD
    MINIO_ROOT_PASSWORD
    MINIO_APP_SECRET_KEY
    QDRANT_API_KEY
    REDIS_PASSWORD
    FALKORDB_PASSWORD
)
# Every `${VAR:?}` the Compose files that `down` loads (database, Temporal, API)
# interpolate. Compose resolves them even for teardown, so `down` gives unset
# ones an inert placeholder; a test derives this list from the Compose files.
DOWN_REQUIRED_VARIABLES=(
    "${DATABASE_GENERATED_SECRETS[@]}"
    POSTGRES_USER
    POSTGRES_DB
    MINIO_ROOT_USER
    HARBORRAG_SECRETS_ENCRYPTION_KEY
)
# Compose embeds these in connection URLs without escaping (MINIO_ROOT_PASSWORD in minio-init's MC_HOST_harbor).
URL_EMBEDDED_SECRETS=(POSTGRES_PASSWORD REDIS_PASSWORD MINIO_ROOT_PASSWORD)

usage() {
    cat <<'EOF'
Usage: scripts/deployment/dev.sh [--build] COMMAND [OPTION...]

Commands:
  bootstrap          Create missing protected env files, then stop for review
  up [--no-worker] [--build] [--device cpu|gpu]
                     Start data, Temporal, worker (default), and API
  down [--volumes]   Stop API, Temporal/worker, and data services
  data               Start only PostgreSQL, Qdrant, FalkorDB, Redis, and MinIO
  temporal           Start only Temporal server services; never starts a worker
  worker [--build] [--device cpu|gpu]
                     Start only the ingestion worker; reuse its image by default
  api [--build]      Start only the API; reuse its image and never start a worker
  token              Print a short-lived owner token (1 hour by default) for the running API

Environment file paths can be overridden with DATABASE_ENV_FILE,
TEMPORAL_ENV_FILE, CONNECTOR_ENV_FILE, PARSER_ENV_FILE, MODEL_ENV_FILE,
API_ENV_FILE, and MCP_ENV_FILE.
Use --build after source, dependency, or baked worker configuration changes.
For up, worker, and api, --build may appear before or after the command.
If a local API or worker image is missing, the first start builds it automatically.

--device selects the ingestion worker's Dockerfile and compose overlay:
cpu (default) uses Dockerfile.temporal-worker; gpu uses the CUDA-enabled
Dockerfile.temporal-worker.gpu and reserves an NVIDIA GPU (requires the
NVIDIA Container Toolkit on the host). Each device builds its own image tag,
so switching devices rebuilds automatically the first time:
  scripts/deployment/dev.sh worker --device gpu --build
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

require_control_plane_encryption_key() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    local configured
    if [[ -v HARBORRAG_SECRETS_ENCRYPTION_KEY ]]; then
        configured="${HARBORRAG_SECRETS_ENCRYPTION_KEY}"
    else
        configured="$(
            sed -n 's/^HARBORRAG_SECRETS_ENCRYPTION_KEY=//p' \
                "${ROOT_DIR}/${DATABASE_ENV_FILE}" | tail -n 1
        )"
    fi
    configured="${configured#"${configured%%[![:space:]]*}"}"
    configured="${configured%"${configured##*[![:space:]]}"}"
    if [[ -z "${configured}" || "${configured}" == '""' || "${configured}" == "''" ]]; then
        fail "HARBORRAG_SECRETS_ENCRYPTION_KEY is required for the API and worker; set it in ${DATABASE_ENV_FILE}."
    fi
}

# The last NAME=value of an env file without evaluating it. Carriage returns,
# surrounding whitespace, and one pair of matching quotes are removed.
env_file_value() {
    local path="$1"
    local name="$2"
    local value
    value="$(sed -En "s/^[[:space:]]*(export[[:space:]]+)?${name}=//p" "${ROOT_DIR}/${path}" | tail -n 1)"
    value="${value//$'\r'/}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "${value}" == \"*\" || "${value}" == \'*\' ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s' "${value}"
}

# Compose lets an exported variable override the --env-file value; match it.
configured_secret() {
    local path="$1"
    local name="$2"
    if [[ -v "${name}" ]]; then
        printf '%s' "${!name}"
    else
        env_file_value "${path}" "${name}"
    fi
}

is_placeholder_secret() {
    local value="$1"
    [[ -z "${value}" || "${value}" == change-me* || "${value}" == REPLACE* ]]
}

# Rewrite NAME=... in place without putting the value in any process argument
# (ps would show it): awk reads it from the environment, the copy is created
# under umask 077, and mv keeps the file at mode 600.
replace_env_value() {
    local target_path="$1"
    local name="$2"
    local value="$3"
    local temporary
    temporary="$(umask 077 && mktemp "${target_path}.XXXXXX")"
    if ! ENV_NAME="${name}" ENV_VALUE="${value}" awk '
        BEGIN { pattern = "^[ \t]*(export[ \t]+)?" ENVIRON["ENV_NAME"] "=" }
        $0 ~ pattern { print ENVIRON["ENV_NAME"] "=" ENVIRON["ENV_VALUE"]; next }
        { print }
    ' "${target_path}" >"${temporary}"; then
        rm -f "${temporary}"
        fail "Could not update ${name} in ${target_path}."
    fi
    mv "${temporary}" "${target_path}"
    chmod 600 "${target_path}"
}

generate_missing_secrets() {
    local path="$1"
    shift
    local target_path="${ROOT_DIR}/${path}"
    local name value
    local -a generated=()
    for name in "$@"; do
        [[ -z "$(env_file_value "${path}" "${name}")" ]] || continue
        command -v openssl >/dev/null ||
            fail "OpenSSL is required to generate ${name} in ${path}."
        value="$(openssl rand -hex 32)"
        if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${name}=" "${target_path}"; then
            replace_env_value "${target_path}" "${name}" "${value}"
        else
            if [[ -s "${target_path}" && -n "$(tail -c 1 "${target_path}")" ]]; then
                printf '\n' >>"${target_path}"
            fi
            printf '%s=%s\n' "${name}" "${value}" >>"${target_path}"
        fi
        generated+=("${name}")
    done
    chmod 600 "${target_path}"
    if ((${#generated[@]} > 0)); then
        echo "Generated ${generated[*]} in ${path}."
    fi
}

require_database_secrets() {
    require_file "${DATABASE_ENV_FILE}" "database environment"
    local name
    for name in "${DATABASE_GENERATED_SECRETS[@]}"; do
        if is_placeholder_secret "$(configured_secret "${DATABASE_ENV_FILE}" "${name}")"; then
            fail "${name} in ${DATABASE_ENV_FILE} is empty or a placeholder. Run '$0 bootstrap' to generate missing secrets, or replace the placeholder with a strong value. PostgreSQL keeps the password its volume was created with: change it there with ALTER USER, or discard the data with '$0 down --volumes'."
        fi
    done
    for name in "${URL_EMBEDDED_SECRETS[@]}"; do
        if ! [[ "$(configured_secret "${DATABASE_ENV_FILE}" "${name}")" =~ ^[A-Za-z0-9._~-]+$ ]]; then
            fail "${name} in ${DATABASE_ENV_FILE} contains a character that breaks the connection URL Compose builds from it. Use letters, digits, '-', '_', '.', or '~' (for example 'openssl rand -hex 32')."
        fi
    done
}

# True when a Redis URL carries a password: ":password@" userinfo or a
# password= query parameter.
redis_url_has_password() {
    [[ "$1" =~ ^[A-Za-z][A-Za-z0-9+.-]*://[^/@?]*:[^/@?]+@ || "$1" =~ [?\&]password=[^\&] ]]
}

# An HARBORRAG_REDIS_URL (exported, or in the Temporal env file) overrides the
# Compose default, which carries REDIS_PASSWORD; one without a password fails
# later with NOAUTH.
require_authenticated_worker_redis() {
    require_file "${TEMPORAL_ENV_FILE}" "Temporal environment"
    local url
    url="$(configured_secret "${TEMPORAL_ENV_FILE}" HARBORRAG_REDIS_URL)"
    if [[ -n "${url}" ]] && ! redis_url_has_password "${url}"; then
        fail "HARBORRAG_REDIS_URL (exported or in ${TEMPORAL_ENV_FILE}) has no password, but Redis requires REDIS_PASSWORD. Unset it or delete the line to use the Compose default, or use redis://:PASSWORD@HOST:PORT/DB."
    fi
}

# The Compose files default HARBORRAG_ENV to dev: insecure-remote
# acknowledgements are honoured and HARBORRAG_AUTH_MODE=none is allowed, so the
# production guards are off. Say so once per invocation. The effective value
# follows Compose: an exported value wins, otherwise the last env file that
# sets it, otherwise dev.
development_guard_warned=0
warn_development_guards() {
    ((development_guard_warned)) && return 0
    local value="" path candidate
    if [[ -v HARBORRAG_ENV ]]; then
        value="${HARBORRAG_ENV}"
    else
        for path in "$@"; do
            [[ -f "${ROOT_DIR}/${path}" ]] || continue
            candidate="$(env_file_value "${path}" HARBORRAG_ENV)"
            [[ -z "${candidate}" ]] || value="${candidate}"
        done
    fi
    if [[ "${value:-dev}" == "dev" ]]; then
        development_guard_warned=1
        echo "Warning: this Compose stack runs with HARBORRAG_ENV=dev, so development guards apply; do not expose it beyond this machine." >&2
    fi
}

# The API's env file decides authentication; Compose never overrides it. The
# container reads these values from the file only (an exported shell value does
# not reach it), so the file is the single source checked here.
require_api_authentication() {
    require_file "${API_ENV_FILE}" "API environment"
    local mode
    local secret
    mode="$(env_file_value "${API_ENV_FILE}" HARBORRAG_AUTH_MODE)"
    case "${mode}" in
        hmac)
            secret="$(env_file_value "${API_ENV_FILE}" HARBORRAG_AUTH_SECRET)"
            if is_placeholder_secret "${secret}" || ((${#secret} < 32)); then
                fail "HARBORRAG_AUTH_MODE=hmac needs HARBORRAG_AUTH_SECRET of at least 32 characters in ${API_ENV_FILE}. Run '$0 bootstrap' to generate one."
            fi
            ;;
        none)
            local bind
            bind="$(configured_secret "${API_ENV_FILE}" HARBORRAG_API_BIND_ADDRESS)"
            bind="${bind:-127.0.0.1}"
            bind="${bind#[}"
            bind="${bind%]}"
            if [[ "$(env_file_value "${API_ENV_FILE}" HARBORRAG_ALLOW_INSECURE_DEV)" != "true" ]]; then
                fail "HARBORRAG_AUTH_MODE=none in ${API_ENV_FILE} also needs HARBORRAG_ALLOW_INSECURE_DEV=true, or the API refuses to boot. To enable authentication instead, set HARBORRAG_AUTH_MODE=hmac (bootstrap generates HARBORRAG_AUTH_SECRET)."
            fi
            case "${bind}" in
                127.0.0.1 | localhost | ::1) ;;
                *) fail "HARBORRAG_AUTH_MODE=none is allowed only with a loopback HARBORRAG_API_BIND_ADDRESS (127.0.0.1, localhost or ::1); got '${bind}'." ;;
            esac
            echo "WARNING: API authentication is disabled (HARBORRAG_AUTH_MODE=none). To enable it, set HARBORRAG_AUTH_MODE=hmac in ${API_ENV_FILE}; bootstrap has generated HARBORRAG_AUTH_SECRET." >&2
            ;;
        *)
            fail "Set HARBORRAG_AUTH_MODE=hmac (recommended) or the explicit development opt-in HARBORRAG_AUTH_MODE=none in ${API_ENV_FILE}; got '${mode}'."
            ;;
    esac
}

# One line per problem an existing checkout still has after bootstrap, naming
# keys only: bootstrap never rewrites a value that is set, so older files can
# keep placeholders, disabled authentication, or a passwordless Redis URL.
report_environment_issues() {
    local doc="docs/developers/deployment/README.md"
    local name url
    for name in "${DATABASE_GENERATED_SECRETS[@]}"; do
        if is_placeholder_secret "$(env_file_value "${DATABASE_ENV_FILE}" "${name}")"; then
            echo "NOTICE: ${name} still holds a placeholder in ${DATABASE_ENV_FILE}; see ${doc}."
        fi
    done
    if [[ "$(env_file_value "${API_ENV_FILE}" HARBORRAG_AUTH_MODE)" == "none" ]]; then
        echo "NOTICE: HARBORRAG_AUTH_MODE=none in ${API_ENV_FILE} leaves the API unauthenticated; set HARBORRAG_AUTH_MODE=hmac, see ${doc}."
    fi
    url="$(env_file_value "${TEMPORAL_ENV_FILE}" HARBORRAG_REDIS_URL)"
    if [[ -n "${url}" ]] && ! redis_url_has_password "${url}"; then
        echo "NOTICE: HARBORRAG_REDIS_URL in ${TEMPORAL_ENV_FILE} has no password, so the worker will fail with NOAUTH; remove it or add one, see ${doc}."
    fi
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
    replace_env_value "${target_path}" HARBORRAG_MCP_BEARER_TOKEN "${bearer_token}"
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
    generate_missing_secrets "${DATABASE_ENV_FILE}" "${DATABASE_GENERATED_SECRETS[@]}"
    generate_missing_secrets "${API_ENV_FILE}" HARBORRAG_AUTH_SECRET
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
    local -a compose_files=(
        --file "${ROOT_DIR}/deploy/compose/docker-compose.temporal.yml"
    )
    if [[ "${WORKER_DEVICE}" == "gpu" ]]; then
        compose_files+=(--file "${ROOT_DIR}/deploy/compose/docker-compose.temporal.gpu.yml")
    fi
    docker compose \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" \
        "${compose_files[@]}" \
        "$@"
}

validate_device() {
    [[ "$1" == "cpu" || "$1" == "gpu" ]] || fail "--device must be cpu or gpu, got: $1"
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
    require_database_secrets
    echo "Starting HarborRAG data services..."
    data_compose config --quiet
    data_compose up --detach --scale minio-init=0
    # Compose cannot order services across projects, so the API and worker
    # cannot wait for this one-shot themselves: run it to completion here.
    echo "Provisioning the MinIO buckets and least-privilege application key..."
    data_compose run --rm minio-init
}

start_temporal() {
    require_database_secrets
    require_data_network
    echo "Starting Temporal server services (without worker)..."
    temporal_compose config --quiet
    temporal_compose up --detach temporal-schema temporal
    require_temporal_server
    temporal_compose up --detach temporal-namespace temporal-ui
}

start_worker() {
    local rebuild="${1:-0}"
    warn_development_guards "${DATABASE_ENV_FILE}" "${TEMPORAL_ENV_FILE}"
    require_control_plane_encryption_key
    require_database_secrets
    require_authenticated_worker_redis
    require_data_network
    require_temporal_server
    prepare_worker_mount
    local replicas
    replicas="$(worker_replicas)"
    local worker_image="${TEMPORAL_WORKER_IMAGE}"
    if [[ "${WORKER_DEVICE}" == "gpu" ]]; then
        worker_image="${TEMPORAL_WORKER_IMAGE}-gpu"
    fi
    export HARBORRAG_TEMPORAL_WORKER_IMAGE="${worker_image}"
    local -a build_args=(--no-build)
    if ((rebuild)) || ! docker image inspect "${worker_image}" >/dev/null 2>&1; then
        build_args=(--build)
        echo "Building Temporal ingestion worker image (${WORKER_DEVICE})..."
    else
        echo "Reusing local worker image ${worker_image}."
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
    warn_development_guards "${DATABASE_ENV_FILE}" "${TEMPORAL_ENV_FILE}" \
        "${CONNECTOR_ENV_FILE}" "${MODEL_ENV_FILE}" "${API_ENV_FILE}"
    require_control_plane_encryption_key
    require_database_secrets
    require_api_authentication
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
    echo "Get a bearer token for it with: $0 token"
    echo "Prometheus metrics: http://127.0.0.1:${api_port}/api/v1/metrics (admin token required)"
    echo "Swagger UI: http://127.0.0.1:${api_port}/api/v1/docs (when HARBORRAG_DOCS_ENABLED)"
}

stop_stack() {
    local -a down_args=(down)
    if [[ "${1:-}" == "--volumes" ]]; then
        down_args+=(--volumes)
        shift
    fi
    [[ "$#" -eq 0 ]] || fail "Unknown down option: $1"

    # Compose interpolates every required variable even for `down`, and an older
    # env file may lack the generated secrets. Teardown needs none of their
    # values, so give unset ones an inert placeholder for this process only.
    local secret
    for secret in "${DOWN_REQUIRED_VARIABLES[@]}"; do
        export "${secret}=${!secret:-unused-during-down}"
    done

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
            echo "Review the new environment files: set HARBORRAG_SECRETS_ENCRYPTION_KEY and the model credentials; datastore secrets were generated."
        else
            echo "Development environment files already exist."
        fi
        report_environment_issues
        ;;
    up)
        start_worker_flag=1
        rebuild_images="${global_rebuild}"
        while [[ "$#" -gt 0 ]]; do
            case "$1" in
                --no-worker) start_worker_flag=0 ;;
                --build) rebuild_images=1 ;;
                --device)
                    [[ "$#" -ge 2 ]] || fail "--device requires a value: cpu or gpu."
                    validate_device "$2"
                    WORKER_DEVICE="$2"
                    shift
                    ;;
                *) fail "Unknown up option: $1" ;;
            esac
            shift
        done
        bootstrap_environment
        if ((created_environment)); then
            fail "Review the new environment files, then run '$0 up' again."
        fi
        require_control_plane_encryption_key
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
        while [[ "$#" -gt 0 ]]; do
            case "$1" in
                --build) rebuild_image=1 ;;
                --device)
                    [[ "$#" -ge 2 ]] || fail "--device requires a value: cpu or gpu."
                    validate_device "$2"
                    WORKER_DEVICE="$2"
                    shift
                    ;;
                *) fail "Unknown worker option: $1" ;;
            esac
            shift
        done
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
    token)
        [[ "$#" -eq 0 ]] || fail "token accepts no options."
        require_file "${API_ENV_FILE}" "API environment"
        if [[ "$(env_file_value "${API_ENV_FILE}" HARBORRAG_AUTH_MODE)" != "hmac" ]]; then
            fail "token needs bearer authentication, which is off in ${API_ENV_FILE}: set HARBORRAG_AUTH_MODE=hmac and restart the API with '$0 api'."
        fi
        require_api_authentication
        api_compose exec -T api python -m harborrag_app.api.auth.dev_token
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        fail "Unknown command: ${command}"
        ;;
esac
