from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.config import load_connector_catalog
from harborrag_runtime.config.errors import ConnectorConfigurationError

from .conftest import write_config


def test_resolves_secret_environment_and_allows_explicit_overrides(
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
    config_path = write_config(
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


def test_build_passes_runtime_parser_to_attachment_connector(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors:
          jira:
            provider: jira
            environment:
              base_url: TEST_JIRA_BASE_URL
              email: TEST_JIRA_EMAIL
              project_keys: TEST_JIRA_PROJECT_KEY
            secrets:
              token_env: TEST_JIRA_TOKEN
        """,
    )
    parser = object()

    connector = load_connector_catalog(config_path).build(
        "jira",
        environment={
            "TEST_JIRA_BASE_URL": "https://example.atlassian.net",
            "TEST_JIRA_EMAIL": "user@example.test",
            "TEST_JIRA_PROJECT_KEY": "ENG",
            "TEST_JIRA_TOKEN": "secret",
        },
        connector_kwargs={"parser": parser},
    )

    assert connector.provider._attachments.parser is parser
    assert connector.provider.config.project_keys == ["ENG"]


def test_environment_boolean_and_numeric_settings_are_typed(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors:
          jira:
            provider: jira
            environment:
              base_url: TEST_JIRA_BASE_URL
              include_attachments: TEST_INCLUDE_ATTACHMENTS
              max_attachments: TEST_MAX_ATTACHMENTS
            secrets:
              token_env: TEST_JIRA_TOKEN
              email_env: TEST_JIRA_EMAIL
        """,
    )

    connector = load_connector_catalog(config_path).build(
        "jira",
        environment={
            "TEST_JIRA_BASE_URL": "https://example.atlassian.net",
            "TEST_INCLUDE_ATTACHMENTS": "false",
            "TEST_MAX_ATTACHMENTS": "25",
            "TEST_JIRA_TOKEN": "secret",
            "TEST_JIRA_EMAIL": "user@example.test",
        },
    )

    assert connector.provider.config.include_attachments is False
    assert connector.provider.config.max_attachments == 25
    assert isinstance(connector.provider.config.max_attachments, int)


def test_quoted_yaml_boolean_is_coerced_and_invalid_boolean_is_rejected(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        """
        version: 1
        connectors:
          jira:
            provider: jira
            settings:
              base_url: https://example.atlassian.net
              include_attachments: "false"
            secrets:
              token_env: TEST_JIRA_TOKEN
              email_env: TEST_JIRA_EMAIL
        """,
    )
    catalog = load_connector_catalog(config_path)
    environment = {
        "TEST_JIRA_TOKEN": "secret",
        "TEST_JIRA_EMAIL": "user@example.test",
    }

    connector = catalog.build("jira", environment=environment)
    assert connector.provider.config.include_attachments is False

    with pytest.raises(ConnectorConfigurationError, match="must be a boolean"):
        catalog.build(
            "jira",
            environment=environment,
            overrides={"include_attachments": "sometimes"},
        )
