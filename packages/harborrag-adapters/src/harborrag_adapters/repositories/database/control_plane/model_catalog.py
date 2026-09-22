"""SqlTenantModelCatalog: a tenant's own chat models, read from `providers`.

Chat models otherwise come from one process-wide YAML document whose API keys
expand from the process environment, so a tenant can neither bring its own
deployment nor rotate its own key without a redeploy. This repository is the
read side of the per-tenant alternative, projecting the tenant's `providers`
rows (family `chat`) and the `routing_rules` rows reachable through them into
a `TenantChatCatalog`.

Two behaviours the caller depends on:

* A tenant with no `chat` provider rows yields an **empty** catalog, never an
  error, so the caller falls back to the process-wide YAML catalog and
  existing single-tenant deployments behave exactly as before.
* One malformed or unsupported row is skipped and logged at ERROR with its
  row id; the rest of the tenant's catalog is built normally. A single bad row
  must never take a tenant's chat offline.

`fingerprint()` is one aggregate query -- per-tenant provider/rule row count,
latest `updated_at`, and greatest row id, hashed -- so a caller can validate
a per-tenant cache on every request without a shared invalidation channel. It
therefore depends on every writer of those two tables refreshing `updated_at`
on every mutation (see the ORM docstrings); an in-place edit that skips it is
invisible here. Two states that agree on all six aggregates hash alike, so an
insert immediately undone by a delete legitimately restores the previous
stamp -- the configuration really is the one that stamp labelled.

`chat_catalog()` reads the fingerprint *before* the rows, so a concurrent
write can only make the returned stamp older than the content it labels --
costing one extra refresh rather than caching stale content forever.

The accepted `config_json`/`rule_json` shapes live in `model_catalog_rows`.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_core.ports.model_catalog import (
    TenantChatCatalog,
    TenantModelDefinition,
    TenantModelDeployment,
)

from .model_catalog_rows import (
    CHAT_FAMILY,
    ParsedProviderRow,
    ProviderRowInput,
    RuleOverride,
    parse_provider_row,
    parse_rule,
)
from .schemas import ProviderRow, RoutingRuleRow
from .session import SessionFactory

logger = logging.getLogger("harborrag.adapters.control_plane.model_catalog")


def _stamp(parts: Iterable[object]) -> str:
    """Hash an aggregate tuple into a stable, opaque fingerprint string."""
    payload = "|".join(
        "-" if part is None else part.isoformat() if isinstance(part, datetime) else str(part)
        for part in parts
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _fingerprint_statement(tenant_id: str) -> sa.Select[tuple[object, ...]]:
    """Build the single six-scalar aggregate a fingerprint is derived from.

    Row identity (`max(id)`) rides alongside count and latest `updated_at`
    because a delete can move `max(updated_at)` *backwards*: without it,
    deleting the newest row and inserting a different one in the same instant
    would reproduce the previous stamp.
    """
    scope = ProviderRow.tenant_id == tenant_id
    rules = sa.join(RoutingRuleRow, ProviderRow, RoutingRuleRow.provider_id == ProviderRow.id)
    return sa.select(
        sa.select(sa.func.count()).select_from(ProviderRow).where(scope).scalar_subquery(),
        sa.select(sa.func.max(ProviderRow.updated_at)).where(scope).scalar_subquery(),
        sa.select(sa.func.max(ProviderRow.id)).where(scope).scalar_subquery(),
        sa.select(sa.func.count()).select_from(rules).where(scope).scalar_subquery(),
        sa.select(sa.func.max(RoutingRuleRow.updated_at))
        .select_from(rules)
        .where(scope)
        .scalar_subquery(),
        sa.select(sa.func.max(RoutingRuleRow.id)).select_from(rules).where(scope).scalar_subquery(),
    )


def _group(parsed: Sequence[ParsedProviderRow]) -> tuple[TenantModelDefinition, ...]:
    """Group validated rows into logical models, preserving first-seen order."""
    grouped: dict[str, list[TenantModelDeployment]] = {}
    for row in parsed:
        grouped.setdefault(row.logical_model, []).append(row.deployment)
    definitions: list[TenantModelDefinition] = []
    for logical_model, deployments in grouped.items():
        try:
            definitions.append(
                TenantModelDefinition(
                    logical_model=logical_model,
                    deployments=tuple(deployments),
                )
            )
        except ValueError:
            logger.exception(
                "Skipping tenant logical model %r: its provider rows do not form a "
                "valid definition (row ids: %s)",
                logical_model,
                ", ".join(row.row_id for row in parsed if row.logical_model == logical_model),
            )
    return tuple(definitions)


def _default_model(parsed: Sequence[ParsedProviderRow], allowed: frozenset[str]) -> str | None:
    """Return the first row-declared default that survived validation."""
    for row in parsed:
        if row.is_default and row.logical_model in allowed:
            return row.logical_model
    return None


@dataclass(slots=True)
class SqlTenantModelCatalog:
    """TenantModelCatalogPort over the tenant's providers/routing_rules rows."""

    sessions: SessionFactory

    async def fingerprint(self, tenant_id: str) -> str:
        """Hash (count, latest updated_at, greatest id) over the tenant's provider/rule rows."""
        async with self.sessions() as session:
            aggregate = (await session.execute(_fingerprint_statement(tenant_id))).one()
        return _stamp(tuple(aggregate))

    async def chat_catalog(self, tenant_id: str) -> TenantChatCatalog:
        """Project the tenant's chat provider rows into a catalog; empty when unconfigured."""
        fingerprint = await self.fingerprint(tenant_id)
        async with self.sessions() as session:
            rules = await self._rules(session, tenant_id)
            rows = list(
                await session.scalars(
                    sa.select(ProviderRow)
                    .where(
                        ProviderRow.tenant_id == tenant_id,
                        ProviderRow.family == CHAT_FAMILY,
                    )
                    .order_by(ProviderRow.id)
                )
            )
        parsed = self._parse(rows, rules)
        models = _group(parsed)
        return TenantChatCatalog(
            tenant_id=tenant_id,
            default_model=_default_model(
                parsed, frozenset(definition.logical_model for definition in models)
            ),
            models=models,
            fingerprint=fingerprint,
        )

    @staticmethod
    async def _rules(session: AsyncSession, tenant_id: str) -> dict[str, RuleOverride]:
        """Validate the tenant's chat routing rules, keyed by provider id.

        Rules are applied in row-id order, so if several point at one provider
        the highest rule id is the one that takes effect. A rule that fails
        validation is dropped on its own and logged; the
        provider row it points at is still served with its own weight, because
        a bad routing tweak must not remove a working deployment.
        """
        statement = (
            sa.select(RoutingRuleRow)
            .join(ProviderRow, RoutingRuleRow.provider_id == ProviderRow.id)
            .where(ProviderRow.tenant_id == tenant_id, RoutingRuleRow.family == CHAT_FAMILY)
            .order_by(RoutingRuleRow.id)
        )
        overrides: dict[str, RuleOverride] = {}
        for row in await session.scalars(statement):
            try:
                overrides[row.provider_id] = parse_rule(row.rule_json, row_id=row.provider_id)
            except ValueError:
                logger.exception(
                    "Ignoring unusable chat routing rule id=%r for provider row id=%r",
                    row.id,
                    row.provider_id,
                )
        return overrides

    @staticmethod
    def _parse(
        rows: Sequence[ProviderRow], rules: dict[str, RuleOverride]
    ) -> tuple[ParsedProviderRow, ...]:
        """Validate each row, skipping and logging the ones that cannot be used."""
        parsed: list[ParsedProviderRow] = []
        for row in rows:
            try:
                parsed.append(
                    parse_provider_row(
                        ProviderRowInput(
                            row_id=row.id,
                            name=row.name,
                            config=row.config_json,
                            secret_ref=row.secret_ref,
                            rule=rules.get(row.id),
                        )
                    )
                )
            except ValueError:
                logger.exception(
                    "Skipping unusable tenant chat provider row id=%r tenant=%r",
                    row.id,
                    row.tenant_id,
                )
        return tuple(parsed)
