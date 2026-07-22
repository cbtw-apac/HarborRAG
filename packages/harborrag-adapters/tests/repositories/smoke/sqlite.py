from __future__ import annotations

import asyncio
from tempfile import TemporaryDirectory

from _database import exercise_database
from bootstrap import dependency_available, load_env, safe_error

from harborrag_adapters.repositories.database.sqlite import (
    SQLiteDatabaseBackend,
    SQLiteDatabaseConfig,
)


async def _run() -> str:
    with TemporaryDirectory(prefix="harborrag-sqlite-smoke-") as directory:
        backend = SQLiteDatabaseBackend(
            SQLiteDatabaseConfig(
                instance_name="smoke",
                database=f"{directory}/harborrag.db",
                create_schema=True,
            )
        )
        async with backend:
            context, document_id = await exercise_database(backend, commit=True)
            factory = backend.unit_of_work_factory
            if factory is None:
                raise RuntimeError("SQLite did not provide a unit-of-work factory")
            async with factory() as unit_of_work:
                persisted = await unit_of_work.documents.get(document_id, context=context)
                if persisted is None:
                    raise AssertionError("committed SQLite document was not persisted")
        return str(document_id)


def main() -> int:
    load_env()
    if not dependency_available("aiosqlite", "Install harborrag-adapters normally."):
        return 2
    try:
        document_id = asyncio.run(_run())
    except Exception as exc:
        print(f"SQLite smoke failed: {safe_error(exc)}")
        return 1
    print(f"SQLite smoke passed: committed document_id={document_id!r} in a temporary database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
