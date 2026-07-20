#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

: "${DATABASE_ENV_FILE:?Set DATABASE_ENV_FILE, for example: env/.env.database.dev}"

docker compose \
  --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
  --file "${ROOT_DIR}/deploy/compose/docker-compose.database.yml" \
  up -d