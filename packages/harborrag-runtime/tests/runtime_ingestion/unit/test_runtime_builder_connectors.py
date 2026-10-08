"""Connector construction isolates per-connector configuration failures."""

from __future__ import annotations

from pathlib import Path

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.runtime_builder import IngestionRuntimeBuilder


def _write_connector_config(path: Path, source_dir: Path) -> Path:
    config_path = path / "connectors.yaml"
    config_path.write_text(
        f"""
        version: 1
        connectors:
          local-docs:
            provider: local
            settings:
              source_path: {source_dir}
          github-missing-secret:
            provider: github
            settings:
              owner: example
              repo: docs
            secrets:
              token_env: TEST_MISSING_GITHUB_TOKEN
        """,
        encoding="utf-8",
    )
    return config_path


def test_a_connector_missing_its_secret_does_not_block_a_working_connector(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("TEST_MISSING_GITHUB_TOKEN", raising=False)
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    config_path = _write_connector_config(tmp_path, source_dir)
    builder = IngestionRuntimeBuilder(
        RuntimeSettings(connector_config_path=config_path),
    )

    connectors, fingerprints, errors = builder._connectors(
        attachment_parser=object(),
        rate_limiter=object(),
    )

    assert set(connectors) == {"local-docs"}
    assert set(fingerprints) == {"local-docs"}
    assert set(errors) == {"github-missing-secret"}
    assert "TEST_MISSING_GITHUB_TOKEN" in str(errors["github-missing-secret"])


def _write_sharepoint_policies(path: Path) -> Path:
    config_path = path / "sharepoint.yaml"
    blocks = "".join(
        f"""
          sharepoint-{policy}:
            provider: sharepoint
            settings:
              site_url: https://contoso.sharepoint.com/sites/Engineering
              restricted_content: {policy}
            secrets:
              access_token_env: TEST_SHAREPOINT_TOKEN
        """
        for policy in ("include", "skip")
    )
    config_path.write_text(f"version: 1\nconnectors:{blocks}", encoding="utf-8")
    return config_path


def _build_sharepoint_policies(tmp_path: Path, monkeypatch, settings: dict[str, str]):
    monkeypatch.setenv("TEST_SHAREPOINT_TOKEN", "synthetic-token")
    builder = IngestionRuntimeBuilder(
        RuntimeSettings(connector_config_path=_write_sharepoint_policies(tmp_path), **settings),
    )
    return builder._connectors(attachment_parser=object(), rate_limiter=object())


def test_restricted_content_include_is_refused_under_tenant_shared(tmp_path: Path, monkeypatch):
    connectors, fingerprints, errors = _build_sharepoint_policies(
        tmp_path,
        monkeypatch,
        {"corpus_access_mode": "tenant_shared", "corpus_shared_tenant_id": "tenant-a"},
    )

    assert set(connectors) == set(fingerprints) == {"sharepoint-skip"}
    assert set(errors) == {"sharepoint-include"}
    message = str(errors["sharepoint-include"])
    assert "'sharepoint-include'" in message
    assert "restricted_content=include" in message
    assert "HARBORRAG_CORPUS_ACCESS_MODE=source_acl" in message


def test_restricted_content_include_is_accepted_under_source_acl(tmp_path: Path, monkeypatch):
    connectors, _fingerprints, errors = _build_sharepoint_policies(
        tmp_path, monkeypatch, {"corpus_access_mode": "source_acl"}
    )

    assert set(connectors) == {"sharepoint-include", "sharepoint-skip"}
    assert errors == {}
