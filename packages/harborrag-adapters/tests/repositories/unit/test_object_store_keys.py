from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store.keys import (
    logical_object_key,
    physical_object_key,
    tenant_object_prefix,
    validate_object_key,
)


def test_valid_bucket_and_key_pass_validation() -> None:
    validate_object_key("my-bucket", "a/b/c.txt")


@pytest.mark.parametrize(
    "bucket",
    ["", "a/b", "a\\b", ".", ".."],
)
def test_invalid_bucket_names_are_rejected(bucket: str) -> None:
    with pytest.raises(ValueError, match="invalid bucket name"):
        validate_object_key(bucket, "key")


@pytest.mark.parametrize(
    "key",
    ["", "/abs/path", "\\abs\\path", "a/../b", "..", "a/..", "../a"],
)
def test_invalid_object_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="invalid object key"):
        validate_object_key("bucket", key)


def test_tenant_object_prefix_is_deterministic_and_namespaced() -> None:
    prefix_a = tenant_object_prefix("tenant-a")
    prefix_a_again = tenant_object_prefix("tenant-a")
    prefix_b = tenant_object_prefix("tenant-b")

    assert prefix_a == prefix_a_again
    assert prefix_a != prefix_b
    # The same namespace the tenant's Qdrant collections and FalkorDB graph use.
    assert prefix_a == "harborrag_tenant-a"
    assert tenant_object_prefix("tenant-a", "harbor_smoke") == "harbor_smoke_tenant-a"


def test_one_namespace_names_a_tenant_in_every_store() -> None:
    from harborrag_adapters.repositories.graph.falkordb.tenant_pool import TenantGraphRegistry
    from harborrag_core.storage import StorageOperationContext, tenant_namespace

    context = StorageOperationContext.system("DEFAULT")
    namespace = tenant_namespace("DEFAULT")

    assert namespace == "harborrag_DEFAULT"
    assert TenantGraphRegistry("harborrag").graph_for(context) == namespace
    assert physical_object_key("DEFAULT", "raw/x").startswith(f"{namespace}/")


@pytest.mark.parametrize("tenant", ["", "../x", "a/b", "-lead", "a b"])
def test_untrusted_tenant_ids_never_become_a_namespace(tenant: str) -> None:
    with pytest.raises(ValueError):
        tenant_object_prefix(tenant)


def test_physical_and_logical_object_key_round_trip() -> None:
    physical = physical_object_key("tenant-a", "docs/report.pdf")

    assert physical == f"{tenant_object_prefix('tenant-a')}/docs/report.pdf"
    assert logical_object_key("tenant-a", physical) == "docs/report.pdf"


def test_logical_object_key_returns_none_for_other_tenant_physical_key() -> None:
    physical = physical_object_key("tenant-a", "docs/report.pdf")

    assert logical_object_key("tenant-b", physical) is None


def test_logical_object_key_returns_none_for_unrelated_prefix() -> None:
    assert logical_object_key("tenant-a", "some/other/prefix/x") is None
