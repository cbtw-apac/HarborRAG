"""The connection catalog read behind GET /v1/connections."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_app.workflow_control.ingestion.connections import connection_catalog
from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings

_CATALOG = """
version: 1
connectors:
  zeta-local:
    provider: local
    enabled: true
    environment:
      source_path: LOCAL_SOURCE_PATH
  alpha-confluence:
    provider: confluence
    enabled: true
    environment:
      base_url: CONFLUENCE_BASE_URL
      space_key: CONFLUENCE_SPACE_KEY
    secrets:
      token_env: CONFLUENCE_TOKEN
      email_env: CONFLUENCE_EMAIL
  retired-jira:
    provider: jira
    enabled: false
"""


def _settings(tmp_path: Path, catalog: str) -> RuntimeSettings:
    path = tmp_path / "connectors.yaml"
    path.write_text(catalog, encoding="utf-8")
    return RuntimeSettings(connector_config_path=path)


def test_only_enabled_connections_are_listed_alphabetically(tmp_path: Path) -> None:
    """A disabled connection cannot be submitted, so it is not offered."""
    catalog = connection_catalog(_settings(tmp_path, _CATALOG))

    assert catalog == {
        "items": [
            {"connection_id": "alpha-confluence", "source_type": "confluence"},
            {"connection_id": "zeta-local", "source_type": "local"},
        ]
    }


def test_credentials_and_settings_never_reach_the_response(tmp_path: Path) -> None:
    """Only identity is published; environment and secret references stay server-side."""
    catalog = connection_catalog(_settings(tmp_path, _CATALOG))

    for item in catalog["items"]:  # type: ignore[union-attr]
        assert set(item) == {"connection_id", "source_type"}
    assert "CONFLUENCE_TOKEN" not in repr(catalog)


def test_an_invalid_definition_is_skipped_not_fatal(tmp_path: Path) -> None:
    """One unusable definition must not hide every working connection."""
    catalog = connection_catalog(
        _settings(
            tmp_path,
            """
version: 1
connectors:
  good-local:
    provider: local
    enabled: true
  broken:
    provider: not-a-registered-provider
    enabled: true
""",
        )
    )

    assert catalog == {"items": [{"connection_id": "good-local", "source_type": "local"}]}


def test_an_unreadable_catalog_fails_the_request(tmp_path: Path) -> None:
    """A missing configuration file is a deployment fault, not an empty list."""
    settings = RuntimeSettings(connector_config_path=tmp_path / "absent.yaml")

    with pytest.raises(HarborConfigurationError):
        connection_catalog(settings)
