"""Per-connection tenant allowlists: parsing, lookup, listing, and submission."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.config import ConnectorConfigurationError, load_connector_catalog
from harborrag_runtime.config.errors import ConnectorNotFoundError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.execution.source_submission import prepare_source_submission
from harborrag_runtime.ingestion_contracts import SourceSubmission

from .conftest import write_config

_CATALOG = """
version: 1
connectors:
  acme-jira:
    provider: jira
    enabled: true
    tenants: [acme]
    environment:
      base_url: JIRA_BASE_URL
      project_keys: JIRA_PROJECT_KEYS
    settings:
      deployment_type: cloud
    secrets:
      token_env: JIRA_TOKEN
      email_env: JIRA_EMAIL
  shared-local:
    provider: local
    enabled: true
  retired-local:
    provider: local
    enabled: false
    tenants: [globex]
  broken:
    provider: not-a-provider
"""


def test_a_tenant_allowlist_is_parsed_and_absent_means_every_tenant(tmp_path: Path) -> None:
    catalog = load_connector_catalog(write_config(tmp_path, _CATALOG))

    assert catalog.get("acme-jira").tenants == frozenset({"acme"})
    assert catalog.get("shared-local").tenants is None
    assert catalog.get("acme-jira").allows_tenant("acme") is True
    assert catalog.get("acme-jira").allows_tenant("globex") is False
    assert catalog.get("shared-local").allows_tenant("globex") is True


def test_unknown_disallowed_and_broken_names_are_the_same_not_found(tmp_path: Path) -> None:
    catalog = load_connector_catalog(write_config(tmp_path, _CATALOG))

    messages = set()
    for name in ("acme-jira", "no-such-connection", "broken"):
        with pytest.raises(ConnectorNotFoundError) as caught:
            catalog.get_for_tenant(name, "globex")
        messages.add(str(caught.value).replace(repr(name), "<name>"))

    assert messages == {"Unknown configured connector: <name>"}
    assert catalog.get_for_tenant("acme-jira", "acme").name == "acme-jira"
    assert catalog.get_for_tenant("shared-local", "globex").name == "shared-local"


def test_listing_is_filtered_to_the_callers_tenants(tmp_path: Path) -> None:
    catalog = load_connector_catalog(write_config(tmp_path, _CATALOG))

    assert catalog.names_for_tenants(frozenset({"globex"})) == ["shared-local"]
    assert catalog.names_for_tenants(frozenset({"acme"})) == ["acme-jira", "shared-local"]
    assert catalog.names_for_tenants(None) == ["acme-jira", "shared-local"]


def test_unbound_connections_are_reported_once_any_allowlist_exists(tmp_path: Path) -> None:
    catalog = load_connector_catalog(write_config(tmp_path, _CATALOG))
    (tmp_path / "single").mkdir()  # write_config creates only the final "config" directory
    single_tenant = load_connector_catalog(
        write_config(tmp_path / "single", "version: 1\nconnectors:\n  docs:\n    provider: local\n")
    )

    assert catalog.unbound_connection_names() == ["shared-local"]
    assert single_tenant.unbound_connection_names() == []


@pytest.mark.parametrize(
    "tenants",
    ["acme", "[]", "['*']", "[' acme']", "['']", "[1]", '""', "~"],
)
def test_an_invalid_allowlist_fails_that_connection_only(tmp_path: Path, tenants: str) -> None:
    catalog = load_connector_catalog(
        write_config(
            tmp_path,
            f"version: 1\nconnectors:\n  bad:\n    provider: local\n    tenants: {tenants}\n"
            "  good:\n    provider: local\n",
        )
    )

    with pytest.raises(ConnectorConfigurationError, match="tenants"):
        catalog.get("bad")
    assert catalog.names() == ["good"]


def test_submission_refuses_a_connection_outside_the_tenants_allowlist(tmp_path: Path) -> None:
    connectors = write_config(tmp_path, _CATALOG)
    parsers = tmp_path / "parsers.yaml"
    parsers.write_text("version: 1\n", encoding="utf-8")
    settings = RuntimeSettings(connector_config_path=connectors, parser_config_path=parsers)
    environment = {
        "JIRA_BASE_URL": "https://jira.example.test",
        "JIRA_PROJECT_KEYS": "DOCS",
        "JIRA_TOKEN": "secret",
        "JIRA_EMAIL": "bot@example.test",
    }

    def submit(tenant_id: str):
        return prepare_source_submission(
            settings,
            SourceSubmission(task_id="task-1", tenant_id=tenant_id, connector_name="acme-jira"),
            environment=environment,
        )

    assert submit("acme").connector_type == "jira"
    with pytest.raises(ConnectorNotFoundError, match="Unknown configured connector: 'acme-jira'"):
        submit("globex")
