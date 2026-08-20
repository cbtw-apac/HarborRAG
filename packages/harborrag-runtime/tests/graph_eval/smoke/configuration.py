"""Secret-safe FalkorDB connection for the graph evaluation smoke suite."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient
from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig

ROOT = Path(__file__).resolve().parents[5]


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def build_config(graph_name: str = "harborrag") -> FalkorDBGraphConfig:
    """Load the ignored database env file into a transport-validated config.

    ``FalkorDBGraphConfig.validate_transport`` rejects a password over plaintext and
    insecure non-loopback hosts, exactly as the runtime would.
    """

    load_dotenv(ROOT / "env/.env.database", override=False)
    return FalkorDBGraphConfig(
        host=os.getenv("FALKORDB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(os.getenv("FALKORDB_PORT", "6379").strip() or "6379"),
        username=os.getenv("FALKORDB_USERNAME", "").strip() or None,
        password=os.getenv("FALKORDB_PASSWORD", "").strip() or None,
        graph_name=graph_name,
        ssl=_env_flag("FALKORDB_SSL"),
        allow_insecure_remote=_env_flag("FALKORDB_ALLOW_INSECURE_REMOTE"),
        max_connections=4,
        connect_timeout_seconds=5.0,
        operation_timeout_seconds=30.0,
    )


def build_client(graph_name: str = "harborrag") -> FalkorDBClient:
    """Connect like the runtime would (see ``FalkorDBGraphRepository``)."""

    config = build_config(graph_name)
    return FalkorDBClient(
        host=config.host,
        port=config.port,
        username=config.username,
        password=config.password.get_secret_value() if config.password else None,
        graph_name=config.graph_name,
        ssl=config.ssl,
        max_connections=config.max_connections,
        connect_timeout_seconds=config.connect_timeout_seconds,
        operation_timeout_seconds=config.operation_timeout_seconds,
    )


def postgres_url() -> str:
    """Load the ignored database env file and build the control-plane URL."""

    load_dotenv(ROOT / "env/.env.database", override=False)
    parts = {}
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        value = os.getenv(name, "").strip()
        if not value:
            raise RuntimeError(f"database environment variable is missing: {name}")
        parts[name] = quote(value, safe="")
    port = os.getenv("POSTGRES_PORT", "5432").strip() or "5432"
    return (
        f"postgresql+asyncpg://{parts['POSTGRES_USER']}:{parts['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{port}/{parts['POSTGRES_DB']}"
    )
