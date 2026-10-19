#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=common/mcp-launcher.sh
source "${ROOT_DIR}/scripts/deployment/common/mcp-launcher.sh"

MCP_LAUNCHER="scripts/deployment/mcp.sh"
MCP_SERVICE="mcp"
MCP_COMPOSE_FILE="docker-compose.mcp.yml"
MCP_SERVER_LABEL="MCP HTTP server"
MCP_HTTP_SETTINGS="HARBORRAG_MCP_HOST/PORT/PATH"

usage() {
    cat <<'EOF'
Usage: scripts/deployment/mcp.sh [--build] [MODE] [OPTION...]

Modes (all run the MCP server in the harborrag-mcp Docker container):
  (default)          Stdio server for an MCP client; stdin/stdout must be pipes
  --check [OPTION...]
                     Perform an MCP handshake and print the advertised tools
  --http             Start the loopback HTTP server and status UI in the
                     background and wait until it is healthy
  down               Stop the background HTTP server
  logs [OPTION...]   Show the background HTTP server logs

Other options are forwarded to the server in stdio and check mode. HTTP host,
port, and path come from env/.env.mcp.

--build rebuilds the MCP image; a missing image is built automatically. Use it
after source, dependency, or baked configuration changes.

Environment file paths can be overridden with DATABASE_ENV_FILE,
MODEL_ENV_FILE, and MCP_ENV_FILE. The data services must already be running
('scripts/deployment/dev.sh data'); the container uses the host network to
reach their loopback ports.
EOF
}

mcp_build_images() {
    build_image "$1" "${MCP_IMAGE}" "MCP" "${MCP_COMPOSE_FILE}" mcp
}

mcp_print_endpoint() {
    local port
    port="$(mcp_env_value HARBORRAG_MCP_PORT)"
    echo "HarborRAG MCP status UI: http://127.0.0.1:${port:-8010}/"
}

mcp_launcher_main "$@"
