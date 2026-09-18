"""TenantModelCatalogPort: a tenant's own chat models, not the process YAML.

Chat models are otherwise loaded once per process from a YAML document whose
API keys expand from the process environment, so a tenant can neither bring
its own deployment nor rotate its own key without a redeploy. This port is
the read side of the per-tenant alternative: a tenant's stored provider rows
projected into a `TenantChatCatalog` the model layer can build a client from.

Two deliberate properties:

* `chat_catalog` never fails for a tenant that simply has nothing configured
  -- it returns an empty catalog (`is_empty()`), and the caller falls back to
  the process-wide YAML catalog. Existing single-tenant deployments therefore
  behave exactly as before.
* `fingerprint` is a cheap, monotonic-ish stamp over the tenant's stored
  configuration. It exists so a caller can validate a per-tenant cache
  without a shared invalidation channel: same fingerprint means the cached
  catalog is still current, a different one means rebuild. It must be far
  cheaper than `chat_catalog`, because it runs on every request.

`TenantChatCatalog.fingerprint` always carries the stamp of the configuration
the catalog was built from, so a caller can cache the pair together.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


def _require_text(value: str, *, label: str) -> None:
    """Reject a blank or whitespace-only name."""
    if not value.strip():
        raise ValueError(f"{label} must not be blank")


@dataclass(frozen=True, slots=True)
class TenantModelDeployment:
    """One concrete provider deployment behind a tenant's logical model.

    `secret_ref` is an opaque SecretsPort ref, never key material; resolving
    it is the model layer's job and is tenant-scoped at the secrets port.
    """

    name: str
    provider: str
    model: str
    secret_ref: str | None = None
    api_base: str | None = None
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    weight: int = 1
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Require non-blank identity and a usable routing weight."""
        _require_text(self.name, label="deployment name")
        _require_text(self.provider, label="deployment provider")
        _require_text(self.model, label="deployment model")
        if self.weight <= 0:
            raise ValueError(f"deployment {self.name!r} weight must be positive, got {self.weight}")


@dataclass(frozen=True, slots=True)
class TenantModelDefinition:
    """One logical model name and the deployments that can serve it."""

    logical_model: str
    deployments: tuple[TenantModelDeployment, ...]

    def __post_init__(self) -> None:
        """Require a non-blank name, at least one deployment, and unique deployment names."""
        _require_text(self.logical_model, label="logical model name")
        if not self.deployments:
            raise ValueError(f"logical model {self.logical_model!r} has no deployments")
        seen: set[str] = set()
        for deployment in self.deployments:
            if deployment.name in seen:
                raise ValueError(
                    f"logical model {self.logical_model!r} has duplicate "
                    f"deployment name {deployment.name!r}"
                )
            seen.add(deployment.name)


@dataclass(frozen=True, slots=True)
class TenantChatCatalog:
    """A tenant's complete chat catalog plus the stamp it was built from."""

    tenant_id: str
    default_model: str | None
    models: tuple[TenantModelDefinition, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        """Require a non-blank tenant and unique logical model names."""
        _require_text(self.tenant_id, label="tenant id")
        seen: set[str] = set()
        for definition in self.models:
            if definition.logical_model in seen:
                raise ValueError(
                    f"tenant {self.tenant_id!r} has duplicate logical "
                    f"model name {definition.logical_model!r}"
                )
            seen.add(definition.logical_model)

    def is_empty(self) -> bool:
        """Return whether this tenant configured no usable models at all."""
        return not self.models

    def allows(self, logical_model: str) -> bool:
        """Return whether the tenant configured this logical model name."""
        return any(definition.logical_model == logical_model for definition in self.models)


class TenantModelCatalogPort(Protocol):
    """Read a tenant's stored chat configuration and a cheap stamp over it."""

    async def fingerprint(self, tenant_id: str) -> str:
        """Return a cheap stamp that changes whenever the tenant's rows change."""

    async def chat_catalog(self, tenant_id: str) -> TenantChatCatalog:
        """Return the tenant's chat catalog; empty (never an error) when unconfigured."""
