"""Secret-free, deterministic source submission behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal.schemas import SourceQuery
from harborrag_runtime.temporal.submission import (
    SourceSubmission,
    build_source_input,
)


def _settings(tmp_path: Path) -> RuntimeSettings:
    connectors = tmp_path / "connectors.yaml"
    connectors.write_text(
        """
version: 1
connectors:
  docs:
    provider: jira
    enabled: true
    environment:
      base_url: JIRA_BASE_URL
      project_keys: JIRA_PROJECT_KEYS
    settings:
      deployment_type: cloud
    secrets:
      token_env: JIRA_TOKEN
      email_env: JIRA_EMAIL
""".strip(),
        encoding="utf-8",
    )
    parsers = tmp_path / "parsers.yaml"
    parsers.write_text("version: 1\n", encoding="utf-8")
    return RuntimeSettings(
        connector_config_path=connectors,
        parser_config_path=parsers,
    )


def _submission(**changes: object) -> SourceSubmission:
    values: dict[str, object] = {
        "task_id": "task-1",
        "tenant_id": "tenant-1",
        "connector_name": "docs",
    }
    values.update(changes)
    return SourceSubmission(**values)  # type: ignore[arg-type]


def test_submission_builds_deterministic_secret_free_source_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    environment = {
        "JIRA_BASE_URL": "https://jira.example.test",
        "JIRA_PROJECT_KEYS": "DOCS",
        "JIRA_TOKEN": "first-secret",
    }

    first = build_source_input(
        settings,
        _submission(),
        environment=environment,
    )
    second = build_source_input(
        settings,
        _submission(),
        environment={**environment, "JIRA_TOKEN": "rotated-secret"},
    )

    assert first == second
    assert first.connector_type == "jira"
    assert first.connection_id == "docs"
    assert first.source_scope_id.startswith("scope-")
    assert first.processing.vector_projection_schema == "vector-v2"
    assert first.processing.graph_projection_version == "graph-v3"
    assert first.discovery_page_size == 50
    assert first.discovery_concurrency == 4
    assert "first-secret" not in first.configuration_fingerprint


def test_query_changes_scope_without_changing_connector_configuration(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    environment = {
        "JIRA_BASE_URL": "https://jira.example.test",
        "JIRA_PROJECT_KEYS": "DOCS",
    }

    first = build_source_input(
        settings,
        _submission(query=SourceQuery(pattern="*.md")),
        environment=environment,
    )
    second = build_source_input(
        settings,
        _submission(query=SourceQuery(pattern="*.txt")),
        environment=environment,
    )

    assert first.source_scope_id != second.source_scope_id
    assert first.configuration_fingerprint == second.configuration_fingerprint


def test_execution_toggles_do_not_split_scope_and_connector_can_disable_attachments(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    environment = {
        "JIRA_BASE_URL": "https://jira.example.test",
        "JIRA_PROJECT_KEYS": "DOCS",
    }

    enabled_request = build_source_input(
        settings,
        _submission(
            query=SourceQuery(
                include_attachments=True,
                filters_json='{"include_comments":true,"build_graph":true}',
            )
        ),
        environment=environment,
    )
    disabled_request = build_source_input(
        settings,
        _submission(
            query=SourceQuery(
                include_attachments=False,
                filters_json='{"include_comments":false,"build_graph":false}',
            )
        ),
        environment=environment,
    )

    assert enabled_request.query.include_attachments is False
    assert disabled_request.query.include_attachments is False
    assert enabled_request.source_scope_id == disabled_request.source_scope_id


def test_non_secret_connector_change_updates_configuration_fingerprint(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    first = build_source_input(
        settings,
        _submission(),
        environment={
            "JIRA_BASE_URL": "https://jira-a.example.test",
            "JIRA_PROJECT_KEYS": "DOCS",
        },
    )
    second = build_source_input(
        settings,
        _submission(),
        environment={
            "JIRA_BASE_URL": "https://jira-b.example.test",
            "JIRA_PROJECT_KEYS": "DOCS",
        },
    )

    assert first.configuration_fingerprint != second.configuration_fingerprint


def test_explicit_connection_and_scope_are_preserved(tmp_path: Path) -> None:
    source = build_source_input(
        _settings(tmp_path),
        _submission(
            connection_id="shared-files",
            source_scope_id="engineering-docs",
        ),
        environment={
            "JIRA_BASE_URL": "https://jira.example.test",
            "JIRA_PROJECT_KEYS": "DOCS",
        },
    )

    assert source.connection_id == "shared-files"
    assert source.source_scope_id == "engineering-docs"


def test_missing_non_secret_connector_environment_is_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConnectorConfigurationError, match="JIRA_BASE_URL"):
        build_source_input(
            _settings(tmp_path),
            _submission(),
            environment={},
        )


@pytest.mark.parametrize(
    "filters_json",
    [
        '{"access_token":"secret"}',
        '{"nested":{"provider_api_key":"secret"}}',
    ],
)
def test_source_query_rejects_credentials(filters_json: str) -> None:
    with pytest.raises(ValueError, match="cannot contain credentials"):
        SourceQuery(filters_json=filters_json)


@pytest.mark.parametrize("updated_after", ["not-a-date", "2026-07-31T10:00:00"])
def test_source_query_requires_an_aware_iso_timestamp(updated_after: str) -> None:
    with pytest.raises(ValueError, match="updated_after"):
        SourceQuery(updated_after=updated_after)


def _settings_with_ingestion_defaults(
    tmp_path: Path,
    *,
    batch_size: int,
    document_concurrency: int,
) -> RuntimeSettings:
    settings = _settings(tmp_path)
    temporal_config = tmp_path / "temporal.yaml"
    temporal_config.write_text(
        f"version: 1\ningestion:\n  batch_size: {batch_size}\n"
        f"  document_concurrency: {document_concurrency}\n",
        encoding="utf-8",
    )
    return settings.__class__(
        connector_config_path=settings.connector_config_path,
        parser_config_path=settings.parser_config_path,
        temporal_config_path=temporal_config,
    )


def test_unset_batching_falls_back_to_the_configured_ingestion_defaults(
    tmp_path: Path,
) -> None:
    settings = _settings_with_ingestion_defaults(
        tmp_path,
        batch_size=5,
        document_concurrency=5,
    )
    environment = {
        "JIRA_BASE_URL": "https://jira.example.test",
        "JIRA_PROJECT_KEYS": "DOCS",
    }

    source = build_source_input(settings, _submission(), environment=environment)

    assert source.batch_size == 5
    assert source.document_concurrency == 5


def test_explicit_batching_overrides_the_configured_ingestion_defaults(
    tmp_path: Path,
) -> None:
    settings = _settings_with_ingestion_defaults(
        tmp_path,
        batch_size=5,
        document_concurrency=5,
    )
    environment = {
        "JIRA_BASE_URL": "https://jira.example.test",
        "JIRA_PROJECT_KEYS": "DOCS",
    }

    source = build_source_input(
        settings,
        _submission(batch_size=200, document_concurrency=8),
        environment=environment,
    )

    assert source.batch_size == 200
    assert source.document_concurrency == 8
