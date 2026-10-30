"""Source-type registry: published schemas, validation, and parity with the YAML catalog."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from harborrag_runtime.config import load_connector_catalog
from harborrag_runtime.config.connectors.models import (
    ConnectionValidationError,
    source_types,
    to_connector_config,
    validate,
)
from harborrag_runtime.config.connectors.providers import coerce_config_values

from .conftest import REPO_ROOT, write_config

_SPEC_UI_KEYS = {"group", "order", "widget", "help", "placeholder"}
_SPEC_TYPE_KEYS = {
    "source_type",
    "name",
    "category",
    "icon",
    "version",
    "docs_url",
    "auth_methods",
    "config_schema",
    "ui",
    "capabilities",
}
_MIGRATION = (
    REPO_ROOT
    / "packages/harborrag-adapters/src/harborrag_adapters/repositories/database"
    / "control_plane/alembic/versions/0041_editable_connections.py"
)

_ALL_TYPES = """
version: 1
connectors:
  engineering-github:
    provider: github
    environment:
      repository_url: GITHUB_REPOSITORY_URL
    settings:
      branch: main
      root_path: docs
      allowed_extensions: [md, txt, pdf]
      requests_per_minute: 120
      request_timeout_seconds: 30
      max_retries: 3
      backoff_factor: 0.5
    secrets:
      token_env: GITHUB_TOKEN
  confluence-main:
    provider: confluence
    environment:
      base_url: CONFLUENCE_BASE_URL
      space_key: CONFLUENCE_SPACE_KEY
    settings:
      deployment_type: cloud
      content_types: [page]
      include_comments: false
      allowed_extensions: [pdf, docx]
      excluded_extensions: [exe]
      max_comments: 1000
      requests_per_minute: 600
      page_size: 100
    secrets:
      token_env: CONFLUENCE_TOKEN
      email_env: CONFLUENCE_EMAIL
  jira-main:
    provider: jira
    environment:
      base_url: JIRA_BASE_URL
    settings:
      deployment_type: cloud
      project_keys: [ENG]
      include_comments: true
      allowed_extensions: [pdf, docx]
      excluded_extensions: [exe]
      include_changelog: false
      max_changelog_items: 1000
      requests_per_minute: 60
      page_size: 50
    secrets:
      token_env: JIRA_TOKEN
      email_env: JIRA_EMAIL
  knowledge-sharepoint:
    provider: sharepoint
    environment:
      site_url: SHAREPOINT_SITE_URL
    settings:
      drive_name: Documents
      root_path: Shared Documents
      recursive: true
      allowed_extensions: [md, txt, pdf, docx]
      include_pages: false
      requests_per_minute: 120
      page_size: 200
    secrets:
      tenant_id_env: MICROSOFT_TENANT_ID
      client_id_env: MICROSOFT_CLIENT_ID
      client_secret_env: MICROSOFT_CLIENT_SECRET
  delegated-sharepoint:
    provider: sharepoint
    settings:
      auth_mode: device_code
      site_url: https://acme.sharepoint.com/sites/knowledge
    secrets:
      tenant_id_env: MICROSOFT_TENANT_ID
      client_id_env: MICROSOFT_CLIENT_ID
"""

_ENVIRONMENT = {
    "GITHUB_REPOSITORY_URL": "https://github.com/acme/handbook",
    "GITHUB_TOKEN": "ghp_example",
    "CONFLUENCE_BASE_URL": "https://acme.atlassian.net/wiki",
    "CONFLUENCE_SPACE_KEY": "ENG",
    "CONFLUENCE_TOKEN": "conf-token",
    "CONFLUENCE_EMAIL": "bot@acme.com",
    "JIRA_BASE_URL": "https://acme.atlassian.net",
    "JIRA_TOKEN": "jira-token",
    "JIRA_EMAIL": "bot@acme.com",
    "SHAREPOINT_SITE_URL": "https://acme.sharepoint.com/sites/knowledge",
    "MICROSOFT_TENANT_ID": "tenant",
    "MICROSOFT_CLIENT_ID": "client",
    "MICROSOFT_CLIENT_SECRET": "client-secret",
}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0041", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("spec", source_types.all(), ids=lambda spec: spec.source_type)
def test_every_type_publishes_the_spec_shape(spec: Any) -> None:
    described = spec.describe()

    assert set(described) == _SPEC_TYPE_KEYS
    assert described["category"] in {"issue_tracker", "wiki", "files", "code"}
    assert set(described["capabilities"]) == {"incremental", "acl_sync", "attachments"}
    assert described["capabilities"]["acl_sync"] is False
    for method in described["auth_methods"]:
        assert method["id"] in {"api_token", "oauth", "pat", "app", "none"}
        assert method["schema"]["type"] == "object"
    for name, hint in described["ui"].items():
        assert set(hint) == _SPEC_UI_KEYS, name
        assert hint["widget"] in {"text", "secret", "select", "textarea", "path"}
    for name in spec.secret_fields:
        assert described["ui"][name]["widget"] == "secret"
        schemas = [m["schema"]["properties"] for m in described["auth_methods"]]
        assert all(props[name]["writeOnly"] is True for props in schemas if name in props)


def test_jira_accepts_the_spec_example_body() -> None:
    validated = validate(
        "jira",
        {
            "base_url": "https://acme.atlassian.net",
            "email": "bot@acme.com",
            "api_token": "ATATT3xFfGF0…",
        },
        {"jql": "project = CPM"},
    )

    assert validated.credentials["method"] == "api_token"
    assert validated.config == {"jql": "project = CPM"}
    assert source_types.get("jira").query_filters(validated.config) == {"jql": "project = CPM"}


def test_validation_lists_every_failing_field() -> None:
    with pytest.raises(ConnectionValidationError) as caught:
        validate(
            "jira",
            {"base_url": "not-a-url", "method": "api_token"},
            {"bogus": 1},
            {"page_size": 0},
        )

    fields = {issue.field: issue.issue for issue in caught.value.issues}
    assert fields["credentials.email"] == "required"
    assert fields["credentials.api_token"] == "required"
    assert fields["config.bogus"] == "unknown field"
    assert "credentials.base_url" in fields and "tuning.page_size" in fields
    assert caught.value.details == {"errors": [issue.as_dict() for issue in caught.value.issues]}


def test_unknown_method_and_unknown_type_are_field_errors() -> None:
    with pytest.raises(ConnectionValidationError) as method_error:
        validate("github", {"method": "oauth"}, {})
    assert method_error.value.issues[0].field == "credentials.method"

    with pytest.raises(ConnectionValidationError) as type_error:
        validate("notion", {}, {})
    assert type_error.value.issues[0].field == "source_type"


def test_secret_fields_accept_values_and_references() -> None:
    for value in ("ghp_x", {"secret_ref": "env:GITHUB_TOKEN"}):
        validated = validate("github", {"method": "pat", "token": value}, {"repo": "r"})
        assert validated.credentials["token"] == value


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("local", "local_file"), ("filesystem", "local_file"), ("jira_cloud", "jira")],
)
def test_aliases_resolve_to_canonical_types(alias: str, canonical: str) -> None:
    assert source_types.get(alias).source_type == canonical


def test_unresolved_secret_reference_is_reported() -> None:
    with pytest.raises(ConnectionValidationError, match="does not match"):
        to_connector_config("github", {"token": {"secret_ref": "env:X"}}, {"repo": "r"}, {}, {})


def test_migration_0041_field_layout_matches_the_registry() -> None:
    migration = _load_migration()

    for spec in source_types.all():
        frozen = migration._CREDENTIAL_FIELDS[spec.source_type]
        renamed = {
            migration._CREDENTIAL_RENAMES.get(spec.source_type, {}).get(f, f) for f in frozen
        }
        assert renamed == set(spec.credential_fields), spec.source_type
        assert set(spec.tuning_fields) <= migration._TUNING_FIELDS
    assert migration._SOURCE_TYPES == {
        **{name: spec.source_type for spec in source_types.all() for name in spec.aliases},
        **{spec.source_type: spec.source_type for spec in source_types.all()},
        "local": "local_file",
    }


def test_example_yaml_entries_split_into_valid_parts(tmp_path: Path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    catalog = load_connector_catalog(REPO_ROOT / "config" / "connectors.example.yaml")

    for name in catalog.names():
        definition = catalog.get(name)
        flat = definition.resolve_settings(environment={"LOCAL_SOURCE_PATH": str(source)})
        spec = source_types.get(definition.provider)
        credentials, config, tuning = spec.split_flat(flat)
        validate(spec.source_type, credentials, config, tuning)


@pytest.mark.parametrize(
    "name",
    [
        "engineering-github",
        "confluence-main",
        "jira-main",
        "knowledge-sharepoint",
        "delegated-sharepoint",
        "workspace",
    ],
)
def test_to_connector_config_builds_the_same_dataclass_as_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    for key in _ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    source = tmp_path / "workspace-docs"
    source.mkdir()
    content = _ALL_TYPES + (
        "  workspace:\n    provider: local\n    settings:\n"
        f"      source_path: {source}\n      allowed_extensions: [md, pdf]\n      max_depth: 3\n"
    )
    catalog = load_connector_catalog(write_config(tmp_path, content))
    definition = catalog.get(name)
    flat = definition.resolve_settings(environment=_ENVIRONMENT)
    factory = source_types.provider_factory(definition.provider)
    expected = factory(**coerce_config_values(factory, flat))

    spec = source_types.get(definition.provider)
    credentials, config, tuning = spec.split_flat(flat)
    validated = validate(spec.source_type, credentials, config, tuning)
    built = to_connector_config(
        spec.source_type, validated.credentials, validated.config, validated.tuning, {}
    )

    assert built == expected
