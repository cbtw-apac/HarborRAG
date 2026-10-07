#!/usr/bin/env bash
set -euo pipefail

# Optional launcher for the HarborRAG Explorer MCP UI server (harborrag-mcp-ui).
# The reader MCP server (scripts/deployment/mcp.sh) never needs it.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=common/mcp-launcher.sh
source "${ROOT_DIR}/scripts/deployment/common/mcp-launcher.sh"

MCP_UI_IMAGE="${HARBORRAG_MCP_UI_IMAGE:-harborrag-mcp-ui}"
export HARBORRAG_MCP_UI_IMAGE="${MCP_UI_IMAGE}"
MCP_LAUNCHER="scripts/deployment/mcp-ui.sh"
MCP_SERVICE="mcp-ui"
MCP_COMPOSE_FILE="docker-compose.mcp-ui.yml"
MCP_SERVER_LABEL="Explorer MCP UI server"
MCP_HTTP_SETTINGS="HARBORRAG_MCP_HOST/UI_PORT/PATH"

usage() {
    cat <<'USAGE'
Usage: scripts/deployment/mcp-ui.sh [--build] [MODE] [OPTION...]

Optional: runs the HarborRAG Explorer MCP UI server, an MCP App for searching,
reading and tracing the corpus, in the harborrag-mcp-ui Docker container. The
reader MCP server (scripts/deployment/mcp.sh) works without it.

Modes:
  (default)          Stdio server for an MCP client; stdin/stdout must be pipes
  --check [OPTION...]
                     Perform an MCP handshake and print the Explorer tools
  --http             Start the loopback HTTP server in the background and wait
                     until it is healthy (port HARBORRAG_MCP_UI_PORT, default 8011)
  down               Stop the background HTTP server
  logs [OPTION...]   Show the background HTTP server logs

Other options are forwarded to harborrag-mcp-ui in stdio and check mode. Host,
path, bearer token and tool configuration are shared with the reader server
through env/.env.mcp and config/mcp.yaml.

--build rebuilds the reader MCP image and the Explorer layer on top of it; a
missing image is built automatically. Use it after source, dependency, or baked
configuration changes.

Environment file paths can be overridden with DATABASE_ENV_FILE,
MODEL_ENV_FILE, and MCP_ENV_FILE. The data services must already be running
('scripts/deployment/dev.sh data').
USAGE
}

# The Explorer image is a layer over the reader image, so that builds first.
mcp_build_images() {
    build_image "$1" "${MCP_IMAGE}" "MCP" docker-compose.mcp.yml mcp
    build_image "$1" "${MCP_UI_IMAGE}" "Explorer MCP UI" "${MCP_COMPOSE_FILE}" mcp-ui
}

mcp_print_endpoint() {
    local port path
    port="$(mcp_env_value HARBORRAG_MCP_UI_PORT)"
    path="$(mcp_env_value HARBORRAG_MCP_PATH)"
    echo "HarborRAG Explorer MCP endpoint: http://127.0.0.1:${port:-8011}${path:-/mcp}"
}

mcp_launcher_main "$@"
