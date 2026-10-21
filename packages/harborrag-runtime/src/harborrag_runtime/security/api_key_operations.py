"""Operator entry points for MCP reader keys, one coroutine per CLI command.

Each call opens the control-plane database with the operator's own credentials,
runs one management operation and closes the engine again. Nothing here prints
or logs a raw key: ``create_key`` returns it once inside ``CreatedKey`` and the
CLI is responsible for delivering it to a file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from harborrag_engine.security import ApiKeyManagementService, CreatedKey

KEY_TABLE = "mcp_api_keys"


class ApiKeySchemaMissing(RuntimeError):
    """The control database has not been migrated to the key table yet."""

    def __init__(self, target: str | None = None) -> None:
        where = f" in {target}" if target else ""
        super().__init__(
            f"the {KEY_TABLE} table does not exist{where}: start the API once (it applies "
            "the control-plane migrations) or re-run with --migrate"
        )


class ApiKeySchemaBehind(RuntimeError):
    """The control database exists but is stamped older than the packaged head."""

    def __init__(self, target: str | None, current: str | None, head: str) -> None:
        where = f" in {target}" if target else ""
        super().__init__(
            f"the control database{where} is at migration {current or 'none'} but this "
            f"release expects {head}: start the API once (it applies the control-plane "
            "migrations) or re-run with --migrate"
        )


class ApiKeyRejected(RuntimeError):
    """The database refused a key row; the SQL error is dropped, it carries bound values."""

    def __init__(self) -> None:
        super().__init__(
            "the database rejected the key row, which usually means its schema is older than "
            "this release: re-run with --migrate or start the API once to apply migrations"
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


async def ensure_key_schema(
    settings: RuntimeSettings, *, migrate: bool, require_head: bool = True
) -> bool:
    """Check the control database; with ``migrate`` apply pending migrations instead.

    Returns True when migrations were applied (the database was behind head).
    Without ``migrate`` nothing is ever changed: a missing key table raises
    ``ApiKeySchemaMissing`` and, when ``require_head``, a database stamped older
    than head raises ``ApiKeySchemaBehind``. ``list`` and ``revoke`` pass
    ``require_head=False``: they only read and update columns every release since
    the key table shipped has, so a database that is merely behind keeps working
    for them, while ``create`` (which writes the newest constraints) insists on head.
    Migrations are the API's job in normal operation; ``--migrate`` is the
    operator's shortcut, run with the same owner credentials the API would use.
    """

    from sqlalchemy import inspect

    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_control_plane_engine,
    )
    from harborrag_adapters.repositories.database.control_plane.migrations import (
        current_revision,
        head_revision,
        run_migrations,
    )

    dsn = settings.control_db_url.get_secret_value()
    head = head_revision(dsn)
    engine = create_control_plane_engine(dsn)
    try:
        async with engine.connect() as connection:
            present = await connection.run_sync(lambda sync: inspect(sync).has_table(KEY_TABLE))
            current = await connection.run_sync(current_revision)
    finally:
        await engine.dispose()
    behind = current != head
    if migrate:
        if behind:
            run_migrations(dsn)
        return behind
    if not present:
        raise ApiKeySchemaMissing(describe_target(dsn))
    if behind and require_head:
        raise ApiKeySchemaBehind(describe_target(dsn), current, head)
    return False


async def create_key(  # noqa: PLR0913 - one argument per CLI option
    settings: RuntimeSettings,
    *,
    operator: str,
    tenant_id: str,
    owner: str,
    name: str,
    lifetime: timedelta,
) -> CreatedKey:
    from sqlalchemy.exc import IntegrityError

    service, engine = await _service(settings)
    try:
        return await service.create_key(
            operator=operator, tenant_id=tenant_id, owner=owner, name=name, lifetime=lifetime
        )
    except IntegrityError:
        raise ApiKeyRejected from None
    finally:
        await engine.dispose()


async def list_keys(settings: RuntimeSettings, tenant_id: str) -> list[dict[str, Any]]:
    service, engine = await _service(settings)
    try:
        return [describe(record) for record in await service.list_keys(tenant_id)]
    finally:
        await engine.dispose()


async def revoke_key(
    settings: RuntimeSettings, *, operator: str, key_id: str, reason: str
) -> Literal["revoked", "already_revoked", "not_found"]:
    from harborrag_adapters.repositories.database.control_plane.engine import (
        create_session_factory,
    )
    from harborrag_adapters.repositories.database.control_plane.mcp_api_keys import (
        SqlApiKeyReader,
    )

    service, engine = await _service(settings)
    try:
        if await service.revoke_key(operator=operator, key_id=key_id, reason=reason):
            return "revoked"
        known = await SqlApiKeyReader(create_session_factory(engine)).get_by_key_id(key_id)
        return "not_found" if known is None else "already_revoked"
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
    "ApiKeyRejected",
    "ApiKeySchemaBehind",
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
