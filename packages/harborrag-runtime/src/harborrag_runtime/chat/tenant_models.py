"""The two ports per-tenant chat resolution needs, bundled for wiring."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ports.model_catalog import TenantModelCatalogPort
from harborrag_core.ports.secrets import SecretsPort


@dataclass(frozen=True, slots=True)
class TenantModelSources:
    """Read a tenant's stored chat catalog and the keys its deployments name.

    Both are control-plane ports, so they are handed to the runtime at
    composition rather than loaded from configuration: the runtime SDK is
    constructed from settings alone and must keep working for every
    deployment that wires neither.
    """

    catalog: TenantModelCatalogPort
    secrets: SecretsPort


__all__ = ["TenantModelSources"]
