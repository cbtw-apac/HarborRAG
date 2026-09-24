"""SQLAlchemy control-plane repositories grouped by capability."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import (
    MemberRow,
    ProviderRow,
    RoutingRuleRow,
    WorkspaceSettingsRow,
)
from harborrag_core.contracts.errors import HarborConflictError, HarborValidationError
from harborrag_core.domain.member import Member, Role
from harborrag_core.domain.provider import Provider, ProviderFamily
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.domain.settings import WorkspaceSettings

from .mapping import utc_now
from .session import SessionFactory

_LEGACY_WORKSPACE_TENANT_ID = "DEFAULT"


@dataclass(slots=True)
class SqlSettingsRepository:
    """SettingsRepositoryPort over the single workspace_settings row (id=1)."""

    sessions: SessionFactory

    async def get(self) -> WorkspaceSettings:
        """The settings document; empty document when never written."""
        async with self.sessions() as session:
            row = await session.get(WorkspaceSettingsRow, 1)
            return (
                WorkspaceSettings(tenant_id=row.tenant_id, data=dict(row.data))
                if row
                else WorkspaceSettings(tenant_id=_LEGACY_WORKSPACE_TENANT_ID)
            )

    async def put(self, settings: WorkspaceSettings) -> WorkspaceSettings:
        """Upsert the settings document."""
        async with self.sessions.begin() as session:
            row = await session.get(WorkspaceSettingsRow, 1)
            if row is None:
                row = WorkspaceSettingsRow(
                    id=1,
                    tenant_id=settings.tenant_id,
                    updated_at=utc_now(),
                )
                session.add(row)
            elif row.tenant_id != settings.tenant_id:
                raise HarborConflictError("workspace settings tenant identity is immutable")
            row.data = dict(settings.data)
            row.updated_at = utc_now()
        return settings


@dataclass(slots=True)
class SqlProviderRepository:
    """ProviderRepositoryPort over the providers table.

    Delete is a soft delete: ``routing_rules.provider_id`` is a DB foreign
    key to ``providers.id``, so a hard-deleted, still-referenced provider
    would either violate that constraint or leave a dangling reference,
    depending on the backend. Instead ``delete`` sets ``deleted_at`` and
    every other method here treats a tombstoned row as absent.
    """

    sessions: SessionFactory

    async def list_page(
        self,
        *,
        tenant_ids: frozenset[str] | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Provider], str | None]:
        """Non-deleted providers visible to ``tenant_ids``, walked via an opaque keyset
        cursor over ``id`` (already a unique, generated key -- no second tiebreaker
        column is needed for a stable order)."""
        statement = (
            sa.select(ProviderRow).where(ProviderRow.deleted_at.is_(None)).order_by(ProviderRow.id)
        )
        if tenant_ids is not None:
            statement = statement.where(ProviderRow.tenant_id.in_(tenant_ids))
        if cursor is not None:
            statement = statement.where(ProviderRow.id > _decode_provider_cursor(cursor))
        statement = statement.limit(limit + 1)
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = _encode_provider_cursor(page[-1].id) if has_more else None
        return [self._to_domain(row) for row in page], next_cursor

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Provider]:
        """Non-deleted providers visible to ``tenant_ids`` (None: unrestricted), ordered by id."""
        statement = (
            sa.select(ProviderRow).where(ProviderRow.deleted_at.is_(None)).order_by(ProviderRow.id)
        )
        if tenant_ids is not None:
            statement = statement.where(ProviderRow.tenant_id.in_(tenant_ids))
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [self._to_domain(row) for row in rows]

    async def get(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> Provider | None:
        """One non-deleted provider by id within ``tenant_ids``, or None."""
        async with self.sessions() as session:
            row = await session.get(ProviderRow, provider_id)
            if (
                row is None
                or row.deleted_at is not None
                or (tenant_ids is not None and row.tenant_id not in tenant_ids)
            ):
                return None
            return self._to_domain(row)

    async def save(self, provider: Provider) -> Provider:
        """Upsert the provider row."""
        async with self.sessions.begin() as session:
            row = await session.get(ProviderRow, provider.id)
            if row is None:
                row = ProviderRow(id=provider.id, tenant_id=provider.tenant_id)
                session.add(row)
            elif row.tenant_id != provider.tenant_id:
                raise HarborConflictError("provider tenant identity is immutable")
            row.name = provider.name
            row.family = provider.family
            row.config_json = dict(provider.config)
            row.secret_ref = provider.secret_ref
            row.deleted_at = provider.deleted_at
            # The tenant model-catalog fingerprint is (count, max(updated_at));
            # skipping this on an in-place edit would leave stale catalogs cached.
            row.updated_at = utc_now()
        return provider

    async def delete(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Tombstone the provider row within ``tenant_ids``; the row is kept, not removed."""
        statement = (
            sa.update(ProviderRow)
            .where(ProviderRow.id == provider_id, ProviderRow.deleted_at.is_(None))
            .values(deleted_at=utc_now())
        )
        if tenant_ids is not None:
            statement = statement.where(ProviderRow.tenant_id.in_(tenant_ids))
        async with self.sessions.begin() as session:
            await session.execute(statement)

    @staticmethod
    def _to_domain(row: ProviderRow) -> Provider:
        """Map a providers row to the Provider aggregate."""
        return Provider(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            family=cast(ProviderFamily, row.family),
            config=dict(row.config_json),
            secret_ref=row.secret_ref,
            deleted_at=row.deleted_at,
        )


@dataclass(slots=True)
class SqlRoutingRuleRepository:
    """RoutingRuleRepositoryPort over the routing_rules table (workspace-wide, no tenant scope)."""

    sessions: SessionFactory

    async def replace(self, rules: Sequence[RoutingRule]) -> list[RoutingRule]:
        """Delete every existing rule and insert ``rules`` in one transaction."""
        async with self.sessions.begin() as session:
            await session.execute(sa.delete(RoutingRuleRow))
            for rule in rules:
                session.add(
                    RoutingRuleRow(
                        id=rule.id,
                        family=rule.family,
                        provider_id=rule.provider_id,
                        rule_json={"priority": rule.priority},
                        created_at=utc_now(),
                        updated_at=utc_now(),
                    )
                )
        return list(rules)

    async def list(self) -> list[RoutingRule]:
        """Every rule, ordered by family then id."""
        statement = sa.select(RoutingRuleRow).order_by(RoutingRuleRow.family, RoutingRuleRow.id)
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(row: RoutingRuleRow) -> RoutingRule:
        """Map a routing_rules row to the RoutingRule aggregate."""
        return RoutingRule(
            id=row.id,
            family=cast(ProviderFamily, row.family),
            provider_id=row.provider_id,
            priority=int(row.rule_json.get("priority", 0)),
        )


@dataclass(slots=True)
class SqlMemberRepository:
    """MemberRepositoryPort over the members table."""

    sessions: SessionFactory

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Member]:
        """Members visible to ``tenant_ids`` (None: unrestricted), ordered by subject."""
        statement = sa.select(MemberRow).order_by(MemberRow.subject)
        if tenant_ids is not None:
            statement = statement.where(MemberRow.tenant_id.in_(tenant_ids))
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [self._to_domain(row) for row in rows]

    async def get_by_subject(self, subject: str) -> Member | None:
        """Member by auth subject (unique), or None."""
        async with self.sessions() as session:
            row = await session.scalar(sa.select(MemberRow).where(MemberRow.subject == subject))
            return self._to_domain(row) if row else None

    async def save(self, member: Member) -> Member:
        """Upsert the membership row."""
        async with self.sessions.begin() as session:
            row = await session.get(MemberRow, member.id)
            if row is None:
                row = MemberRow(
                    id=member.id,
                    tenant_id=member.tenant_id,
                    created_at=utc_now(),
                )
                session.add(row)
            elif row.tenant_id != member.tenant_id:
                raise HarborConflictError("member tenant identity is immutable")
            row.subject = member.subject
            row.role = member.role
        return member

    async def delete(self, member_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Delete the membership row within ``tenant_ids``."""
        statement = sa.delete(MemberRow).where(MemberRow.id == member_id)
        if tenant_ids is not None:
            statement = statement.where(MemberRow.tenant_id.in_(tenant_ids))
        async with self.sessions.begin() as session:
            await session.execute(statement)

    @staticmethod
    def _to_domain(row: MemberRow) -> Member:
        """Map a members row to the Member aggregate."""
        return Member(
            id=row.id,
            tenant_id=row.tenant_id,
            subject=row.subject,
            role=cast(Role, row.role),
        )


def _encode_provider_cursor(provider_id: str) -> str:
    payload = json.dumps({"id": provider_id}, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_provider_cursor(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        provider_id = str(payload["id"])
        if not provider_id:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HarborValidationError("provider cursor is invalid") from error
    return provider_id
