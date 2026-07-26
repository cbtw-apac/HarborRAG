from __future__ import annotations

from pathlib import Path

from harborrag_runtime.config import load_connector_catalog

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
