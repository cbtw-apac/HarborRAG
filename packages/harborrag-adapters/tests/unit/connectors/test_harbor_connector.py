"""White-box unit tests for the HarborConnector factory facade."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.connectors.registry import connector_registry
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class _StubConnector(BaseConnector):
    provider_name = "harbor-connector-stub"
    capabilities = ConnectorCapabilities(attachments=True)

    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.records = [
            SourceRecord(
                id="1",
                source_type="stub",
                locator="stub://1",
                metadata={},
            )
        ]

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        yield from self.records

    def load(self, record: SourceRecord) -> RawDocument:
        return RawDocument(
            id=record.id,
            source="stub",
            content_type="text/plain",
            content=b"stub content",
            metadata={},
        )


@pytest.fixture(autouse=True)
def _register_stub():
    connector_registry.register("harbor-connector-stub", _StubConnector)
    try:
        yield
    finally:
        connector_registry.unregister("harbor-connector-stub")


def test_init_creates_provider_from_registry():
    connector = HarborConnector("harbor-connector-stub", flag=True)
    assert isinstance(connector.provider, _StubConnector)
    assert connector.provider_name == "harbor-connector-stub"
    assert connector.provider.init_kwargs == {"flag": True}


def test_capabilities_proxies_provider():
    connector = HarborConnector("harbor-connector-stub")
    assert connector.capabilities == ConnectorCapabilities(attachments=True)


def test_discover_proxies_provider():
    connector = HarborConnector("harbor-connector-stub")
    records = list(connector.discover(ConnectorQuery(limit=1)))
    assert [r.id for r in records] == ["1"]


def test_load_proxies_provider():
    connector = HarborConnector("harbor-connector-stub")
    record = next(iter(connector.discover()))
    document = connector.load(record)
    assert document.id == "1"


def test_load_raw_documents_proxies_provider():
    connector = HarborConnector("harbor-connector-stub")
    documents = list(connector.load_raw_documents())
    assert [d.id for d in documents] == ["1"]


def test_providers_lists_registered_names():
    assert "harbor-connector-stub" in HarborConnector.providers()
