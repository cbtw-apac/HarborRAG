#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE:-env/.env.temporal}"
DEV_START_WORKER="${HARBORRAG_DEV_START_WORKER:-1}"

created_environment=0

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

ensure_environment_file "${DATABASE_ENV_FILE}" "env-example/.env.database.example"
ensure_environment_file "${TEMPORAL_ENV_FILE}" "env-example/.env.temporal.example"
ensure_environment_file "env/.env.connector" "env-example/.env.connector.example"
ensure_environment_file "env/.env.parser" "env-example/.env.parser.example"
ensure_environment_file "env/.env.models" "env-example/.env.models.example"

if ((created_environment)); then
    echo "Review the new environment files and replace placeholder credentials." >&2
    echo "Run scripts/deployment/dev_up.sh again when configuration is ready." >&2
    exit 2
fi

if [[ "${DEV_START_WORKER}" != "0" && "${DEV_START_WORKER}" != "1" ]]; then
    echo "HARBORRAG_DEV_START_WORKER must be 0 or 1." >&2
    exit 2
fi

echo "Starting HarborRAG data services..."
DATABASE_ENV_FILE="${DATABASE_ENV_FILE}" \
    "${ROOT_DIR}/scripts/deployment/database_up.sh"

echo "Starting Temporal services (worker=${DEV_START_WORKER})..."
DATABASE_ENV_FILE="${DATABASE_ENV_FILE}" \
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE}" \
TEMPORAL_START_WORKER="${DEV_START_WORKER}" \
    "${ROOT_DIR}/scripts/deployment/temporal_up.sh"

echo "Starting the HarborRAG development API..."
docker compose \
    --env-file "${ROOT_DIR}/${TEMPORAL_ENV_FILE}" \
    --file "${ROOT_DIR}/deploy/compose/docker-compose.dev.yml" \
    up --build "$@"
