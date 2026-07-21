"""Unit tests for the shared connector metadata contract."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from harborrag_adapters.connectors.confluence.schemas import ConfluenceMetadata
from harborrag_adapters.connectors.github.schemas import GitHubMetadata
from harborrag_adapters.connectors.jira.schemas import JiraMetadata
from harborrag_adapters.connectors.local.schemas import LocalFileMetadata
from harborrag_adapters.connectors.schemas import ConnectorMetadata
from harborrag_adapters.connectors.sharepoint.schemas import SharePointMetadata

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@dataclass(slots=True, kw_only=True)
class ExampleMetadata(ConnectorMetadata):
    """Concrete test schema with provider-specific nested data."""

    source_system: ClassVar[str] = "example"

    provider_data: dict[str, Any]


@pytest.mark.parametrize(
    ("metadata_type", "source_system"),
    [
        (LocalFileMetadata, "local"),
        (GitHubMetadata, "github"),
        (ConfluenceMetadata, "confluence"),
        (JiraMetadata, "jira"),
        (SharePointMetadata, "sharepoint"),
    ],
)
def test_provider_metadata_inherits_shared_contract(metadata_type, source_system):
    assert issubclass(metadata_type, ConnectorMetadata)
    assert metadata_type.source_system == source_system
    assert "source_system" not in inspect.signature(metadata_type).parameters


def test_to_dict_includes_common_fields_and_serializes_nested_datetimes():
    created_at = datetime(2024, 1, 2, tzinfo=UTC)
    nested_at = datetime(2024, 2, 3, tzinfo=UTC)
    metadata = ExampleMetadata(
        record_id="record-1",
        title="Example",
        checksum="abc123",
        created_at=created_at,
        provider_data={"events": [{"at": nested_at}]},
    )

    payload = metadata.to_dict()

    assert payload == {
        "source_system": "example",
        "metadata_schema_version": 1,
        "record_id": "record-1",
        "title": "Example",
        "checksum": "abc123",
        "created_at": created_at.isoformat(),
        "updated_at": None,
        "provider_data": {"events": [{"at": nested_at.isoformat()}]},
    }
    json.dumps(payload)
