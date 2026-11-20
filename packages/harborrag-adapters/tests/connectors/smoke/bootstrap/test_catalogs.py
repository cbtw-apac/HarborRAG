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


def test_provider_shorthand_resolves_a_sole_disabled_connection_and_build_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = ConnectorDefinition(
        name="local-docs", provider="local", enabled=False, settings={"source_path": "."}
    )
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: _catalog(definition))

    assert catalogs.connector_definition("local") is definition
    with pytest.raises(ConnectorConfigurationError, match="'local-docs' is disabled"):
        catalogs.build_connector("local", include_attachments=False)


def _sharepoint_definition() -> ConnectorDefinition:
    return ConnectorDefinition(
        name="sharepoint-main",
        provider="sharepoint",
        settings={"auth_mode": "device_code"},
        setting_environment={"site_url": "SHAREPOINT_SITE_URL"},
        secret_environment={
            "tenant_id": "MICROSOFT_TENANT_ID",
            "client_id": "MICROSOFT_CLIENT_ID",
        },
    )


@pytest.mark.parametrize(
    ("exported", "expected"), [(None, "DEFAULT"), ("engineering", "engineering")]
)
def test_sharepoint_smoke_connector_gets_the_harborrag_tenant(
    monkeypatch: pytest.MonkeyPatch, exported: str | None, expected: str
) -> None:
    monkeypatch.setattr(catalogs, "connector_catalog", lambda: _catalog(_sharepoint_definition()))
    monkeypatch.setenv("SHAREPOINT_SITE_URL", "https://example.sharepoint.com/sites/demo")
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
    if exported is None:
        monkeypatch.delenv("HARBORRAG_TENANT_ID", raising=False)
    else:
        monkeypatch.setenv("HARBORRAG_TENANT_ID", exported)

    connector = catalogs.build_connector("sharepoint-main", include_attachments=False)

    assert connector.provider.config.harborrag_tenant_id == expected
