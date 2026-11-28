"""Wire the MCP reader-key verifier to the control-plane database.

The factory never runs migrations, because the API owns the schema. A missing
``mcp_api_keys`` table therefore surfaces as ``AuthStoreUnavailable`` on the
first request (denied) rather than as a startup crash. The connection uses the
role in ``HARBORRAG_CONTROL_DB_URL``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_engine.security import ApiKeyVerificationService

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


def build_api_key_verification(settings: RuntimeSettings) -> ApiKeyVerificationService:
    """The MCP server's verifier: one indexed SELECT per request, misses kept for seconds."""

    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
        SqlApiKeyReader,
    )

    # A small pool of its own: authentication must not queue behind the reader
    # tools' connections, and a dead connection must fail fast, not hang a request.
    engine = create_control_plane_engine(
        settings.control_db_url.get_secret_value(),
        pool_size=2,
        max_overflow=3,
        pool_pre_ping=True,
    )
    reader = SqlApiKeyReader(create_session_factory(engine))
    return ApiKeyVerificationService(reader, environment=settings.env)
