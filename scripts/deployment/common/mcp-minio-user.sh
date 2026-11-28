# shellcheck shell=bash
# Creates or refreshes harborrag-mcp, the MinIO user of the MCP server, and attaches
# harborrag-mcp-read, a policy that reads the harborrag-artifacts bucket only.
# 'scripts/deployment/dev.sh mcp-access' runs it inside the MinIO container with the
# policy document on stdin and the user's secret key in MINIO_MCP_PASSWORD. Keys reach
# mc through pipes, never on a command line. Every step is safe to repeat.
set -euo pipefail

MC_CONFIG_DIR="$(mktemp -d)"
export MC_CONFIG_DIR
trap 'rm -rf "${MC_CONFIG_DIR}"' EXIT

cat >"${MC_CONFIG_DIR}/policy.json"
printf '%s\n%s\n' "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" |
    mc alias set harborrag http://127.0.0.1:9000 >/dev/null
mc admin policy create harborrag harborrag-mcp-read "${MC_CONFIG_DIR}/policy.json" >/dev/null
printf '%s\n%s\n' harborrag-mcp "${MINIO_MCP_PASSWORD}" | mc admin user add harborrag >/dev/null
mc admin policy attach harborrag harborrag-mcp-read --user harborrag-mcp >/dev/null
