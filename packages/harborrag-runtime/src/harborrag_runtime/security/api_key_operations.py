"""Operator entry points for MCP reader keys, one coroutine per CLI command.

Each call opens the control-plane database with the operator's own credentials,
runs one management operation and closes the engine again. Nothing here prints
or logs a raw key: ``create_key`` returns it once inside ``CreatedKey`` and the
CLI is responsible for delivering it to a file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from harborrag_engine.security import ApiKeyManagementService, CreatedKey

KEY_TABLE = "mcp_api_keys"


class ApiKeySchemaMissing(RuntimeError):
    """The control database has not been migrated to the key table yet."""

    def __init__(self, target: str | None = None) -> None:
        where = f" in {target}" if target else ""
        super().__init__(
            f"the {KEY_TABLE} table does not exist{where}: start the API once (it applies "
            "the control-plane migrations) or re-run with --migrate; then provision the "
            "MCP server's database role with 'scripts/deployment/dev.sh mcp-role'"
        )


def describe_target(dsn: str) -> str:
    """``scheme://host:port/database`` with credentials dropped, for messages."""

    from urllib.parse import urlsplit

    parts = urlsplit(dsn)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


if TYPE_CHECKING:
    from harborrag_core.ports.api_keys import ApiKeyRecord
    from harborrag_runtime.config.settings import RuntimeSettings


def key_state(record: ApiKeyRecord, *, now: datetime | None = None) -> str:
    """``active``, ``expired`` or ``revoked``; never stored, always derived."""

    if record.revoked_at is not None:
        return "revoked"
    if record.expires_at <= (now or datetime.now(UTC)):
        return "expired"
    return "active"


def describe(record: ApiKeyRecord) -> dict[str, Any]:
    """The safe, operator-facing view of one key: no hash, no secret."""

    return {
        "key_id": record.key_id,
        "tenant_id": record.tenant_id,
        "owner": record.owner,
        "name": record.name,
        "environment": record.environment,
        "state": key_state(record),
        "created_at": record.created_at.isoformat(),
        "created_by": record.created_by,
        "expires_at": record.expires_at.isoformat(),
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "revoked_by": record.revoked_by,
        "revocation_reason": record.revocation_reason,
    }


async def _service(settings: RuntimeSettings) -> tuple[ApiKeyManagementService, Any]:
    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
        SqlApiKeyManager,
    )

    engine = create_control_plane_engine(settings.control_db_url.get_secret_value())
    manager = SqlApiKeyManager(create_session_factory(engine))
    return ApiKeyManagementService(manager, environment=settings.env), engine


async def ensure_key_schema(settings: RuntimeSettings, *, migrate: bool) -> bool:
    """Check for the key table; with ``migrate`` apply pending migrations instead.

    Returns True when migrations were applied. Migrations are the API's job in
    normal operation; this is the operator's shortcut on a stack whose API has
    not started yet, run with the same owner credentials the API would use.
    """

    from sqlalchemy import inspect

    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
    )
    from harborrag_adapters.repositories.database.control_plane.migrations import (
        run_migrations,
    )

    dsn = settings.control_db_url.get_secret_value()
    engine = create_control_plane_engine(dsn)
    try:
        async with engine.connect() as connection:
            present = await connection.run_sync(lambda sync: inspect(sync).has_table(KEY_TABLE))
    finally:
        await engine.dispose()
    if present:
        return False
    if not migrate:
        raise ApiKeySchemaMissing(describe_target(dsn))
    run_migrations(dsn)
    return True


async def create_key(  # noqa: PLR0913 - one argument per CLI option
    settings: RuntimeSettings,
    *,
    operator: str,
    tenant_id: str,
    owner: str,
    name: str,
    lifetime: timedelta,
) -> CreatedKey:
    service, engine = await _service(settings)
    try:
        return await service.create_key(
            operator=operator, tenant_id=tenant_id, owner=owner, name=name, lifetime=lifetime
        )
    finally:
        await engine.dispose()


async def list_keys(settings: RuntimeSettings, tenant_id: str) -> list[dict[str, Any]]:
    service, engine = await _service(settings)
    try:
        return [describe(record) for record in await service.list_keys(tenant_id)]
    finally:
        await engine.dispose()


async def revoke_key(settings: RuntimeSettings, *, operator: str, key_id: str, reason: str) -> bool:
    service, engine = await _service(settings)
    try:
        return await service.revoke_key(operator=operator, key_id=key_id, reason=reason)
    finally:
        await engine.dispose()


async def revoke_owner(
    settings: RuntimeSettings, *, operator: str, tenant_id: str, owner: str, reason: str
) -> list[str]:
    service, engine = await _service(settings)
    try:
        return await service.revoke_owner(
            operator=operator, tenant_id=tenant_id, owner=owner, reason=reason
        )
    finally:
        await engine.dispose()


__all__ = [
    "ApiKeySchemaMissing",
    "create_key",
    "describe",
    "describe_target",
    "ensure_key_schema",
    "key_state",
    "list_keys",
    "revoke_key",
    "revoke_owner",
]
