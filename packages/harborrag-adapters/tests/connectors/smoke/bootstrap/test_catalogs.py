"""Unit tests for the smoke-test connector bootstrap helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap import catalogs
from harborrag_runtime.config import (
    ConnectorCatalog,
    ConnectorConfigurationError,
    ConnectorDefinition,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _catalog(*definitions: ConnectorDefinition) -> ConnectorCatalog:
    return ConnectorCatalog(
        connectors={definition.name: definition for definition in definitions},
        source_path=Path("connectors.yaml"),
        version=1,
    )


def test_build_connector_refuses_disabled_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled_catalog = _catalog(
        ConnectorDefinition(
            name="local-docs",
            provider="local",
            enabled=False,
            settings={"source_path": "."},
        ),
    )
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: disabled_catalog)

    with pytest.raises(ConnectorConfigurationError, match="disabled"):
        catalogs.build_connector("local-docs", include_attachments=False)


def test_connection_id_resolves_exact_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = ConnectorDefinition(name="harborrag-workspace", provider="local")
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: _catalog(definition))

    assert catalogs.connector_definition("harborrag-workspace") is definition


def test_unique_provider_name_remains_supported_as_shorthand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = ConnectorDefinition(name="jira-main", provider="jira")
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: _catalog(definition))

    assert catalogs.connector_definition("jira") is definition


def test_provider_shorthand_rejects_multiple_enabled_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ConnectorDefinition(name="jira-main", provider="jira")
    second = ConnectorDefinition(name="jira-support", provider="jira")
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: _catalog(first, second))

    with pytest.raises(ConnectorConfigurationError, match="multiple configured connections"):
        catalogs.connector_definition("jira")


def test_expected_provider_rejects_wrong_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = ConnectorDefinition(name="jira-main", provider="jira")
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: _catalog(definition))

    with pytest.raises(ConnectorConfigurationError, match="expected 'confluence'"):
        catalogs.connector_definition("jira-main", expected_provider="confluence")
