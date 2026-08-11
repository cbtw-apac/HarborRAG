from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.config import ConnectorConfigurationError, load_connector_catalog

from .conftest import write_config


def test_missing_referenced_secret_fails_before_provider_construction(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors:
          docs-repository:
            provider: github
            settings:
              owner: example
              repo: docs
            secrets:
              token_env: TEST_GITHUB_TOKEN
        """,
    )
    catalog = load_connector_catalog(config_path)

    with pytest.raises(
        ConnectorConfigurationError,
        match="TEST_GITHUB_TOKEN",
    ):
        catalog.build("docs-repository", environment={})


def test_missing_referenced_setting_fails_before_provider_construction(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors:
          confluence:
            provider: confluence
            environment:
              base_url: TEST_CONFLUENCE_BASE_URL
              space_key: TEST_CONFLUENCE_SPACE_KEY
            secrets:
              token_env: TEST_CONFLUENCE_TOKEN
              email_env: TEST_CONFLUENCE_EMAIL
        """,
    )
    catalog = load_connector_catalog(config_path)

    with pytest.raises(
        ConnectorConfigurationError,
        match="TEST_CONFLUENCE_BASE_URL",
    ):
        catalog.build("confluence", environment={})


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            """
            connectors: {}
            """,
            "version must be 1",
        ),
        (
            """
            version: 2
            connectors: {}
            """,
            "Unsupported connector configuration version",
        ),
        (
            """
            version: 1
            extra: true
            connectors: {}
            """,
            "unknown field",
        ),
    ],
)
def test_rejects_invalid_root_level_file_shapes(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    """Root-level structural errors are not scoped to a connector and still

    fail the whole file: there is no valid catalog to load independently.
    """
    config_path = write_config(tmp_path, content)

    with pytest.raises(ConnectorConfigurationError, match=message):
        load_connector_catalog(config_path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            """
            version: 1
            connectors:
              broken:
                provider: unknown
            """,
            "unsupported provider",
        ),
        (
            """
            version: 1
            connectors:
              broken:
                provider: github
                settings:
                  owner: example
                  repo: docs
                  typo_setting: true
            """,
            "unknown github setting",
        ),
        (
            """
            version: 1
            connectors:
              broken:
                provider: github
                settings:
                  owner: example
                  repo: docs
                  token: plaintext-secret
            """,
            "must reference secret",
        ),
        (
            """
            version: 1
            connectors:
              broken:
                provider: github
                environment:
                  token: GITHUB_TOKEN
            """,
            "must reference secret",
        ),
        (
            """
            version: 1
            connectors:
              broken:
                provider: confluence
                settings:
                  base_url: https://example.test/wiki
                environment:
                  base_url: CONFLUENCE_BASE_URL
            """,
            "both settings and environment",
        ),
        (
            """
            version: 1
            connectors:
              broken:
                provider: local
                settings:
                  source_path: docs
                  process_file_callback: callback
            """,
            "Python-only",
        ),
        (
            """
            version: 1
            connectors:
              broken:
                provider: local
                settings:
                  source_path: docs
                  allowed_extensions: md
            """,
            "allowed_extensions must be a list",
        ),
    ],
)
def test_rejects_invalid_connector_shapes_lazily(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    """A single connector's invalid definition loads without raising and

    only surfaces its error when that connector is looked up or built.
    """
    config_path = write_config(tmp_path, content)

    catalog = load_connector_catalog(config_path)

    with pytest.raises(ConnectorConfigurationError, match=message):
        catalog.get("broken")
    with pytest.raises(ConnectorConfigurationError, match=message):
        catalog.build("broken")


def test_one_broken_connector_does_not_affect_a_valid_connector(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors:
          broken-github:
            provider: unknown
          local-docs:
            provider: local
            settings:
              source_path: docs
        """,
    )
    (config_path.parent / "docs").mkdir()

    catalog = load_connector_catalog(config_path)

    assert catalog.names(enabled_only=True) == ["local-docs"]
    connector = catalog.build("local-docs", environment={})
    assert connector.provider.config.source_path.name == "docs"

    with pytest.raises(ConnectorConfigurationError, match="broken-github"):
        catalog.get("broken-github")


def test_provider_validation_errors_include_connector_identity(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors:
          broken-github:
            provider: github
            settings:
              owner: example
              repo: docs
              max_retries: -1
        """,
    )
    catalog = load_connector_catalog(config_path)

    with pytest.raises(
        ConnectorConfigurationError,
        match="broken-github.*max_retries",
    ):
        catalog.build("broken-github", environment={})


def test_unknown_catalog_name_has_configuration_error(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors: {}
        """,
    )
    catalog = load_connector_catalog(config_path)

    with pytest.raises(ConnectorConfigurationError, match="missing"):
        catalog.build("missing")
