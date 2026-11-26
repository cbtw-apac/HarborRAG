"""The MCP server's read-only Postgres role and MinIO user, and the step that applies them."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
ROLE_SQL = "deploy/compose/postgres/mcp-reader-role.sql"
POLICY = "deploy/compose/minio/mcp-reader-policy.json"
MINIO_SCRIPT = "scripts/deployment/common/mcp-minio-user.sh"
MCP_READS_CONTROL_PLANE = frozenset({"mcp_api_keys", "setting_overrides", "setting_revisions"})
PROJECT_FILES = (
    "scripts/deployment/dev.sh",
    "scripts/deployment/common/dev-checks.sh",
    MINIO_SCRIPT,
    ROLE_SQL,
    POLICY,
)
FAKE_DOCKER = """#!/usr/bin/env sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case " $* " in
  *" exec "*" postgres "*)
    cat > "$DOCKER_STDIN.postgres"
    printf '%s' "${POSTGRES_MCP_PASSWORD:-}" > "$DOCKER_STDIN.postgres-password" ;;
  *" exec "*" minio "*)
    cat > "$DOCKER_STDIN.minio"
    printf '%s' "${MINIO_MCP_PASSWORD:-}" > "$DOCKER_STDIN.minio-password" ;;
esac
"""


def _project(tmp_path: Path, database: str) -> tuple[Path, dict[str, str]]:
    for relative in PROJECT_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    (tmp_path / "env").mkdir()
    (tmp_path / "env/.env.database").write_text(database, encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (fake_bin / "docker").chmod(0o755)
    env = {
        **os.environ,
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "DOCKER_STDIN": str(tmp_path / "stdin"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return tmp_path / "scripts/deployment/dev.sh", env


def _mcp_access(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "mcp-access"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )


def _value(path: Path, name: str) -> str:
    match = re.search(rf"^{name}=(.*)$", path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_mcp_access_generates_passwords_and_hands_them_over_outside_argv(tmp_path: Path) -> None:
    script, env = _project(tmp_path, "POSTGRES_DB=harborrag\nPOSTGRES_MCP_PASSWORD=\n")
    database = tmp_path / "env/.env.database"

    result = _mcp_access(script, env)

    assert result.returncode == 0, result.stderr
    postgres_password = _value(database, "POSTGRES_MCP_PASSWORD")
    minio_password = _value(database, "MINIO_MCP_PASSWORD")
    assert re.fullmatch(r"[0-9a-f]{64}", postgres_password)
    assert re.fullmatch(r"[0-9a-f]{64}", minio_password)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "exec -T -e POSTGRES_MCP_PASSWORD postgres" in calls
    assert "exec -T -e MINIO_MCP_PASSWORD minio bash -c" in calls
    assert postgres_password not in calls
    assert minio_password not in calls
    assert (tmp_path / "stdin.postgres-password").read_text() == postgres_password
    assert (tmp_path / "stdin.minio-password").read_text() == minio_password
    assert (tmp_path / "stdin.postgres").read_bytes() == (ROOT / ROLE_SQL).read_bytes()
    assert (tmp_path / "stdin.minio").read_bytes() == (ROOT / POLICY).read_bytes()


def test_mcp_access_keeps_passwords_an_operator_set(tmp_path: Path) -> None:
    script, env = _project(
        tmp_path,
        'POSTGRES_DB=harborrag\nPOSTGRES_MCP_PASSWORD="operator-postgres"\n'
        "MINIO_MCP_PASSWORD=operator-minio\r\n",
    )

    first, second = _mcp_access(script, env), _mcp_access(script, env)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert "Generated" not in first.stdout + second.stdout
    assert (tmp_path / "stdin.postgres-password").read_text() == "operator-postgres"
    assert (tmp_path / "stdin.minio-password").read_text() == "operator-minio"


def test_bootstrap_writes_the_passwords_the_mcp_compose_file_requires(tmp_path: Path) -> None:
    script, env = _project(tmp_path, "")
    (tmp_path / "env/.env.database").unlink()
    for template in (ROOT / "env-example").glob(".env.*.example"):
        target = tmp_path / "env-example" / template.name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(template.read_bytes())
    (tmp_path / "fake-bin/docker").write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")
    env = {k: v for k, v in env.items() if k != "HARBORRAG_SECRETS_ENCRYPTION_KEY"}
    database = tmp_path / "env/.env.database"

    def bootstrap() -> tuple[str, str]:
        result = subprocess.run(
            ["bash", str(script), "bootstrap"],
            env=env,
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return _value(database, "POSTGRES_MCP_PASSWORD"), _value(database, "MINIO_MCP_PASSWORD")

    first = bootstrap()

    assert all(re.fullmatch(r"[0-9a-f]{64}", password) for password in first)
    assert bootstrap() == first
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_role_script_grants_reads_and_revokes_the_control_plane_tables() -> None:
    script = (ROOT / ROLE_SQL).read_text(encoding="utf-8")
    statements = script.upper()

    assert "GRANT SELECT ON ALL TABLES IN SCHEMA PUBLIC TO HARBORRAG_MCP" in statements
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC GRANT SELECT ON TABLES" in statements
    assert "SET DEFAULT_TRANSACTION_READ_ONLY = ON" in statements
    assert "REVOKE CREATE ON SCHEMA PUBLIC FROM PUBLIC" in statements
    for write in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALL PRIVILEGES"):
        assert f"GRANT {write}" not in statements
    revoked = set(re.findall(r"^\s+'([a-z_]+)',?$", script, re.MULTILINE))
    assert {"secrets", "sources", "memories", "conversation_messages", "members"} <= revoked
    assert revoked.isdisjoint(MCP_READS_CONTROL_PLANE)


def test_role_script_revokes_every_control_plane_table_the_server_does_not_read() -> None:
    from harborrag_adapters.repositories.database.control_plane.schemas import Base

    script = (ROOT / ROLE_SQL).read_text(encoding="utf-8")
    revoked = set(re.findall(r"^\s+'([a-z_]+)',?$", script, re.MULTILINE))

    unread = set(Base.metadata.tables) - MCP_READS_CONTROL_PLANE
    assert unread | {"alembic_version"} == revoked


def test_minio_policy_reads_the_artifacts_bucket_only() -> None:
    policy = json.loads((ROOT / POLICY).read_text(encoding="utf-8"))

    granted = {
        (action, resource)
        for statement in policy["Statement"]
        if statement["Effect"] == "Allow"
        for action in statement["Action"]
        for resource in statement["Resource"]
    }
    assert granted == {
        ("s3:ListBucket", "arn:aws:s3:::harborrag-artifacts"),
        ("s3:GetObject", "arn:aws:s3:::harborrag-artifacts/*"),
    }


def test_fresh_database_volume_runs_the_role_script() -> None:
    database = yaml.safe_load(
        (ROOT / "deploy/compose/docker-compose.database.yml").read_text(encoding="utf-8")
    )

    assert (
        "./postgres/mcp-reader-role.sql:/docker-entrypoint-initdb.d/10-mcp-reader-role.sql:ro"
        in database["services"]["postgres"]["volumes"]
    )
