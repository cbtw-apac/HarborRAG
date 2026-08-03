#!/usr/bin/env bash
# Starts the local data + Temporal stack, submits one real ingestion run
# through the existing `harborrag ingest start` CLI, then starts the real
# MCP server standalone and checks harborrag_health_check against it.
#
# Every step below reuses an existing script or CLI command from this repo:
#   - database_up.sh starts Postgres/Qdrant/FalkorDB/Redis and this script
#     only polls the health checks already defined on those containers.
#   - temporal_up.sh starts Temporal and a worker.
#   - `harborrag doctor` / `harborrag ingest start` are the real,
#     Temporal-backed CLI commands documented in the README.
#   - start_mcp_server.sh starts the real MCP server as its own process;
#     check_mcp_server_health.py connects to it separately as a client and
#     calls harborrag_health_check. This script only backgrounds the first
#     and stops it once the second finishes.
# Nothing here re-implements those checks; it only sequences them.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE:-env/.env.temporal}"
HARBORRAG_TEMPORAL_TARGET="${HARBORRAG_TEMPORAL_TARGET:-localhost:7233}"
STARTUP_TIMEOUT="${DATABASE_STARTUP_TIMEOUT:-120}"
TENANT_ID="${HEALTH_CHECK_TENANT:-tenant-1}"
CONNECTOR_NAME="${HEALTH_CHECK_CONNECTOR:-local}"
HARBORRAG_MCP_HOST="${HARBORRAG_MCP_HOST:-127.0.0.1}"
HARBORRAG_MCP_PORT="${HARBORRAG_MCP_PORT:-8765}"

for env_file in "${DATABASE_ENV_FILE}" "${TEMPORAL_ENV_FILE}"; do
  if [[ ! -f "${ROOT_DIR}/${env_file}" ]]; then
    echo "Missing ${env_file}. See README 'Create protected environment files'." >&2
    exit 2
  fi
done

echo "==> [1/4] Starting HarborRAG data services"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE}" "${ROOT_DIR}/scripts/deployment/database_up.sh"

compose_db_args=(
  --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}"
  --file "${ROOT_DIR}/deploy/compose/docker-compose.database.yml"
)

echo "==> Waiting up to ${STARTUP_TIMEOUT}s for data service health checks"
deadline=$((SECONDS + STARTUP_TIMEOUT))
pending=(postgres falkordb redis)
while ((${#pending[@]} > 0)); do
  if ((SECONDS >= deadline)); then
    echo "Timed out waiting for: ${pending[*]}" >&2
    docker compose "${compose_db_args[@]}" ps >&2
    exit 1
  fi
  still_pending=()
  for svc in "${pending[@]}"; do
    health="$(docker compose "${compose_db_args[@]}" ps --format '{{.Health}}' "${svc}" 2>/dev/null || true)"
    [[ "${health}" == "healthy" ]] || still_pending+=("${svc}")
  done
  pending=("${still_pending[@]+"${still_pending[@]}"}")
  ((${#pending[@]} > 0)) && sleep 2
done

echo "==> [2/4] Starting Temporal and a worker"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE}" \
TEMPORAL_ENV_FILE="${TEMPORAL_ENV_FILE}" \
TEMPORAL_START_WORKER=1 \
  "${ROOT_DIR}/scripts/deployment/temporal_up.sh"

echo "==> Waiting up to ${STARTUP_TIMEOUT}s for the Temporal frontend"
deadline=$((SECONDS + STARTUP_TIMEOUT))
until HARBORRAG_TEMPORAL_TARGET="${HARBORRAG_TEMPORAL_TARGET}" \
  uv run --package harborrag-app harborrag doctor >/dev/null 2>&1; do
  if ((SECONDS >= deadline)); then
    echo "Timed out waiting for Temporal at ${HARBORRAG_TEMPORAL_TARGET}" >&2
    exit 1
  fi
  sleep 3
done

echo "==> [3/4] Submitting a real ingestion run (tenant=${TENANT_ID}, connector=${CONNECTOR_NAME})"
HARBORRAG_TEMPORAL_TARGET="${HARBORRAG_TEMPORAL_TARGET}" \
  uv run --package harborrag-app harborrag ingest start \
    --tenant "${TENANT_ID}" \
    --connector "${CONNECTOR_NAME}" \
    --wait

echo "==> [4/4] Starting the MCP server and checking harborrag_health_check"
HARBORRAG_MCP_HOST="${HARBORRAG_MCP_HOST}" \
HARBORRAG_MCP_PORT="${HARBORRAG_MCP_PORT}" \
  "${ROOT_DIR}/scripts/deployment/start_mcp_server.sh" &
mcp_server_pid=$!
trap 'kill "${mcp_server_pid}" 2>/dev/null || true' EXIT

if ! kill -0 "${mcp_server_pid}" 2>/dev/null; then
  echo "The MCP server process exited immediately after startup." >&2
  exit 1
fi

HARBORRAG_MCP_HOST="${HARBORRAG_MCP_HOST}" \
HARBORRAG_MCP_PORT="${HARBORRAG_MCP_PORT}" \
  uv run python "${ROOT_DIR}/scripts/check_mcp_server_health.py" \
    --timeout "${STARTUP_TIMEOUT}" \
    --interval 1

echo "==> All checks passed."
