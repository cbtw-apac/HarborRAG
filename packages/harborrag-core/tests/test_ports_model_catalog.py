"""Validation contract for the tenant model-catalog dataclasses.

These types are the boundary the model layer builds a per-tenant chat client
from, so a malformed catalog must fail at construction rather than surface as
a routing error at request time.
"""

from __future__ import annotations

import pytest

from harborrag_core.ports.model_catalog import (
    TenantChatCatalog,
    TenantModelDefinition,
    TenantModelDeployment,
)


def _deployment(name: str = "primary", **overrides: object) -> TenantModelDeployment:
    fields: dict[str, object] = {"name": name, "provider": "openai", "model": "gpt-4o-mini"}
    fields.update(overrides)
    return TenantModelDeployment(**fields)  # type: ignore[arg-type]


def test_deployment_defaults_are_empty_and_weight_is_one() -> None:
    deployment = _deployment()
    assert (deployment.secret_ref, deployment.api_base) == (None, None)
    assert (dict(deployment.capabilities), dict(deployment.extra), deployment.weight) == (
        {},
        {},
        1,
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_deployment_rejects_blank_identity(blank: str) -> None:
    with pytest.raises(ValueError, match="deployment name must not be blank"):
        _deployment(name=blank)
    with pytest.raises(ValueError, match="deployment provider must not be blank"):
        _deployment(provider=blank)
    with pytest.raises(ValueError, match="deployment model must not be blank"):
        _deployment(model=blank)


@pytest.mark.parametrize("weight", [0, -1])
def test_deployment_requires_positive_weight(weight: int) -> None:
    with pytest.raises(ValueError, match="weight must be positive"):
        _deployment(weight=weight)


def test_definition_requires_a_non_blank_name_and_one_deployment() -> None:
    with pytest.raises(ValueError, match="logical model name must not be blank"):
        TenantModelDefinition(logical_model=" ", deployments=(_deployment(),))
    with pytest.raises(ValueError, match="has no deployments"):
        TenantModelDefinition(logical_model="fast", deployments=())


def test_definition_rejects_duplicate_deployment_names() -> None:
    with pytest.raises(ValueError, match="duplicate deployment name 'primary'"):
        TenantModelDefinition(
            logical_model="fast",
            deployments=(_deployment(), _deployment(model="gpt-4o")),
        )


def _catalog(*definitions: TenantModelDefinition, default: str | None = None) -> TenantChatCatalog:
    return TenantChatCatalog(
        tenant_id="tenant-a",
        default_model=default,
        models=definitions,
        fingerprint="stamp",
    )


def test_catalog_requires_a_tenant_and_rejects_duplicate_logical_models() -> None:
    with pytest.raises(ValueError, match="tenant id must not be blank"):
        TenantChatCatalog(tenant_id="", default_model=None, models=(), fingerprint="s")
    definition = TenantModelDefinition(logical_model="fast", deployments=(_deployment(),))
    other = TenantModelDefinition(logical_model="fast", deployments=(_deployment("second"),))
    with pytest.raises(ValueError, match="duplicate logical model name 'fast'"):
        _catalog(definition, other)


def test_catalog_is_empty_and_allows() -> None:
    empty = _catalog()
    assert empty.is_empty() is True
    assert empty.allows("fast") is False

    definition = TenantModelDefinition(logical_model="fast", deployments=(_deployment(),))
    populated = _catalog(definition, default="fast")
    assert populated.is_empty() is False
    assert populated.allows("fast") is True
    assert populated.allows("slow") is False
    assert populated.default_model == "fast"
