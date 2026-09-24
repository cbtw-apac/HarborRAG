"""Provider/routing/probe/cost fakes: split out of control_plane_fakes.py to
keep that file under the repo's file-length gate.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.ports.provider_probe import ProviderProbeResult


def _in_scope(tenant_id: str, tenant_ids: frozenset[str] | None) -> bool:
    """Mirror the Sql* repositories' tenant filter: None means unrestricted."""
    return tenant_ids is None or tenant_id in tenant_ids


@dataclass(slots=True)
class FakeProviderRepository:
    """Dict-backed ProviderRepositoryPort.

    Mirrors ``SqlProviderRepository``'s soft delete: a routing rule may
    reference a provider's id, so ``delete`` tombstones (``deleted_at``)
    instead of dropping it from ``providers``.
    """

    providers: dict[str, Provider] = field(default_factory=dict)

    async def list_page(
        self,
        *,
        tenant_ids: frozenset[str] | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Provider], str | None]:
        """Non-deleted providers visible to ``tenant_ids``, walked via an opaque keyset
        cursor over ``id`` -- mirrors ``SqlProviderRepository.list_page``."""
        scoped = sorted(
            (
                p
                for p in self.providers.values()
                if p.deleted_at is None and _in_scope(p.tenant_id, tenant_ids)
            ),
            key=lambda p: p.id,
        )
        if cursor is not None:
            after = _decode_provider_cursor(cursor)
            scoped = [p for p in scoped if p.id > after]
        page = scoped[:limit]
        next_cursor = _encode_provider_cursor(page[-1].id) if len(scoped) > limit else None
        return page, next_cursor

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Provider]:
        """Non-deleted providers visible to ``tenant_ids`` (None: unrestricted)."""
        return [
            p
            for p in self.providers.values()
            if p.deleted_at is None and _in_scope(p.tenant_id, tenant_ids)
        ]

    async def get(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> Provider | None:
        """Non-deleted provider by id within ``tenant_ids``, or None."""
        provider = self.providers.get(provider_id)
        if provider is None or provider.deleted_at is not None:
            return None
        if not _in_scope(provider.tenant_id, tenant_ids):
            return None
        return provider

    async def save(self, provider: Provider) -> Provider:
        """Insert or overwrite a provider."""
        self.providers[provider.id] = provider
        return provider

    async def delete(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Tombstone the provider if present within ``tenant_ids``; the row is kept."""
        provider = self.providers.get(provider_id)
        if (
            provider is not None
            and provider.deleted_at is None
            and _in_scope(provider.tenant_id, tenant_ids)
        ):
            provider.deleted_at = utc_now()


@dataclass(slots=True)
class FakeRoutingRuleRepository:
    """List-backed RoutingRuleRepositoryPort; mirrors the SQL adapter's full-replace semantics."""

    rules: list[RoutingRule] = field(default_factory=list)

    async def replace(self, rules: Sequence[RoutingRule]) -> list[RoutingRule]:
        """Atomically swap the whole table for ``rules``."""
        self.rules = list(rules)
        return list(self.rules)

    async def list(self) -> list[RoutingRule]:
        """Every rule, ordered by family then id -- mirrors the SQL adapter's order."""
        return sorted(self.rules, key=lambda rule: (rule.family, rule.id))


@dataclass(slots=True)
class FakeProviderProbe:
    """Canned ProviderProbePort; records calls instead of making a real network request."""

    result: ProviderProbeResult = field(
        default_factory=lambda: ProviderProbeResult(
            ok=True, message="Provider responded successfully", latency_ms=1.0
        )
    )
    probed: list[Provider] = field(default_factory=list)

    async def probe(self, provider: Provider) -> ProviderProbeResult:
        """Record ``provider`` and return the canned result without any I/O."""
        self.probed.append(provider)
        return self.result


@dataclass(slots=True)
class FakeProviderCostTracker:
    """In-memory ProviderCostTrackerPort double; same semantics as the production tracker."""

    started_at: datetime = field(default_factory=utc_now)
    _totals: dict[str, float] = field(default_factory=dict)

    def record(self, provider_id: str, cost_usd: float) -> None:
        """Add ``cost_usd`` to ``provider_id``'s running total."""
        self._totals[provider_id] = self._totals.get(provider_id, 0.0) + cost_usd

    def snapshot(self, provider_ids: Iterable[str]) -> dict[str, float]:
        """Current total spend per id (0.0 for one never recorded)."""
        return {provider_id: self._totals.get(provider_id, 0.0) for provider_id in provider_ids}


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
