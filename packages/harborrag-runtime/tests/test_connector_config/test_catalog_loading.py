from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_runtime.config import load_connector_catalog

from .conftest import REPO_ROOT, write_config


def test_repository_example_is_valid_and_builds_enabled_local_connector(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="harborrag.runtime.config.connectors"):
        catalog = load_connector_catalog(REPO_ROOT / "config" / "connectors.example.yaml")

    assert catalog.names(enabled_only=True) == ["harborrag-workspace"]
    assert catalog.names() == ["harborrag-workspace"]
    connectors = catalog.build_enabled(environment={"LOCAL_SOURCE_PATH": str(REPO_ROOT / "docs")})
    assert list(connectors) == ["harborrag-workspace"]
    assert "Connector catalog loaded" in caplog.text
    assert "definitions=1 enabled=1" in caplog.text


def test_repository_runtime_connections_match_the_public_api_contract() -> None:
    catalog = load_connector_catalog(REPO_ROOT / "config" / "connectors.yaml")

    assert catalog.names() == [
        "confluence-main",
        "harborrag-workspace",
        "jira-main",
    ]
    assert {name: catalog.get(name).provider for name in catalog.names()} == {
        "confluence-main": "confluence",
        "harborrag-workspace": "local",
        "jira-main": "jira",
    }


def test_loads_named_connectors_and_builds_file_relative_local_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    (source / "readme.md").write_text("hello", encoding="utf-8")
    config_path = write_config(
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
    config_path = write_config(
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


def test_override_source_path_resolves_from_config_directory_not_process_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    config_path = write_config(
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
    source = config_path.parent / "override-docs"
    source.mkdir()
    monkeypatch.chdir(other_cwd)

    connector = load_connector_catalog(config_path).build(
        "local-docs",
        environment={"TEST_LOCAL_SOURCE_PATH": "/env/should/not/be/used"},
        overrides={"source_path": "override-docs"},
    )

    assert connector.provider.config.source_path == source.resolve()
