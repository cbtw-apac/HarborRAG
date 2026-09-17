"""Windows-native stdio launcher for the HarborRAG MCP server.

Mirrors scripts/deployment/mcp.sh (env loading, derived connection settings)
without spawning bash.exe, so MCP clients on Windows can invoke this script's
own Python interpreter directly and avoid MSYS stdio pipe issues that appear
as an immediate post-handshake disconnect.

Usage from an MCP client config: point "command" at the project's
.venv\\Scripts\\python.exe and "args" at this file's absolute path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parents[2]


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def require_file(relative_path: str, label: str) -> Path:
    path = ROOT_DIR / relative_path
    if not path.is_file():
        fail(f"Missing {label}: {relative_path}. Run 'scripts/deployment/dev.sh bootstrap'.")
    return path


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main() -> int:
    database_env = require_file("env/.env.database", "database environment")
    model_env = require_file("env/.env.models", "model environment")
    api_env = require_file("env/.env.api", "API environment")
    mcp_env = ROOT_DIR / "env/.env.mcp"

    load_env_file(database_env)
    load_env_file(model_env)
    load_env_file(api_env)
    if mcp_env.is_file():
        load_env_file(mcp_env)

    postgres_user = os.environ.get("POSTGRES_USER")
    postgres_password = os.environ.get("POSTGRES_PASSWORD")
    postgres_db = os.environ.get("POSTGRES_DB")
    if not (postgres_user and postgres_password and postgres_db):
        fail("Database environment must define POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB.")

    postgres_port = os.environ.get("POSTGRES_PORT", "5432")
    os.environ.setdefault(
        "HARBORRAG_CONTROL_DB_URL",
        "postgresql+asyncpg://{user}:{password}@localhost:{port}/{database}".format(
            user=quote(postgres_user, safe=""),
            password=quote(postgres_password, safe=""),
            port=postgres_port,
            database=quote(postgres_db, safe=""),
        ),
    )
    os.environ.setdefault(
        "HARBORRAG_OBJECT_STORE_ENDPOINT_URL",
        f"http://localhost:{os.environ.get('MINIO_API_PORT', '9000')}",
    )
    os.environ.setdefault(
        "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID", os.environ.get("MINIO_ROOT_USER", "")
    )
    os.environ.setdefault(
        "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY", os.environ.get("MINIO_ROOT_PASSWORD", "")
    )
    os.environ.setdefault(
        "HARBORRAG_QDRANT_URL", f"http://localhost:{os.environ.get('QDRANT_HTTP_PORT', '6333')}"
    )
    os.environ.setdefault("HARBORRAG_FALKORDB_HOST", "localhost")
    os.environ.setdefault("HARBORRAG_FALKORDB_PORT", os.environ.get("FALKORDB_PORT", "6379"))
    model_config_path = os.environ.setdefault(
        "HARBORRAG_MODEL_CONFIG_PATH", str(ROOT_DIR / "config/models.yaml")
    )
    if not Path(model_config_path).is_file():
        fail(f"Model configuration does not exist: {model_config_path}")

    os.chdir(ROOT_DIR)

    from harborrag_mcp_server.__main__ import main as mcp_main

    return mcp_main([])


if __name__ == "__main__":
    raise SystemExit(main())
