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
