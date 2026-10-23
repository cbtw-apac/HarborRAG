"""Shared project scaffolding for the CLI project test modules."""

from __future__ import annotations

from pathlib import Path


def scaffold(root: Path) -> Path:
    """A minimal project: a marker with runtime defaults, plus a .env to load."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "harborrag.yaml").write_text(
        "execution_mode: direct\nruntime:\n  env: dev\n"
        "  connector_config_path: config/connectors.yaml\n"
        "  qdrant_prefer_grpc: false\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("OPENAI_API_KEY=from-dotenv\nHARBORRAG_ENV=prod\n")
    return root


__all__ = ["scaffold"]
