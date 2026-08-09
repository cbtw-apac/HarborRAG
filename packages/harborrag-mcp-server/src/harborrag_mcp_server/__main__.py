"""Command-line entry point for the local HarborRAG MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from harborrag_core.invariants import HarborInvariantError
from harborrag_mcp_server.configuration import McpConfigurationStore
from harborrag_mcp_server.server import McpServer, create_mcp_server
from harborrag_mcp_server.server.http import (
    create_local_token_verifier,
    register_http_routes,
    validate_local_http_settings,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from harborrag_core.ports.conversation import ConversationRepository
    from harborrag_runtime.config.settings import RuntimeSettings


class _TerminalStream(Protocol):
    def isatty(self) -> bool: ...


_PACKAGED_CONFIG_PATH = Path(__file__).parent / "defaults" / "mcp.yaml"


def _default_config_path() -> str:
    """Prefer an operator file, then the configuration shipped in the wheel."""

    configured = os.environ.get("HARBORRAG_MCP_CONFIG_PATH")
    if configured:
        return configured
    workspace_path = Path("config/mcp.yaml")
    if workspace_path.is_file():
        return str(workspace_path)
    return str(_PACKAGED_CONFIG_PATH)


def _tool_names(registry: McpServer) -> list[str]:
    return [spec.name for spec in registry.list_tools()]


def _configure_registry(registry: McpServer, path: str) -> McpConfigurationStore:
    store = McpConfigurationStore.load(
        path=path,
        specs=registry.list_tools(),
        audit=registry.audit,
    )
    registry.configuration = store
    return store


def _configured_memory(settings: RuntimeSettings) -> ConversationRepository:
    from harborrag_runtime.memory import DatabaseConversationMemory

    return DatabaseConversationMemory.configured(settings)


async def _check_protocol(transport: FastMCP[Any]) -> list[str]:
    """Open a real in-memory MCP session and return its advertised tools."""
    from fastmcp import Client

    async with Client(transport) as client:
        return [tool.name for tool in await client.list_tools()]


def _reject_interactive_stdio(parser: argparse.ArgumentParser, stdin: _TerminalStream) -> None:
    if not stdin.isatty():
        return
    parser.error(
        "the stdio server must be launched by an MCP client; "
        "run with --check to verify it from a terminal"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the HarborRAG MCP server.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Perform an MCP handshake and print the advertised tool names.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HARBORRAG_MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("HARBORRAG_MCP_PORT", "8010")),
        help="HTTP bind port (default: 8010).",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("HARBORRAG_MCP_PATH", "/mcp"),
        help="Streamable HTTP endpoint path (default: /mcp).",
    )
    parser.add_argument(
        "--config",
        default=_default_config_path(),
        help="MCP tool configuration path (default: workspace or packaged configuration).",
    )
    arguments = parser.parse_args(argv)
    if not arguments.check and arguments.transport == "stdio":
        _reject_interactive_stdio(parser, sys.stdin)
    if arguments.check:
        registry = McpServer()
        _configure_registry(registry, arguments.config)
        transport = cast(
            "FastMCP[Any]",
            create_mcp_server(
                registry=registry,
                allow_unauthenticated_local=True,
            ),
        )
        advertised_tools = asyncio.run(_check_protocol(transport))
        expected_tools = _tool_names(registry)
        if advertised_tools != expected_tools:
            parser.error("MCP transport advertised a different tool registry")
        print(json.dumps(advertised_tools))
        return 0
    if arguments.transport == "http":
        try:
            bearer_token = validate_local_http_settings(
                host=arguments.host,
                port=arguments.port,
                path=arguments.path,
                bearer_token=os.environ.get("HARBORRAG_MCP_BEARER_TOKEN"),
            )
        except ValueError as exc:
            parser.error(str(exc))
        auth = create_local_token_verifier(bearer_token)
    else:
        auth = None
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

    settings = RuntimeSettings()
    runtime = HarborRAG(HarborRAGConfig(runtime=settings))
    memory = _configured_memory(settings)
    registry = McpServer(runtime=runtime, memory=memory)
    configuration = _configure_registry(registry, arguments.config)
    transport = cast(
        "FastMCP[Any]",
        create_mcp_server(
            registry=registry,
            runtime=runtime,
            auth=auth,
            allow_unauthenticated_local=arguments.transport == "stdio",
            manage_runtime_lifecycle=True,
        ),
    )
    try:
        if arguments.transport == "http":
            if auth is None:
                raise HarborInvariantError("auth must not be None here")
            register_http_routes(
                transport,
                mcp_path=arguments.path,
                registry=registry,
                configuration=configuration,
                token_verifier=auth,
            )
            print(
                f"HarborRAG MCP UI: http://{arguments.host}:{arguments.port}/",
                file=sys.stderr,
            )
            print(
                f"HarborRAG MCP endpoint: http://{arguments.host}:{arguments.port}{arguments.path}",
                file=sys.stderr,
            )
            transport.run(
                transport="http",
                host=arguments.host,
                port=arguments.port,
                path=arguments.path,
                show_banner=False,
            )
        else:
            transport.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        print("HarborRAG MCP server stopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
