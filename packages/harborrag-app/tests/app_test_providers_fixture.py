"""ProviderServiceFixture: split out of app_test_fixtures.py to keep that file
under the repo's file-length gate.

Mixed into MockAppService, which initializes ``self.providers``,
``self.routing_rules``, and the ``*_calls`` lists this fixture reads/appends.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from harborrag_app.workflow_control import AppResponse
from harborrag_core.contracts.errors import HarborCapabilityError, HarborNotFoundError
from harborrag_core.domain.provider import Provider, ProviderFamily
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.ports.provider_probe import ProviderProbeResult


class ProviderServiceFixture:
    async def list_providers(
        self,
        *,
        tenant_ids: frozenset[str] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> AppResponse:
        del tenant_ids
        ordered = sorted(self.providers.values(), key=lambda p: p.id)
        if cursor is not None:
            ordered = [p for p in ordered if p.id > cursor]
        page = ordered[:limit]
        next_cursor = page[-1].id if len(ordered) > limit else None
        return AppResponse(True, {"providers": page, "next_cursor": next_cursor})

    async def get_provider(
        self, provider_id: str, *, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del tenant_ids
        found = self.providers.get(provider_id)
        if found is None:
            raise HarborNotFoundError(f"provider {provider_id!r} not found")
        return AppResponse(True, {"provider": found})

    async def create_provider(  # noqa: PLR0913 - mirrors the workflow_control facade
        self,
        *,
        tenant_id: str,
        name: str,
        family: ProviderFamily,
        config: Mapping[str, object],
        api_key: str | None,
        actor: str,
    ) -> AppResponse:
        self.provider_create_calls.append(
            {
                "tenant_id": tenant_id,
                "name": name,
                "family": family,
                "config": dict(config),
                "api_key": api_key,
                "actor": actor,
            }
        )
        created = Provider(
            id=f"prov_{len(self.providers) + 1}",
            tenant_id=tenant_id,
            name=name,
            family=family,
            config=dict(config),
            secret_ref="secret://db/mock" if api_key else None,
        )
        self.providers[created.id] = created
        return AppResponse(True, {"provider": created})

    async def update_provider(
        self,
        provider_id: str,
        *,
        updates: dict[str, object],
        actor: str,
        tenant_ids: frozenset[str] | None = None,
    ) -> AppResponse:
        self.provider_update_calls.append(
            {"provider_id": provider_id, "updates": dict(updates), "actor": actor}
        )
        found = self.providers.get(provider_id)
        if found is None or (tenant_ids is not None and found.tenant_id not in tenant_ids):
            raise HarborNotFoundError(f"provider {provider_id!r} not found")
        if "name" in updates:
            found.name = updates["name"]  # type: ignore[assignment]
        if "config" in updates:
            found.config = dict(updates["config"])  # type: ignore[arg-type]
        if "api_key" in updates:
            found.secret_ref = "secret://db/rotated" if updates["api_key"] else None
        return AppResponse(True, {"provider": found})

    async def delete_provider(
        self, provider_id: str, *, actor: str, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del tenant_ids
        self.provider_delete_calls.append({"provider_id": provider_id, "actor": actor})
        if provider_id not in self.providers:
            raise HarborNotFoundError(f"provider {provider_id!r} not found")
        del self.providers[provider_id]
        return AppResponse(True, {"provider_id": provider_id})

    async def test_provider_connection(
        self, provider_id: str, *, tenant_ids: frozenset[str] | None = None
    ) -> AppResponse:
        del tenant_ids
        self.provider_test_calls.append({"provider_id": provider_id})
        found = self.providers.get(provider_id)
        if found is None:
            raise HarborNotFoundError(f"provider {provider_id!r} not found")
        if found.family != "chat":
            raise HarborCapabilityError(
                f"test-connection is only supported for chat providers, not {found.family!r}"
            )
        return AppResponse(
            True,
            {
                "result": ProviderProbeResult(
                    ok=True, message="Provider responded successfully", latency_ms=12.5
                )
            },
        )

    async def list_routing_rules(self) -> AppResponse:
        return AppResponse(True, {"rules": list(self.routing_rules)})

    async def replace_routing_rules(
        self, rules: Sequence[Mapping[str, object]], *, actor: str
    ) -> AppResponse:
        self.routing_replace_calls.append({"rules": [dict(rule) for rule in rules], "actor": actor})
        for entry in rules:
            provider_id = entry["provider_id"]
            if provider_id not in self.providers:
                raise HarborNotFoundError(f"provider {provider_id!r} not found")
        self.routing_rules = [
            RoutingRule(
                id=f"rule_{index + 1}",
                family=entry["family"],  # type: ignore[arg-type]
                provider_id=entry["provider_id"],  # type: ignore[arg-type]
                priority=entry.get("priority", 0),  # type: ignore[arg-type]
            )
            for index, entry in enumerate(rules)
        ]
        return AppResponse(True, {"rules": list(self.routing_rules)})

    async def get_provider_cost(self, *, tenant_ids: frozenset[str] | None = None) -> AppResponse:
        del tenant_ids
        return AppResponse(
            True,
            {
                "since": datetime(2026, 1, 1, tzinfo=UTC),
                "providers": dict.fromkeys(self.providers, 0.0),
            },
        )
