"""Secret-safe FalkorDB connection for the graph evaluation smoke suite."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient

ROOT = Path(__file__).resolve().parents[5]


def build_client(graph_name: str = "harborrag") -> FalkorDBClient:
    """Load the ignored database env file and connect like the runtime would."""

    load_dotenv(ROOT / "env/.env.database", override=False)
    return FalkorDBClient(
        host=os.getenv("FALKORDB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(os.getenv("FALKORDB_PORT", "6379").strip() or "6379"),
        username=os.getenv("FALKORDB_USERNAME", "").strip() or None,
        password=os.getenv("FALKORDB_PASSWORD", "").strip() or None,
        graph_name=graph_name,
        ssl=False,
        max_connections=4,
        connect_timeout_seconds=5.0,
        operation_timeout_seconds=30.0,
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
