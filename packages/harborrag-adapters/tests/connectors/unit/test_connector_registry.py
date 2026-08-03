"""White-box unit tests for the connector registry."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.document_transform import ConnectorDocumentTransform
from harborrag_adapters.connectors.registry import (
    ConnectorProviderDefinition,
    ConnectorRegistry,
    connector_registry,
)
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class _DummyConnector(BaseConnector):
    provider_name = "dummy"

    def discover(self, query=None) -> Iterator[SourceRecord]:
        yield from ()

    def load(self, record: SourceRecord) -> RawDocument:
        raise NotImplementedError


class _OtherConnector(_DummyConnector):
    provider_name = "other"


def test_registry_registers_name_and_aliases():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector, aliases=["dm", "test-dummy"])

    assert registry.get_class("dummy") is _DummyConnector
    assert registry.get_class("dm") is _DummyConnector
    assert registry.get_class("test-dummy") is _DummyConnector


def test_registry_create_instantiates():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    assert isinstance(registry.create("dummy"), _DummyConnector)


def test_registry_exposes_one_definition_for_canonical_name_and_aliases():
    registry = ConnectorRegistry()
    definition = ConnectorProviderDefinition(
        name="dummy",
        provider_cls=_DummyConnector,
        aliases=("dm",),
        config_factory=dict,
        constructor_dependencies={"parser": "attachment_parser"},
        config_path_fields=("source_path",),
        document_kind="page",
    )

    registry.register_provider(definition)

    assert registry.get_definition("dm") is definition
    assert registry.canonical_name("dm") == "dummy"
    assert registry.canonical_names() == ["dummy"]
    assert definition.constructor_dependencies == {"parser": "attachment_parser"}


@pytest.mark.parametrize("provider_name", ["confluence", "jira", "local"])
def test_builtin_provider_owns_its_document_transform(provider_name: str) -> None:
    definition = connector_registry.get_definition(provider_name)

    assert definition.document_transform_factory is not None
    assert isinstance(definition.document_transform_factory(), ConnectorDocumentTransform)


@pytest.mark.parametrize("provider_name", ["github", "sharepoint"])
def test_generic_provider_does_not_require_a_document_transform(provider_name: str) -> None:
    definition = connector_registry.get_definition(provider_name)

    assert definition.document_transform_factory is None


def test_registry_duplicate_key_different_class_raises():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("dummy", _OtherConnector)


def test_registry_same_class_reregister_is_noop():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    registry.register("dummy", _DummyConnector)
    assert registry.get_class("dummy") is _DummyConnector


def test_registry_replace_allows_override():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector)
    registry.register("dummy", _OtherConnector, replace=True)
    assert registry.get_class("dummy") is _OtherConnector


def test_registry_names_are_sorted():
    registry = ConnectorRegistry()
    registry.register("zeta", _DummyConnector)
    registry.register("alpha", _OtherConnector, aliases=["mid"])
    assert registry.names() == ["alpha", "mid", "zeta"]


def test_registry_unknown_name_raises():
    from harborrag_adapters.connectors.exceptions import ConnectorNotFoundError

    registry = ConnectorRegistry()
    with pytest.raises(ConnectorNotFoundError):
        registry.get_class("nope")


def test_registry_unregister_removes_name():
    registry = ConnectorRegistry()
    registry.register("dummy", _DummyConnector, aliases=["dm"])

    registry.unregister("dm")

    assert "dm" not in registry.names()
    assert registry.get_class("dummy") is _DummyConnector


def test_registry_unregister_unknown_name_raises():
    from harborrag_adapters.connectors.exceptions import ConnectorNotFoundError

    registry = ConnectorRegistry()
    with pytest.raises(ConnectorNotFoundError):
        registry.unregister("nope")
