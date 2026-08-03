#!/usr/bin/env bash
# Starts the real HarborRAG MCP server over HTTP (streamable-http) so it has
# an address a separate client can connect to while it keeps running. The
# stdio transport documented in the README's "Run the local MCP stdio
# transport" section does not work for that split: its stdin/stdout is tied
# 1:1 to whichever single process launches it, so no second script can
# attach to an already-running stdio instance later.
#
# SECURITY: allow_unauthenticated_local=True skips the auth provider the
# transport otherwise requires (see
# harborrag_mcp_server.server.create_mcp_server). The README restricts that
# override to local stdio, which opens no network listener. Running it over
# HTTP here is a local development/testing convenience only. Loopback-only
# validation is enforced in create_mcp_server when unauthenticated mode is
# enabled.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

HARBORRAG_MCP_HOST="${HARBORRAG_MCP_HOST:-127.0.0.1}"
HARBORRAG_MCP_PORT="${HARBORRAG_MCP_PORT:-8765}"

echo "Starting the HarborRAG MCP server (unauthenticated, local-only) on http://${HARBORRAG_MCP_HOST}:${HARBORRAG_MCP_PORT}/mcp"

HARBORRAG_MCP_HOST="${HARBORRAG_MCP_HOST}" \
HARBORRAG_MCP_PORT="${HARBORRAG_MCP_PORT}" \
exec uv run python -c "
import os
from harborrag_mcp_server import create_mcp_server

server = create_mcp_server(
  host=os.environ['HARBORRAG_MCP_HOST'],
  allow_unauthenticated_local=True,
)
server.run(
    transport='http',
    host=os.environ['HARBORRAG_MCP_HOST'],
    port=int(os.environ['HARBORRAG_MCP_PORT']),
)
"
