"""Fakes for per-tenant chat catalog resolution."""

from __future__ import annotations

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
)
from harborrag_core.ports.model_catalog import (
    TenantChatCatalog,
    TenantModelDefinition,
    TenantModelDeployment,
)


class FakeTenantChatClient:
    """Async chat client double that records how often it was disposed."""

    def __init__(self, label: str = "tenant") -> None:
        self.label = label
        self.closes = 0
        self.calls = 0

    async def achat(self, messages=None, *, request=None, model=None, **kwargs):  # type: ignore[no-untyped-def]
        del messages, request, model, kwargs
        self.calls += 1
        return HarborChatResponse(
            id=f"chat-{self.label}",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment=f"{self.label}-primary",
            message=HarborChatMessage.assistant("hello"),
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def aclose(self) -> None:
        self.closes += 1

    @property
    def closed(self) -> bool:
        return self.closes > 0


class FakeSecrets:
    """``SecretsPort`` double recording the tenant every resolve was scoped to."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {"ref-1": "tenant-key"}
        self.resolved: list[tuple[str, str]] = []

    async def put(self, value: str, *, tenant_id: str) -> str:
        ref = f"ref-{len(self.values) + 1}"
        del tenant_id
        self.values[ref] = value
        return ref

    async def resolve(self, ref: str, *, tenant_id: str) -> str:
        self.resolved.append((ref, tenant_id))
        try:
            return self.values[ref]
        except KeyError as exc:
            raise LookupError(f"unknown secret ref: {ref}") from exc

    async def delete(self, ref: str, *, tenant_id: str) -> None:
        del tenant_id
        self.values.pop(ref, None)


class FakeCatalog:
    """``TenantModelCatalogPort`` double with a mutable fingerprint."""

    def __init__(
        self,
        catalogs: dict[str, TenantChatCatalog] | None = None,
        *,
        fingerprints: dict[str, str] | None = None,
    ) -> None:
        self.catalogs = catalogs or {}
        self.fingerprints = fingerprints or {}
        self.fingerprint_calls = 0
        self.catalog_calls: list[str] = []
        self.fingerprint_failure: Exception | None = None
        self.catalog_failure: Exception | None = None

    async def fingerprint(self, tenant_id: str) -> str:
        self.fingerprint_calls += 1
        if self.fingerprint_failure is not None:
            raise self.fingerprint_failure
        return self.fingerprints.get(tenant_id, "fp-0")

    async def chat_catalog(self, tenant_id: str) -> TenantChatCatalog:
        self.catalog_calls.append(tenant_id)
        if self.catalog_failure is not None:
            raise self.catalog_failure
        existing = self.catalogs.get(tenant_id)
        if existing is not None:
            return existing
        return empty_catalog(tenant_id, self.fingerprints.get(tenant_id, "fp-0"))


def empty_catalog(tenant_id: str, fingerprint: str = "fp-0") -> TenantChatCatalog:
    return TenantChatCatalog(
        tenant_id=tenant_id,
        default_model=None,
        models=(),
        fingerprint=fingerprint,
    )


def tenant_deployment(tenant_id: str) -> TenantModelDeployment:
    """One streaming-capable deployment; ``dataclasses.replace`` varies it."""

    return TenantModelDeployment(
        name=f"{tenant_id}-deployment",
        provider="openai",
        model="gpt-4o-mini",
        secret_ref="ref-1",
        capabilities={"streaming": True},
    )


def tenant_catalog(
    tenant_id: str,
    *,
    fingerprint: str = "fp-0",
    logical_model: str = "tenant-primary",
    deployment: TenantModelDeployment | None = None,
) -> TenantChatCatalog:
    """One tenant catalog with a single logical model."""

    return TenantChatCatalog(
        tenant_id=tenant_id,
        default_model=logical_model,
        models=(
            TenantModelDefinition(
                logical_model=logical_model,
                deployments=(deployment or tenant_deployment(tenant_id),),
            ),
        ),
        fingerprint=fingerprint,
    )


__all__ = [
    "FakeCatalog",
    "FakeSecrets",
    "FakeTenantChatClient",
    "empty_catalog",
    "tenant_catalog",
    "tenant_deployment",
]
