"""Shared project fixtures for the doctor test modules."""

from __future__ import annotations

import shutil
from pathlib import Path

from harborrag_app.cli.doctor import probes

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_CATALOGS = (
    "config/models/models.yaml",
    "config/ingestion/parsers.yaml",
    "config/ingestion/chunking.yaml",
    "config/temporal.yaml",
)
PROJECT_MARKER = """\
execution_mode: direct
discover_plugins: false
runtime:
  env: dev
  connector_config_path: config/sources/connectors.yaml
  parser_config_path: config/ingestion/parsers.yaml
  chunking_config_path: config/ingestion/chunking.yaml
  model_config_path: config/models/models.yaml
  temporal_config_path: config/temporal.yaml
"""
CONNECTORS = {
    "workspace": """\
version: 1
connectors:
  workspace:
    provider: local
    enabled: true
    environment:
      source_path: LOCAL_SOURCE_PATH
""",
    "github": """\
version: 1
connectors:
  github:
    provider: github
    enabled: true
    environment:
      repository_url: GITHUB_REPOSITORY_URL
    secrets:
      token_env: GITHUB_TOKEN
""",
}
GATEWAY_VARIABLES = (
    "HARBORRAG_URL",
    "HARBORRAG_API_KEY",
    "HARBORRAG_CHAT_MODEL",
    "HARBORRAG_EMBED_MODEL",
    "HARBORRAG_RERANKER_MODEL",
)


def project(
    tmp_path: Path, monkeypatch, *, api_key: str | None, connectors: str = "workspace"
) -> None:
    """Lay out a HarborRAG project from the shipped catalogs and run from inside it.

    Args:
        tmp_path: Directory that becomes the project root.
        monkeypatch: Moves into the project and clears variables that would leak in.
        api_key: ``HARBORRAG_API_KEY`` written to the project's ``.env``; ``None``
            leaves it blank.
        connectors: Key of ``CONNECTORS`` written as the connector catalog.
    """
    for relative in SHIPPED_CATALOGS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)
    sources = tmp_path / "config" / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "connectors.yaml").write_text(CONNECTORS[connectors], encoding="utf-8")
    (tmp_path / "harborrag.yaml").write_text(PROJECT_MARKER, encoding="utf-8")
    (tmp_path / ".env").write_text(_environment(api_key), encoding="utf-8")
    (tmp_path / "docs").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    for name in (
        *GATEWAY_VARIABLES,
        "LOCAL_SOURCE_PATH",
        "HARBORRAG_QDRANT_URL",
        "HARBORRAG_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)


def _environment(api_key: str | None) -> str:
    return (
        "HARBORRAG_ENV=dev\n"
        "HARBORRAG_CONTROL_DB_URL=sqlite+aiosqlite:///./.harborrag/control.db\n"
        f"HARBORRAG_SECRETS_ENCRYPTION_KEY={'11' * 32}\n"
        "HARBORRAG_QDRANT_URL=http://localhost:6333\n"
        "HARBORRAG_FALKORDB_HOST=localhost\n"
        "HARBORRAG_FALKORDB_PORT=6379\n"
        "HARBORRAG_OBJECT_STORE_ENDPOINT_URL=http://localhost:9000\n"
        "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID=harborrag\n"
        "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY=test-object-store-secret\n"
        "HARBORRAG_OCR_SERVER_URL=\n"
        "LOCAL_SOURCE_PATH=./docs\n"
        "HARBORRAG_URL=https://gateway.example.com\n"
        f"HARBORRAG_API_KEY={api_key or ''}\n"
        "HARBORRAG_CHAT_MODEL=chat-test\n"
        "HARBORRAG_EMBED_MODEL=embed-test\n"
        "HARBORRAG_RERANKER_MODEL=rerank-test\n"
    )


def unreachable(monkeypatch) -> None:
    monkeypatch.setattr(probes, "tcp_reachable", lambda *a, **k: "connection refused")
    monkeypatch.setattr(probes, "http_ok", lambda *a, **k: "connection refused")
    monkeypatch.setattr(probes, "redis_ping", lambda *a, **k: "connection refused")
    monkeypatch.setattr(probes, "s3_credentials", lambda *a, **k: "unreachable")
