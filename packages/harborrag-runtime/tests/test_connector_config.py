from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from harborrag_runtime.config import (
    ConnectorConfigurationError,
    load_connector_catalog,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config" / "connectors.yaml"
    config_path.parent.mkdir()
    config_path.write_text(dedent(content), encoding="utf-8")
    return config_path


def test_repository_example_is_valid_and_builds_enabled_local_connector() -> None:
    catalog = load_connector_catalog(REPO_ROOT / "config" / "connectors.yaml")

    assert catalog.names(enabled_only=True) == ["local-docs"]
    assert catalog.names() == ["local-docs"]
    connectors = catalog.build_enabled(
        environment={"LOCAL_SOURCE_PATH": str(REPO_ROOT / "docs")}
    )
    assert list(connectors) == ["local-docs"]


def test_loads_named_connectors_and_builds_file_relative_local_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    (source / "readme.md").write_text("hello", encoding="utf-8")
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        connectors:
          local-docs:
            provider: filesystem
            settings:
              source_path: ../docs
              allowed_extensions: [md]
          later-github:
            provider: github
            enabled: false
            settings:
              owner: example
              repo: docs
            secrets:
              token_env: GITHUB_TOKEN
        """,
    )

    catalog = load_connector_catalog(config_path)

    assert catalog.version == 1
    assert catalog.names() == ["later-github", "local-docs"]
    assert catalog.names(enabled_only=True) == ["local-docs"]
    connectors = catalog.build_enabled(environment={})
    local = connectors["local-docs"].provider
    assert local.config.source_path == source.resolve()
    assert local.config.allowed_extensions == {".md"}


def test_environment_local_source_resolves_from_process_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        connectors:
          local-docs:
            provider: local
            environment:
              source_path: TEST_LOCAL_SOURCE_PATH
        """,
    )
    monkeypatch.chdir(tmp_path)

    connector = load_connector_catalog(config_path).build(
        "local-docs",
        environment={"TEST_LOCAL_SOURCE_PATH": "./docs"},
    )

    assert connector.provider.config.source_path == source.resolve()


def test_resolves_secret_environment_and_allows_explicit_overrides(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        connectors:
          docs-repository:
            provider: github
            settings:
              owner: example
              repo: docs
              branch: main
            secrets:
              token_env: TEST_GITHUB_TOKEN
        """,
    )
    catalog = load_connector_catalog(config_path)

    connector = catalog.build(
        "docs-repository",
        environment={"TEST_GITHUB_TOKEN": "from-environment"},
        overrides={"branch": "release", "token": "explicit"},
    )

    assert connector.provider.config.branch == "release"
    assert connector.provider.config.token == "explicit"


def test_resolves_non_secret_settings_from_environment(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        connectors:
          docs-repository:
            provider: github
            environment:
              repository_url: TEST_GITHUB_REPOSITORY_URL
            settings:
              branch: main
            secrets:
              token_env: TEST_GITHUB_TOKEN
        """,
    )

    connector = load_connector_catalog(config_path).build(
        "docs-repository",
        environment={
            "TEST_GITHUB_REPOSITORY_URL": "https://github.com/example/docs",
            "TEST_GITHUB_TOKEN": "secret",
        },
    )

    assert connector.provider.config.owner == "example"
    assert connector.provider.config.repo == "docs"


def test_missing_referenced_secret_fails_before_provider_construction(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
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
    config_path = _write_config(
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
    ],
)
def test_rejects_invalid_file_shapes(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    config_path = _write_config(tmp_path, content)

    with pytest.raises(ConnectorConfigurationError, match=message):
        load_connector_catalog(config_path)


def test_provider_validation_errors_include_connector_identity(tmp_path: Path) -> None:
    config_path = _write_config(
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
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        connectors: {}
        """,
    )
    catalog = load_connector_catalog(config_path)

    with pytest.raises(ConnectorConfigurationError, match="missing"):
        catalog.build("missing")
