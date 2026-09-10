"""Row builders for the tenant model-catalog tests.

Nothing in the product writes `providers`/`routing_rules` yet, so these
helpers insert the rows directly through the ORM -- including `updated_at`,
which the fingerprint depends on every writer refreshing. The migrated
`sessions` fixture they are used with lives in this directory's conftest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import (
    ProviderRow,
    RoutingRuleRow,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def chat_config(logical_model: str, **overrides: Any) -> dict[str, Any]:
    """Build a valid chat `config_json` body, overriding any documented key."""
    config: dict[str, Any] = {
        "provider": "openai",
        "logical_model": logical_model,
        "model": "gpt-4o-mini",
    }
    config.update(overrides)
    return config


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One `providers` row to insert."""

    id: str
    config: Mapping[str, Any] = field(default_factory=dict)
    tenant_id: str = TENANT
    name: str = "row-name"
    family: str = "chat"
    secret_ref: str | None = None
    updated_at: datetime = EPOCH


async def insert_providers(sessions: SessionFactory, *specs: ProviderSpec) -> None:
    """Insert provider rows exactly as stored, without any repository validation."""
    async with sessions.begin() as session:
        for spec in specs:
            session.add(
                ProviderRow(
                    id=spec.id,
                    tenant_id=spec.tenant_id,
                    name=spec.name,
                    family=spec.family,
                    config_json=dict(spec.config),
                    secret_ref=spec.secret_ref,
                    updated_at=spec.updated_at,
                )
            )


async def insert_rule(
    sessions: SessionFactory,
    rule_id: str,
    provider_id: str,
    rule: Mapping[str, Any],
    family: str = "chat",
) -> None:
    """Insert one `routing_rules` row pointing at a provider row."""
    async with sessions.begin() as session:
        session.add(
            RoutingRuleRow(
                id=rule_id,
                family=family,
                provider_id=provider_id,
                rule_json=dict(rule),
                created_at=EPOCH,
                updated_at=EPOCH,
            )
        )


async def touch_provider(
    sessions: SessionFactory, row_id: str, *, config: Mapping[str, Any] | None = None
) -> None:
    """Edit a provider row the way a compliant writer would: bump `updated_at`."""
    values: dict[str, Any] = {"updated_at": EPOCH.replace(year=2027)}
    if config is not None:
        values["config_json"] = dict(config)
    async with sessions.begin() as session:
        await session.execute(
            sa.update(ProviderRow).where(ProviderRow.id == row_id).values(**values)
        )


async def delete_provider(sessions: SessionFactory, row_id: str) -> None:
    """Remove a provider row."""
    async with sessions.begin() as session:
        await session.execute(sa.delete(ProviderRow).where(ProviderRow.id == row_id))
