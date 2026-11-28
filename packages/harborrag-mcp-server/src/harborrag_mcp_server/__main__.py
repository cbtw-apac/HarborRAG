"""Command-line entry point for the local HarborRAG MCP server."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from harborrag_core.invariants import HarborInvariantError
from harborrag_mcp_server.audit import DEFAULT_AUDIT_PATH, McpAuditLog, prepare_audit_directory
from harborrag_mcp_server.configuration import McpConfigurationStore
from harborrag_mcp_server.configuration.launch import load_launch_environment
from harborrag_mcp_server.preflight import describe_startup_failure, start_and_stop
from harborrag_mcp_server.server import (
    McpServer,
    create_explorer_server,
    create_mcp_server,
    require_explorer_extra,
)
from harborrag_mcp_server.server.api_keys import (
    create_api_key_verifier,
    create_composite_verifier,
    create_postgres_api_key_verifier,
)
from harborrag_mcp_server.server.http import (
    RouteAccess,
    create_api_key_owner_verifier,
    create_local_token_verifier,
    parse_allowed_hosts,
    register_health_route,
    register_http_routes,
    require_acknowledged_bind,
    require_loopback_host_names,
    served_origins,
    validate_http_bind,
    validate_local_http_settings,
)
from harborrag_mcp_server.server.request_limits import (
    CredentialLimiter,
    request_limits_from_environment,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.auth import TokenVerifier

    from harborrag_runtime.config.settings import RuntimeSettings


class _TerminalStream(Protocol):
    def isatty(self) -> bool: ...


logger = logging.getLogger("harborrag.mcp.server.preflight")

_PACKAGED_CONFIG_PATH = Path(__file__).parent / "defaults" / "mcp.yaml"


def _default_config_path() -> str:
    """Prefer an operator file, then the configuration shipped in the wheel."""
    configured = os.environ.get("HARBORRAG_MCP_CONFIG_PATH")
    if configured:
        return configured
    workspace_path = Path("config/mcp/server.yaml")
    if workspace_path.is_file():
        return str(workspace_path)
    return str(_PACKAGED_CONFIG_PATH)


_READER_DEFAULT_PORT = "8010"
_UI_DEFAULT_PORT = "8011"


def _tool_names(registry: McpServer, *, ui: bool = False) -> list[str]:
    if ui:
        from harborrag_mcp_server.server.explorer import explorer_tool_names

        return explorer_tool_names(registry)
    return [spec.name for spec in registry.list_tools()]


def _configure_registry(registry: McpServer, path: str) -> McpConfigurationStore:
    store = McpConfigurationStore.load(
        path=path,
        specs=registry.list_tools(),
        audit=registry.audit,
    )
    registry.configuration = store
    return store


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


def _http_auth(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace, settings: RuntimeSettings
) -> TokenVerifier:
    """Build the token verifier for the configured HTTP authentication mode.

    With reader keys in Postgres, the owner token stays valid beside the keys only
    when it is bound to one explicit tenant, so the status UI can still edit
    configuration. Unset or ``*``, it is refused with a WARNING.
    """
    auth_mode = os.environ.get("HARBORRAG_MCP_AUTH_MODE", "local")
    try:
        require_acknowledged_bind(
            arguments.host,
            environment=settings.env,
            acknowledgement=os.environ.get("HARBORRAG_MCP_ALLOW_INSECURE_BIND"),
        )
        if auth_mode == "api_key":
            validate_http_bind(
                host=arguments.host,
                port=arguments.port,
                path=arguments.path,
                allowed_hosts=arguments.allowed_hosts,
            )
            store = os.environ.get("HARBORRAG_MCP_KEY_STORE", "postgres")
            if store == "file":
                return create_api_key_verifier(
                    os.environ.get("HARBORRAG_MCP_KEYS_PATH", "config/mcp/keys.yaml")
                )
            if store == "postgres":
                from harborrag_runtime.composition.mcp_auth import build_api_key_verification

                keys = create_postgres_api_key_verifier(build_api_key_verification(settings))
                owner = create_api_key_owner_verifier(
                    bearer_token=os.environ.get("HARBORRAG_MCP_BEARER_TOKEN"),
                    tenant_id=os.environ.get("HARBORRAG_MCP_READER_TENANT_ID"),
                )
                if owner is None:
                    return keys
                print("MCP owner token enabled alongside reader keys.", file=sys.stderr)
                return create_composite_verifier([owner, keys])
            raise ValueError("HARBORRAG_MCP_KEY_STORE must be postgres or file")
        if auth_mode == "local":
            bearer_token = validate_local_http_settings(
                host=arguments.host,
                port=arguments.port,
                path=arguments.path,
                bearer_token=os.environ.get("HARBORRAG_MCP_BEARER_TOKEN"),
                allowed_hosts=arguments.allowed_hosts,
            )
            require_loopback_host_names(arguments.allowed_hosts)
            return create_local_token_verifier(
                bearer_token, tenant_id=os.environ.get("HARBORRAG_MCP_READER_TENANT_ID", "*")
            )
        raise ValueError("HARBORRAG_MCP_AUTH_MODE must be local or api_key")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


def _prepare_arguments(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace, *, ui: bool = False
) -> None:
    try:
        load_launch_environment(
            checkout_root=arguments.local_stack_root,
            env_files=arguments.env_file,
            check=arguments.check,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if arguments.http:
        arguments.transport = "http"
    if arguments.host is None:
        arguments.host = os.environ.get("HARBORRAG_MCP_HOST", "127.0.0.1")
    if arguments.port is None:
        variable = "HARBORRAG_MCP_UI_PORT" if ui else "HARBORRAG_MCP_PORT"
        default = _UI_DEFAULT_PORT if ui else _READER_DEFAULT_PORT
        try:
            arguments.port = int(os.environ.get(variable, default))
        except ValueError:
            parser.error(f"{variable} must be an integer")
    if arguments.path is None:
        arguments.path = os.environ.get("HARBORRAG_MCP_PATH", "/mcp")
    arguments.allowed_hosts = parse_allowed_hosts(os.environ.get("HARBORRAG_MCP_ALLOWED_HOSTS"))
    try:
        arguments.request_limits = request_limits_from_environment(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    if arguments.config is None:
        arguments.config = _default_config_path()
    arguments.audit_path = Path(os.environ.get("HARBORRAG_MCP_AUDIT_PATH", DEFAULT_AUDIT_PATH))
    try:
        prepare_audit_directory(arguments.audit_path)
    except OSError as exc:
        parser.error(f"HARBORRAG_MCP_AUDIT_PATH: {exc}")


def _register_routes(  # noqa: PLR0913 - the launched server's parts
    transport: FastMCP[Any],
    arguments: argparse.Namespace,
    registry: McpServer,
    configuration: McpConfigurationStore,
    auth: TokenVerifier,
    *,
    ui: bool,
) -> None:
    """Attach the HTTP side routes and announce where the server listens.

    The Explorer UI server gets a health route only: the status page and the
    configuration API stay on the reader server, so one process owns writes to
    the configuration file.
    """
    limiter = CredentialLimiter(arguments.request_limits)
    if ui:
        register_health_route(
            transport,
            mcp_path=arguments.path,
            tool_names=_tool_names(registry, ui=True),
            service="harborrag-mcp-ui",
            limiter=limiter,
        )
    else:
        register_http_routes(
            transport,
            mcp_path=arguments.path,
            registry=registry,
            configuration=configuration,
            access=RouteAccess(auth, limiter),
        )
    for line in _announcement(arguments, ui=ui):
        print(line, file=sys.stderr)


def _announcement(arguments: argparse.Namespace, *, ui: bool) -> list[str]:
    """Return the start-up lines that say where the server listens.

    ``0.0.0.0`` and ``::`` are not addresses a client can open, and the process
    cannot know which address a container or proxy publishes for it. For such a
    bind the line names the port and the paths only.
    """
    service = "HarborRAG Explorer MCP" if ui else "HarborRAG MCP"
    routes = {"endpoint": arguments.path} if ui else {"UI": "/", "endpoint": arguments.path}
    base = _listen_url(arguments.host, arguments.port)
    if base is None:
        paths = ", ".join(f"{name} at {path}" for name, path in routes.items())
        return [
            f"{service} listens on every interface, port {arguments.port} ({paths}). "
            "Open it at the address this port is published on."
        ]
    return [f"{service} {name}: {base}{path}" for name, path in routes.items()]


def _listen_url(host: str, port: int) -> str | None:
    """Return the base URL of a bind address, or None when it names every interface."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"http://{host}:{port}"
    if address.is_unspecified:
        return None
    return f"http://[{address}]:{port}" if address.version == 6 else f"http://{address}:{port}"


@dataclass(frozen=True, slots=True)
class _ComposedServer:
    """A server wired to its runtime and not yet started.

    Attributes:
        transport: FastMCP server whose lifespan starts and stops the runtime.
        registry: Audited tool registry behind the transport.
        configuration: Tool configuration the registry enforces.
        auth: Token verifier for the HTTP transport, or ``None`` on stdio.
        settings: Runtime settings the server was composed from.
    """

    transport: FastMCP[Any]
    registry: McpServer
    configuration: McpConfigurationStore
    auth: TokenVerifier | None
    settings: RuntimeSettings


def _compose_server(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    factory: Callable[..., object],
) -> _ComposedServer:
    """Build the runtime, registry and transport that serving and the preflight share."""
    from harborrag_runtime.composition.readers import open_reader_application
    from harborrag_runtime.config.settings import RuntimeSettings

    settings = RuntimeSettings()
    auth = _http_auth(parser, arguments, settings) if arguments.transport == "http" else None
    runtime = open_reader_application(settings)
    registry = McpServer(
        invoker=runtime.invoker,
        references=runtime.references,
        corpus_mode=settings.corpus_access_mode,
        shared_tenant_id=settings.corpus_shared_tenant_id,
        config_provider=runtime.config_provider,
        known_tenants=(settings.tenant_id,),
        tenant_directory=runtime.tenant_directory,
        audit=McpAuditLog(path=arguments.audit_path),
    )
    configuration = _configure_registry(registry, arguments.config)
    transport = cast(
        "FastMCP[Any]",
        factory(
            registry=registry,
            runtime=runtime,
            auth=auth,
            allow_unauthenticated_local=arguments.transport == "stdio",
            manage_runtime_lifecycle=True,
        ),
    )
    return _ComposedServer(transport, registry, configuration, auth, settings)


def _key_store_probe(
    arguments: argparse.Namespace, settings: RuntimeSettings
) -> Callable[[], Awaitable[object]] | None:
    """Return one lookup of a key nobody holds, or ``None`` when keys are not in Postgres.

    The lookup raises ``AuthStoreUnavailable`` when the key table cannot be read,
    which the running server would only log while it denies every key.
    """
    if arguments.transport != "http":
        return None
    if os.environ.get("HARBORRAG_MCP_AUTH_MODE", "local") != "api_key":
        return None
    if os.environ.get("HARBORRAG_MCP_KEY_STORE", "postgres") != "postgres":
        return None
    from harborrag_core.security.api_keys import generate_key
    from harborrag_runtime.composition.mcp_auth import build_api_key_verification

    service = build_api_key_verification(settings)
    return lambda: service.verify_key(generate_key(settings.env).raw_key)


def _preflight(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    factory: Callable[..., object],
) -> int:
    """Start and stop the composed server once and say on stderr whether it worked."""
    try:
        server = _compose_server(parser, arguments, factory)
        asyncio.run(
            start_and_stop(
                server.transport,
                key_store_probe=_key_store_probe(arguments, server.settings),
            )
        )
    except Exception as exc:  # noqa: BLE001 - the preflight's contract is one line and a status
        logger.debug("mcp_preflight_failed", exc_info=True)
        print(f"HarborRAG MCP preflight failed: {describe_startup_failure(exc)}", file=sys.stderr)
        return 1
    print("HarborRAG MCP preflight passed.", file=sys.stderr)
    return 0


def ui_main(argv: Sequence[str] | None = None) -> int:
    """Start the separate HarborRAG Explorer MCP UI server."""
    return main(argv, ui=True)


def main(argv: Sequence[str] | None = None, *, ui: bool = False) -> int:
    """Start the reader MCP server, or with ``ui`` the Explorer MCP UI server."""
    factory = create_explorer_server if ui else create_mcp_server
    parser = argparse.ArgumentParser(
        prog="harborrag-mcp-ui" if ui else "harborrag-mcp",
        description=(
            "Start the HarborRAG Explorer MCP UI server."
            if ui
            else "Start the HarborRAG MCP server."
        ),
    )
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=[],
        help="Load an environment file as data; may be repeated (process env wins).",
    )
    parser.add_argument(
        "--local-stack-root",
        type=Path,
        help="Use checkout env files and local Compose backend addresses from this root.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Perform an MCP handshake and print the advertised tool names.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Connect every backend once, report a failure in one line, and exit.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument("--http", action="store_true", help="Shortcut for --transport http.")
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP bind host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"HTTP bind port (default: {_UI_DEFAULT_PORT if ui else _READER_DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Streamable HTTP endpoint path (default: /mcp).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="MCP tool configuration path (default: workspace or packaged configuration).",
    )
    arguments = parser.parse_args(argv)
    if ui:
        try:
            require_explorer_extra()
        except RuntimeError as exc:
            parser.error(str(exc))
    _prepare_arguments(parser, arguments, ui=ui)
    if not (arguments.check or arguments.preflight) and arguments.transport == "stdio":
        _reject_interactive_stdio(parser, sys.stdin)
    if arguments.check:
        registry = McpServer(audit=McpAuditLog(path=arguments.audit_path))
        _configure_registry(registry, arguments.config)
        transport = cast(
            "FastMCP[Any]",
            factory(
                registry=registry,
                allow_unauthenticated_local=True,
            ),
        )
        advertised_tools = asyncio.run(_check_protocol(transport))
        expected_tools = _tool_names(registry, ui=ui)
        if advertised_tools != expected_tools:
            parser.error("MCP transport advertised a different tool registry")
        print(json.dumps(advertised_tools))
        return 0
    if arguments.preflight:
        return _preflight(parser, arguments, factory)
    server = _compose_server(parser, arguments, factory)
    transport = server.transport
    try:
        if arguments.transport == "http":
            if server.auth is None:
                raise HarborInvariantError("auth must not be None here")
            _register_routes(
                transport, arguments, server.registry, server.configuration, server.auth, ui=ui
            )
            transport.run(
                transport="http",
                host=arguments.host,
                port=arguments.port,
                path=arguments.path,
                show_banner=False,
                host_origin_protection=True if arguments.allowed_hosts else "auto",
                allowed_hosts=list(arguments.allowed_hosts) or None,
                allowed_origins=list(served_origins(arguments.allowed_hosts)) or None,
            )
        else:
            transport.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        print("HarborRAG MCP server stopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
