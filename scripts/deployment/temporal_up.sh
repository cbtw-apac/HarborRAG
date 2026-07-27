#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE:-env/.env.temporal}"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"

if [[ ! -f "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" ]]; then
    umask 077
    mkdir -p "$(dirname "${ROOT_DIR}/${TEMPORAL_ENV_FILE}")"
    cp \
        "${ROOT_DIR}/env-example/.env.temporal.example" \
        "${ROOT_DIR}/${TEMPORAL_ENV_FILE}"
    chmod 600 "${ROOT_DIR}/${TEMPORAL_ENV_FILE}"
    echo "Created ${TEMPORAL_ENV_FILE}; review its local credentials before reuse."
fi

if [[ ! -f "${ROOT_DIR}/${DATABASE_ENV_FILE}" ]]; then
    echo "Temporal reuses the deployed database stack but its environment file is missing:" >&2
    echo "  ${DATABASE_ENV_FILE}" >&2
    echo "Create it and start the database stack first:" >&2
    echo "  cp env-example/.env.database.example ${DATABASE_ENV_FILE}" >&2
    echo "  DATABASE_ENV_FILE=${DATABASE_ENV_FILE} scripts/deployment/database_up.sh" >&2
    exit 2
fi

# The worker mounts a host directory at the fixed container path /data/sources.
# The directory itself is configured once in the connector layer, as
# LOCAL_SOURCE_PATH in env/.env.connector, which config/connectors.yaml maps to
# the local connector's source_path. Relative values there resolve from the
# repository root, while a relative bind-mount source would resolve from the
# compose file directory, so export an absolute path for Compose.
local_source_path="$(
    sed -n 's/^LOCAL_SOURCE_PATH=//p' "${ROOT_DIR}/env/.env.connector" 2>/dev/null | tail -n 1
)"
local_source_path="${local_source_path:-docs}"
if [[ "${local_source_path}" == /* ]]; then
    HARBORRAG_LOCAL_SOURCE_DIR="${local_source_path}"
else
    HARBORRAG_LOCAL_SOURCE_DIR="${ROOT_DIR}/${local_source_path#./}"
fi
if [[ ! -d "${HARBORRAG_LOCAL_SOURCE_DIR}" ]]; then
    echo "The local connector source directory does not exist:" >&2
    echo "  ${HARBORRAG_LOCAL_SOURCE_DIR}" >&2
    echo "Create it, or set LOCAL_SOURCE_PATH in env/.env.connector." >&2
    exit 2
fi
export HARBORRAG_LOCAL_SOURCE_DIR

ensure_volume() {
    local volume_name="$1"
    if ! docker volume inspect "${volume_name}" >/dev/null 2>&1; then
        docker volume create "${volume_name}" >/dev/null
    fi
}

compose_args=(
    --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}"
    --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}"
    --file "${ROOT_DIR}/deploy/compose/docker-compose.temporal.yml"
)
worker_scale_args=()

if ! docker network inspect harborrag-data-network >/dev/null 2>&1; then
    echo "Temporal requires the existing PostgreSQL service on harborrag-data-network." >&2
    echo "Start the database stack first:" >&2
    echo "  DATABASE_ENV_FILE=env/.env.database scripts/deployment/database_up.sh" >&2
    exit 2
fi

if [[ -v TEMPORAL_START_WORKER ]]; then
    start_worker="${TEMPORAL_START_WORKER}"
else
    start_worker="$(
        sed -n 's/^TEMPORAL_START_WORKER=//p' \
            "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" | tail -n 1
    )"
fi

if [[ "${start_worker:-0}" == "1" ]]; then
    ensure_volume harborrag-model-cache
    compose_args+=(--profile worker)

    if [[ -v HARBORRAG_TEMPORAL_WORKER_REPLICAS ]]; then
        worker_replicas="${HARBORRAG_TEMPORAL_WORKER_REPLICAS}"
    else
        worker_replicas="$(
            sed -n 's/^HARBORRAG_TEMPORAL_WORKER_REPLICAS=//p' \
                "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" | tail -n 1
        )"
    fi
    worker_replicas="${worker_replicas:-2}"
    if [[ ! "${worker_replicas}" =~ ^[1-9][0-9]*$ ]]; then
        echo "HARBORRAG_TEMPORAL_WORKER_REPLICAS must be a positive integer." >&2
        exit 2
    fi
    worker_scale_args+=(--scale "temporal-worker=${worker_replicas}")
fi

docker compose "${compose_args[@]}" up --build --detach "${worker_scale_args[@]}"
