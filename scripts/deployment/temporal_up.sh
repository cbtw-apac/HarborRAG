#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE:-env/.env.temporal}"

if [[ ! -f "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" ]]; then
    mkdir -p "$(dirname "${ROOT_DIR}/${TEMPORAL_ENV_FILE}")"
    cp \
        "${ROOT_DIR}/env-example/.env.temporal.example" \
        "${ROOT_DIR}/${TEMPORAL_ENV_FILE}"
    echo "Created ${TEMPORAL_ENV_FILE}; review its local credentials before reuse."
fi

ensure_volume() {
    local volume_name="$1"
    if ! docker volume inspect "${volume_name}" >/dev/null 2>&1; then
        docker volume create "${volume_name}" >/dev/null
    fi
}

ensure_volume harborrag-temporal-postgresql-data

compose_args=(
    --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}"
    --file "${ROOT_DIR}/deploy/compose/docker-compose.temporal.yml"
)
worker_scale_args=()

if [[ -v TEMPORAL_START_WORKER ]]; then
    start_worker="${TEMPORAL_START_WORKER}"
else
    start_worker="$(
        sed -n 's/^TEMPORAL_START_WORKER=//p' \
            "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" | tail -n 1
    )"
fi

if [[ "${start_worker:-0}" == "1" ]]; then
    if ! docker network inspect harborrag-data-network >/dev/null 2>&1; then
        echo "The Temporal worker requires harborrag-data-network." >&2
        echo "Start the database stack first:" >&2
        echo "  DATABASE_ENV_FILE=env/.env.database scripts/deployment/database_up.sh" >&2
        exit 2
    fi
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
