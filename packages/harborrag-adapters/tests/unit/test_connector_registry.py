"""White-box unit tests for the connector registry."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.registry import ConnectorRegistry
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
