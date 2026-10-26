"""Wire the MCP reader-key verifier to the control-plane database.

The factory never runs migrations: the API owns the schema, and the MCP server
connects with a read-only role that could not migrate anyway. A missing
``mcp_api_keys`` table therefore surfaces as ``AuthStoreUnavailable`` on the
first request, which is never answered with data (HTTP 500 on ``/mcp``), rather
than as a startup crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_engine.security import ApiKeyVerificationService

if TYPE_CHECKING:
    from harborrag_runtime.config.mcp_auth_settings import McpAuthSettings

# Slow DB => fail fast with a clear error rather than hanging an MCP request.
_AUTH_DB_TIMEOUT_SECONDS = 2


def build_api_key_verification(settings: McpAuthSettings) -> ApiKeyVerificationService:
    """SqlApiKeyReader -> ApiKeyVerificationService, ready for the MCP server to wrap.

    The engine is the verifier's own: a small pre-pinged pool and short timeouts,
    so authentication neither queues behind other traffic nor hangs on a dead DB.
    """

    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
        SqlApiKeyReader,
    )

    engine = create_control_plane_engine(
        settings.read_database_dsn(),
        pool_size=5,
        pool_pre_ping=True,
        pool_timeout=_AUTH_DB_TIMEOUT_SECONDS,
        connect_args={
            "timeout": _AUTH_DB_TIMEOUT_SECONDS,
            "command_timeout": _AUTH_DB_TIMEOUT_SECONDS,
        },
    )
    reader = SqlApiKeyReader(create_session_factory(engine))
    return ApiKeyVerificationService(reader, environment=settings.environment)


__all__ = ["build_api_key_verification"]
