from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.errors import (
    HarborStorageLeaseError,
)
from harborrag_adapters.repositories.state.base import (
    HarborLeaseStore,
)
from harborrag_adapters.repositories.telemetry import (
    traced_repository_operation,
)
from harborrag_core.schemas.state import LeaseRecord
from harborrag_core.schemas.storage import (
    StorageOperationContext,
)

from .sql_base import SQLStoreBase
from .sql_schema import _LEASE_FENCING, _LEASES


class SQLLeaseStore(SQLStoreBase, HarborLeaseStore):
    """Coordinates expiring leases with monotonic fencing tokens."""

    @traced_repository_operation("lease_acquire")
    async def acquire(
        self,
        resource: str,
        owner_token: str,
        lease_duration: timedelta,
        *,
        context: StorageOperationContext,
    ) -> LeaseRecord | None:
        self._validate_duration(lease_duration)
        now = datetime.now(UTC)
        expires_at = now + lease_duration
        tenant_id = str(context.tenant_id)
        try:
            async with self._session() as session:
                # Updating the durable counter first serializes contenders for an
                # existing resource. The counter increment and lease claim share
                # one transaction, so a losing claim rolls the increment back.
                counter_update = await session.execute(
                    sa_update(_LEASE_FENCING)
                    .where(
                        _LEASE_FENCING.c.tenant_id == tenant_id,
                        _LEASE_FENCING.c.resource == resource,
                    )
                    .values(counter=_LEASE_FENCING.c.counter + 1)
                )
                if counter_update.rowcount == 0:
                    await session.execute(
                        sa_insert(_LEASE_FENCING).values(
                            tenant_id=tenant_id,
                            resource=resource,
                            counter=1,
                        )
                    )
                    fencing = 1
                else:
                    fencing = int(
                        (
                            await session.execute(
                                sa_select(_LEASE_FENCING.c.counter).where(
                                    _LEASE_FENCING.c.tenant_id == tenant_id,
                                    _LEASE_FENCING.c.resource == resource,
                                )
                            )
                        ).scalar_one()
                    )

                values = {
                    "owner_token": owner_token,
                    "fencing_token": fencing,
                    "acquired_at": now,
                    "expires_at": expires_at,
                }
                claimed = await session.execute(
                    sa_update(_LEASES)
                    .where(
                        _LEASES.c.tenant_id == tenant_id,
                        _LEASES.c.resource == resource,
                        _LEASES.c.expires_at <= now,
                    )
                    .values(**values)
                )
                if claimed.rowcount != 1:
                    # The primary key makes this an atomic insert-or-fail for an
                    # absent lease. An active or concurrently inserted lease
                    # raises IntegrityError and makes this contender lose.
                    await session.execute(
                        sa_insert(_LEASES).values(
                            tenant_id=tenant_id,
                            resource=resource,
                            **values,
                        )
                    )
        except IntegrityError:
            return None
        return LeaseRecord(
            resource=resource,
            owner_token=owner_token,
            fencing_token=fencing,
            acquired_at=now,
            expires_at=expires_at,
        )

    @traced_repository_operation("lease_renew")
    async def renew(
        self,
        lease: LeaseRecord,
        lease_duration: timedelta,
        *,
        context: StorageOperationContext,
    ) -> LeaseRecord:
        self._validate_duration(lease_duration)
        now = datetime.now(UTC)
        expires_at = now + lease_duration
        async with self._session() as session:
            result = await session.execute(
                sa_update(_LEASES)
                .where(
                    _LEASES.c.tenant_id == str(context.tenant_id),
                    _LEASES.c.resource == lease.resource,
                    _LEASES.c.owner_token == lease.owner_token,
                    _LEASES.c.fencing_token == lease.fencing_token,
                    _LEASES.c.expires_at > now,
                )
                .values(expires_at=expires_at)
            )
        if result.rowcount != 1:
            raise HarborStorageLeaseError(
                f"lease {lease.resource!r} is not owned or has expired",
                context=self._backend.error_context("lease_renew", context, lease.resource),
            )
        return lease.model_copy(update={"expires_at": expires_at})

    @traced_repository_operation("lease_release")
    async def release(
        self,
        lease: LeaseRecord,
        *,
        context: StorageOperationContext,
    ) -> bool:
        async with self._session() as session:
            result = await session.execute(
                sa_delete(_LEASES).where(
                    _LEASES.c.tenant_id == str(context.tenant_id),
                    _LEASES.c.resource == lease.resource,
                    _LEASES.c.owner_token == lease.owner_token,
                    _LEASES.c.fencing_token == lease.fencing_token,
                )
            )
        return bool(result.rowcount == 1)

    @staticmethod
    def _validate_duration(lease_duration: timedelta) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
