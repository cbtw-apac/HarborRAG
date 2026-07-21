from __future__ import annotations

from typing import Literal

import pytest
from harborrag_adapters.repositories.errors import HarborStorageConfigurationError
from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_adapters.repositories.shared.provider_map import ProviderMap
from harborrag_core.schemas.storage import StorageFamily
from pydantic import BaseModel


class _ExampleConfig(RepositoryConfig):
    backend: Literal["example"] = "example"
    value: int = 1


class _OtherConfig(RepositoryConfig):
    backend: Literal["other"] = "other"


class _ImposterConfig(RepositoryConfig):
    """Shares the 'example' backend name but is not the registered config type."""

    backend: Literal["example"] = "example"


class _ExampleCapabilities(BaseModel):
    supports_x: bool = True


class _ExamplePlugin(RepositoryPlugin[_ExampleConfig, str]):
    name = "example"
    family = StorageFamily.CACHE
    config_type = _ExampleConfig
    capabilities = _ExampleCapabilities()

    def create(self, config: _ExampleConfig, dependencies: RepositoryDependencies) -> str:
        del dependencies
        return f"product-{config.value}"


class _WrongFamilyPlugin(RepositoryPlugin[_ExampleConfig, str]):
    name = "wrong-family"
    family = StorageFamily.DATABASE
    config_type = _ExampleConfig

    def create(self, config: _ExampleConfig, dependencies: RepositoryDependencies) -> str:
        del config, dependencies
        return "unused"


def test_register_rejects_plugin_from_a_different_family() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    with pytest.raises(ValueError, match="belongs to"):
        providers.register(_WrongFamilyPlugin())


def test_register_rejects_duplicate_backend_name() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    providers.register(_ExamplePlugin())
    with pytest.raises(ValueError, match="already registered"):
        providers.register(_ExamplePlugin())


def test_names_returns_registered_backends_in_sorted_order() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    providers.register(_ExamplePlugin())
    assert providers.names() == ("example",)


def test_capabilities_returns_the_plugins_published_model() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    providers.register(_ExamplePlugin())
    capabilities = providers.capabilities("example")
    assert isinstance(capabilities, _ExampleCapabilities)
    assert capabilities.supports_x is True


def test_capabilities_raises_for_unregistered_backend() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    with pytest.raises(HarborStorageConfigurationError, match="unsupported"):
        providers.capabilities("missing")


def test_create_validates_and_dispatches_to_the_registered_plugin() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    providers.register(_ExamplePlugin())
    product = providers.create(
        backend="example",
        instance_name="test",
        options={"value": 7},
        dependencies=None,
    )
    assert product == "product-7"


def test_create_raises_for_unregistered_backend_with_helpful_message() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    providers.register(_ExamplePlugin())
    with pytest.raises(HarborStorageConfigurationError, match="unsupported cache backend"):
        providers.create(
            backend="missing",
            instance_name="test",
            options=None,
            dependencies=None,
        )


def test_create_from_config_dispatches_when_config_type_matches() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    providers.register(_ExamplePlugin())
    product = providers.create_from_config(_ExampleConfig(value=9), None)
    assert product == "product-9"


def test_create_from_config_rejects_mismatched_config_type() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    providers.register(_ExamplePlugin())

    with pytest.raises(TypeError, match="requires"):
        providers.create_from_config(_ImposterConfig(), None)


def test_create_from_config_raises_for_unregistered_backend() -> None:
    providers: ProviderMap[str] = ProviderMap(StorageFamily.CACHE)
    with pytest.raises(HarborStorageConfigurationError, match="unsupported"):
        providers.create_from_config(_OtherConfig(), None)
