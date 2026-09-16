"""Canonical provider/routing-rule payloads shared by the app test doubles."""

from __future__ import annotations

from harborrag_core.domain.provider import Provider
from harborrag_core.domain.routing_rule import RoutingRule


def provider(
    provider_id: str = "prov_1",
    *,
    tenant_id: str = "DEFAULT",
    name: str = "OpenAI Production",
    family: str = "chat",
    secret_ref: str | None = "secret://db/1",
) -> Provider:
    """Return one configured provider for API contract tests."""

    return Provider(
        id=provider_id,
        tenant_id=tenant_id,
        name=name,
        family=family,  # type: ignore[arg-type]
        config={"model": "openai/gpt-4o-mini"},
        secret_ref=secret_ref,
    )


def routing_rule(
    rule_id: str = "rule_1",
    *,
    family: str = "chat",
    provider_id: str = "prov_1",
    priority: int = 0,
) -> RoutingRule:
    """Return one routing rule for API contract tests."""

    return RoutingRule(
        id=rule_id,
        family=family,  # type: ignore[arg-type]
        provider_id=provider_id,
        priority=priority,
    )


__all__ = ["provider", "routing_rule"]
