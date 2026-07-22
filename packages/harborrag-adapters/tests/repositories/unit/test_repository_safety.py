from __future__ import annotations

from typing import Any, Literal

import pytest

from harborrag_adapters.repositories.database.sqlite.config import SQLiteDatabaseConfig
from harborrag_adapters.repositories.errors import (
    HarborStorageAuthorizationError,
    HarborStorageConfigurationError,
    HarborUnsafeQueryError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.mapping import FalkorDBMapper
from harborrag_adapters.repositories.graph.falkordb.repository import (
    FalkorDBGraphRepository,
)
from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.shared.provider_map import ProviderMap
from harborrag_adapters.repositories.shared.tenancy import ensure_tenant
from harborrag_adapters.repositories.state.sqlite.config import SQLiteStateConfig
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext


class ExampleConfig(RepositoryConfig):
    backend: Literal["example"] = "example"


class BrokenPlugin(RepositoryPlugin[ExampleConfig, object]):
    name = "example"
    family = StorageFamily.CACHE
    config_type = ExampleConfig
    optional_dependency = "example"

    def __init__(self, message: str) -> None:
        self._message = message

    def create(self, config: ExampleConfig, dependencies: RepositoryDependencies) -> object:
        del config, dependencies
        raise ImportError(self._message)


class FakeFalkorClient:
    async def read(self, statement: str, parameters: dict[str, Any]) -> Any:
        raise AssertionError(f"unsafe query was executed: {statement!r}, {parameters!r}")


def test_provider_map_does_not_mask_internal_import_errors() -> None:
    providers: ProviderMap[object] = ProviderMap(StorageFamily.CACHE)
    providers.register(BrokenPlugin("cannot import name 'broken_internal'"))

    with pytest.raises(ImportError, match="broken_internal"):
        providers.create(
            backend="example",
            instance_name="test",
            options=None,
            dependencies=None,
        )


def test_provider_map_wraps_explicit_missing_dependency_error() -> None:
    providers: ProviderMap[object] = ProviderMap(StorageFamily.CACHE)
    providers.register(BrokenPlugin("example-sdk is not installed"))

    with pytest.raises(HarborStorageConfigurationError):
        providers.create(
            backend="example",
            instance_name="test",
            options=None,
            dependencies=None,
        )


def test_tenant_mismatch_uses_storage_authorization_error() -> None:
    context = StorageOperationContext(tenant_id="tenant-a")
    with pytest.raises(HarborStorageAuthorizationError):
        ensure_tenant(
            "tenant-b",
            context,
            error_context=StorageErrorContext(
                family=StorageFamily.DATABASE,
                backend="test",
                instance_name="test",
                operation="write",
                tenant_id=str(context.tenant_id),
            ),
        )


@pytest.mark.asyncio
async def test_falkordb_advanced_query_is_never_executed() -> None:
    repository = FalkorDBGraphRepository(
        FalkorDBGraphConfig(),
        client=FakeFalkorClient(),  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")

    assert repository.capabilities.advanced_native_query is False
    with pytest.raises(HarborUnsafeQueryError):
        await repository.advanced_query(
            "MATCH (n) WHERE n.tenant_id = $harbor_tenant_id OR true RETURN n",
            {},
            context=context,
        )


def test_sqlite_schema_creation_is_opt_in() -> None:
    assert SQLiteDatabaseConfig().create_schema is False
    assert SQLiteStateConfig().create_schema is False


def test_falkordb_structured_properties_round_trip_through_json_marker() -> None:
    original = {
        "nested": {"source": "smoke", "rank": 1},
        "items": [{"id": 1}, {"id": 2}],
        "ordinary": "text",
        "prefixed": f"{FalkorDBMapper.json_prefix}user-text",
    }

    encoded = FalkorDBMapper.encode_properties(original)
    decoded = {key: FalkorDBMapper.decode_property(value) for key, value in encoded.items()}

    assert decoded == original
    assert encoded["ordinary"] == "text"
    assert encoded["nested"].startswith(FalkorDBMapper.json_prefix)
