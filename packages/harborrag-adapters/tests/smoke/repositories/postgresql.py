from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

from _database import exercise_database
from bootstrap import dependency_available, env, env_int, load_env, safe_error
from harborrag_adapters.repositories.database.postgresql import (
    PostgreSQLDatabaseBackend,
    PostgreSQLDatabaseConfig,
)


def _database_url() -> str:
    configured = os.getenv("HARBOR_SMOKE_POSTGRES_URL")
    if configured and configured.strip():
        return configured.strip()
    username = quote(env("POSTGRES_USER", "postgres"), safe="")
    password = quote(env("POSTGRES_PASSWORD", "harborrag-local-dev"), safe="")
    host = env("POSTGRES_HOST", "127.0.0.1")
    port = env_int("POSTGRES_PORT", 5432)
    database = quote(env("POSTGRES_DB", "harborrag"), safe="")
    return f"postgresql+asyncpg://{username}:{password}@{host}:{port}/{database}"


async def _run() -> str:
    backend = PostgreSQLDatabaseBackend(
        PostgreSQLDatabaseConfig(
            instance_name="smoke",
            url=_database_url(),
            pool_size=2,
            max_overflow=0,
            pool_recycle_seconds=300,
            create_schema=True,
        )
    )
    async with backend:
        _, document_id = await exercise_database(backend, commit=False)
    return str(document_id)


def main() -> int:
    load_env()
    if not dependency_available(
        "asyncpg",
        'Install it with: uv pip install -e "packages/harborrag-adapters[postgres]"',
    ):
        return 2
    try:
        document_id = asyncio.run(_run())
    except Exception as exc:
        print(f"PostgreSQL smoke failed: {safe_error(exc)}")
        return 1
    print(f"PostgreSQL smoke passed: rolled back document_id={document_id!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
