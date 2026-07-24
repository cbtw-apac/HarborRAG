"""SQLAlchemy control-plane repositories grouped by capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import (
    MemberRow,
    ProviderRow,
    WorkspaceSettingsRow,
)
from harborrag_core.domain.member import Member, Role
from harborrag_core.domain.provider import Provider, ProviderFamily
from harborrag_core.domain.settings import WorkspaceSettings

from .mapping import utc_now
from .session import SessionFactory


@dataclass(slots=True)
class SqlSettingsRepository:
    """SettingsRepositoryPort over the single workspace_settings row (id=1)."""

    sessions: SessionFactory

    async def get(self) -> WorkspaceSettings:
        """The settings document; empty document when never written."""
        async with self.sessions() as session:
            row = await session.get(WorkspaceSettingsRow, 1)
            return WorkspaceSettings(data=dict(row.data)) if row else WorkspaceSettings()

    async def put(self, settings: WorkspaceSettings) -> WorkspaceSettings:
        """Upsert the settings document."""
        async with self.sessions.begin() as session:
            row = await session.get(WorkspaceSettingsRow, 1)
            if row is None:
                row = WorkspaceSettingsRow(id=1, updated_at=utc_now())
                session.add(row)
            row.data = dict(settings.data)
            row.updated_at = utc_now()
        return settings

@dataclass(slots=True)
class SqlProviderRepository:
    """ProviderRepositoryPort over the providers table."""

    sessions: SessionFactory

    async def list(self) -> list[Provider]:
        """All providers ordered by id."""
        async with self.sessions() as session:
            rows = await session.scalars(sa.select(ProviderRow).order_by(ProviderRow.id))
            return [self._to_domain(row) for row in rows]

    async def get(self, provider_id: str) -> Provider | None:
        """One provider by id, or None."""
        async with self.sessions() as session:
            row = await session.get(ProviderRow, provider_id)
            return self._to_domain(row) if row else None

    async def save(self, provider: Provider) -> Provider:
        """Upsert the provider row."""
        async with self.sessions.begin() as session:
            row = await session.get(ProviderRow, provider.id)
            if row is None:
                row = ProviderRow(id=provider.id)
                session.add(row)
            row.name = provider.name
            row.family = provider.family
            row.config_json = dict(provider.config)
            row.secret_ref = provider.secret_ref
        return provider

    async def delete(self, provider_id: str) -> None:
        """Delete the provider row."""
        async with self.sessions.begin() as session:
            await session.execute(sa.delete(ProviderRow).where(ProviderRow.id == provider_id))

    @staticmethod
    def _to_domain(row: ProviderRow) -> Provider:
        """Map a providers row to the Provider aggregate."""
        return Provider(
            id=row.id,
            name=row.name,
            family=cast(ProviderFamily, row.family),
            config=dict(row.config_json),
            secret_ref=row.secret_ref,
        )

@dataclass(slots=True)
class SqlMemberRepository:
    """MemberRepositoryPort over the members table."""

    sessions: SessionFactory

    async def list(self) -> list[Member]:
        """All members ordered by subject."""
        async with self.sessions() as session:
            rows = await session.scalars(sa.select(MemberRow).order_by(MemberRow.subject))
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
                row = MemberRow(id=member.id, created_at=utc_now())
                session.add(row)
            row.subject = member.subject
            row.role = member.role
        return member

    async def delete(self, member_id: str) -> None:
        """Delete the membership row."""
        async with self.sessions.begin() as session:
            await session.execute(sa.delete(MemberRow).where(MemberRow.id == member_id))

    @staticmethod
    def _to_domain(row: MemberRow) -> Member:
        """Map a members row to the Member aggregate."""
        return Member(id=row.id, subject=row.subject, role=cast(Role, row.role))
