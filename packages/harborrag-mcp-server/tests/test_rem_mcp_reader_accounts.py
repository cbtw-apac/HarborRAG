from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_mcp_server.configuration.launch import (
    READER_DATABASE_ROLE,
    READER_OBJECT_STORE_USER,
    load_launch_environment,
)
from harborrag_mcp_server.preflight import describe_startup_failure

pytestmark = pytest.mark.unit

REPOSITORY = Path(__file__).resolve().parents[3]
OWNER_SECRETS = ("owner-secret", "object-root-secret")


def _checkout(root: Path, database: str) -> None:
    (root / "env").mkdir()
    (root / "env/.env.database").write_text(database, encoding="utf-8")
    (root / "env/.env.models").write_text("", encoding="utf-8")


def _owner_and_reader_values() -> str:
    return (
        "POSTGRES_USER=owner\nPOSTGRES_PASSWORD=owner-secret\nPOSTGRES_DB=harbor\n"
        "POSTGRES_MCP_PASSWORD=reader-secret\n"
        "MINIO_ROOT_USER=object-root\nMINIO_ROOT_PASSWORD=object-root-secret\n"
        "MINIO_MCP_PASSWORD=object-reader-secret\n"
    )


def test_checkout_server_connects_with_the_read_only_accounts_only(tmp_path: Path) -> None:
    _checkout(
        tmp_path,
        _owner_and_reader_values()
        + 'HARBORRAG_CONTROL_DB_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}'
        '@127.0.0.1:5432/${POSTGRES_DB}"\n',
    )
    environment: dict[str, str] = {}

    load_launch_environment(checkout_root=tmp_path, environ=environment)

    assert environment["HARBORRAG_CONTROL_DB_URL"] == (
        f"postgresql+asyncpg://{READER_DATABASE_ROLE}:reader-secret@localhost:5432/harbor"
    )
    assert environment["HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"] == READER_OBJECT_STORE_USER
    assert environment["HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY"] == "object-reader-secret"
    connection_settings = (
        environment["HARBORRAG_CONTROL_DB_URL"],
        environment["HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"],
        environment["HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY"],
    )
    for secret in OWNER_SECRETS:
        assert all(secret not in value for value in connection_settings)


@pytest.mark.parametrize(
    ("missing", "message"),
    (
        ("POSTGRES_MCP_PASSWORD=reader-secret\n", "database configuration is missing"),
        ("MINIO_MCP_PASSWORD=object-reader-secret\n", "object store configuration is missing"),
    ),
)
def test_checkout_server_refuses_to_fall_back_to_owner_accounts(
    tmp_path: Path, missing: str, message: str
) -> None:
    _checkout(tmp_path, _owner_and_reader_values().replace(missing, ""))

    with pytest.raises(ValueError, match=message) as error:
        load_launch_environment(checkout_root=tmp_path, environ={})

    assert "mcp-access" in str(error.value)
    assert all(secret not in str(error.value) for secret in OWNER_SECRETS)


def test_catalog_check_needs_no_read_only_accounts(tmp_path: Path) -> None:
    _checkout(tmp_path, "POSTGRES_USER=owner\nPOSTGRES_PASSWORD=owner-secret\n")
    environment: dict[str, str] = {}

    load_launch_environment(checkout_root=tmp_path, check=True, environ=environment)

    assert "HARBORRAG_CONTROL_DB_URL" not in environment
    assert "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY" not in environment


def test_compose_server_gets_the_same_read_only_accounts_as_the_checkout() -> None:
    compose = (REPOSITORY / "deploy/compose/docker-compose.mcp.yml").read_text(encoding="utf-8")
    role_script = (REPOSITORY / "deploy/compose/postgres/mcp-reader-role.sql").read_text(
        encoding="utf-8"
    )
    minio_script = (REPOSITORY / "scripts/deployment/common/mcp-minio-user.sh").read_text(
        encoding="utf-8"
    )

    assert f"postgresql+asyncpg://{READER_DATABASE_ROLE}:${{POSTGRES_MCP_PASSWORD:?" in compose
    assert f"HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID: {READER_OBJECT_STORE_USER}\n" in compose
    assert "${MINIO_MCP_PASSWORD:?" in compose
    for owner_value in (
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
    ):
        assert owner_value not in compose
    assert f"CREATE ROLE {READER_DATABASE_ROLE} NOLOGIN" in role_script
    assert f"printf '%s\\n%s\\n' {READER_OBJECT_STORE_USER} " in minio_script


class InvalidPasswordError(Exception):
    """Stands in for the driver error a refused database login raises."""


def test_startup_failure_names_a_refused_login_without_its_message() -> None:
    try:
        try:
            raise InvalidPasswordError('password authentication failed for user "harborrag_mcp"')
        except InvalidPasswordError as refused:
            raise HarborUnavailableError("settings store is unreachable") from refused
    except HarborUnavailableError as error:
        description = describe_startup_failure(error)

    assert description == (
        "HarborUnavailableError: settings store is unreachable (cause: InvalidPasswordError)"
    )


def test_startup_failure_survives_a_cause_chain_that_loops() -> None:
    first, second = RuntimeError("first"), ValueError("second")
    first.__cause__, second.__cause__ = second, first

    assert describe_startup_failure(first) == "RuntimeError: first (cause: ValueError)"
