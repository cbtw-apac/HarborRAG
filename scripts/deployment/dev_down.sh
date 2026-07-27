#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE:-env/.env.temporal}"

# The Temporal compose file interpolates the worker's source mount, so teardown
# needs the same resolved path. A missing directory must not block a stop.
# shellcheck source=scripts/deployment/lib/local_source.sh
source "${ROOT_DIR}/scripts/deployment/lib/local_source.sh"
resolve_local_source_dir "${ROOT_DIR}" 0

api_compose_args=(
    --file "${ROOT_DIR}/deploy/compose/docker-compose.dev.yml"
)
if [[ -f "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" ]]; then
    api_compose_args=(
        --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}"
        "${api_compose_args[@]}"
    )
fi

echo "Stopping the HarborRAG development API..."
docker compose "${api_compose_args[@]}" down "$@"

if [[ -f "${ROOT_DIR}/${DATABASE_ENV_FILE}" && -f "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" ]]; then
    echo "Stopping Temporal services..."
    docker compose \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/docker-compose.temporal.yml" \
        --profile worker \
        down "$@"
else
    echo "Skipping Temporal teardown because its environment files are missing." >&2
fi

if [[ -f "${ROOT_DIR}/${DATABASE_ENV_FILE}" ]]; then
    echo "Stopping HarborRAG data services..."
    docker compose \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/docker-compose.database.yml" \
        down "$@"
else
    echo "Skipping data-service teardown because ${DATABASE_ENV_FILE} is missing." >&2
fi
