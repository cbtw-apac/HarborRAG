"""The CLI reads the project's connector and model credentials from its env files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harborrag_app.cli.environment import load_project_environment


def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_connector_credentials_load_without_an_inline_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Compose hands these files to the worker; the host CLI needs the same values."""

    _write(tmp_path, "env/.env.connector", "JIRA_BASE_URL=https://example.atlassian.net\n")
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)

    loaded = load_project_environment(tmp_path)

    assert os.environ["JIRA_BASE_URL"] == "https://example.atlassian.net"
    assert [path.name for path in loaded] == [".env.connector"]


def test_an_exported_variable_outranks_the_file(tmp_path: Path, monkeypatch) -> None:
    """Prefixing a variable onto the command is explicit and must win."""

    _write(tmp_path, "env/.env.connector", "JIRA_BASE_URL=https://from-file.example.com\n")
    monkeypatch.setenv("JIRA_BASE_URL", "https://from-shell.example.com")

    load_project_environment(tmp_path)

    assert os.environ["JIRA_BASE_URL"] == "https://from-shell.example.com"


def test_missing_files_are_skipped_rather_than_failing(tmp_path: Path) -> None:
    """A checkout without env files must still run commands that need no credentials."""

    assert load_project_environment(tmp_path) == ()


def test_the_dev_script_override_variables_are_honoured(tmp_path: Path, monkeypatch) -> None:
    """dev.sh lets an operator relocate these files; the CLI must follow the same names."""

    custom = _write(tmp_path, "custom/connectors.env", "JIRA_PROJECT_KEY=RELOCATED\n")
    monkeypatch.setenv("CONNECTOR_ENV_FILE", str(custom))
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)

    loaded = load_project_environment(tmp_path)

    assert os.environ["JIRA_PROJECT_KEY"] == "RELOCATED"
    assert loaded == (custom,)


def test_service_only_files_are_never_loaded(tmp_path: Path, monkeypatch) -> None:
    """.env.api and .env.database point at in-cluster hostnames a host CLI cannot reach."""

    _write(
        tmp_path, "env/.env.api", "HARBORRAG_CONTROL_DB_URL=postgresql+asyncpg://x@postgres/db\n"
    )
    _write(tmp_path, "env/.env.database", "POSTGRES_PASSWORD=secret\n")
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    assert load_project_environment(tmp_path) == ()
    assert "HARBORRAG_CONTROL_DB_URL" not in os.environ
    assert "POSTGRES_PASSWORD" not in os.environ


_ACCESS = "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID"
_SECRET = "HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY"  # noqa: S105 - a variable name


def _clear_object_store(monkeypatch) -> None:
    for name in (_ACCESS, _SECRET, "POSTGRES_PASSWORD", "DATABASE_ENV_FILE"):
        # setenv first so teardown restores "absent" even though the loader sets it.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _mark_checkout(root: Path) -> None:
    """A repository checkout is recognised by its config/connectors.yaml."""

    _write(root, "config/connectors.yaml", "connectors: {}\n")


def _activate_project(monkeypatch) -> None:
    from harborrag_app.cli import project as project_module

    monkeypatch.setattr(project_module, "active_project", lambda: object())


def test_checkout_maps_minio_credentials_only(tmp_path: Path, monkeypatch) -> None:
    _write(
        tmp_path,
        "env/.env.database",
        "MINIO_ROOT_USER=u\nMINIO_ROOT_PASSWORD=p\nPOSTGRES_PASSWORD=x\n",
    )
    _mark_checkout(tmp_path)
    _clear_object_store(monkeypatch)

    loaded = load_project_environment(tmp_path)

    assert os.environ[_ACCESS] == "u"
    assert os.environ[_SECRET] == "p"
    assert "POSTGRES_PASSWORD" not in os.environ
    assert [path.name for path in loaded] == [".env.database"]


def test_project_object_store_values_win(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, "env/.env.database", "MINIO_ROOT_USER=u\nMINIO_ROOT_PASSWORD=p\n")
    _mark_checkout(tmp_path)
    _clear_object_store(monkeypatch)
    monkeypatch.setenv(_ACCESS, "proj")

    load_project_environment(tmp_path)

    assert os.environ[_ACCESS] == "proj"


def test_database_env_file_override_is_honoured(tmp_path: Path, monkeypatch) -> None:
    custom = _write(tmp_path, "elsewhere/db.env", "MINIO_ROOT_USER=alt\nMINIO_ROOT_PASSWORD=altp\n")
    _clear_object_store(monkeypatch)
    monkeypatch.setenv("DATABASE_ENV_FILE", str(custom))

    load_project_environment(tmp_path)

    assert os.environ[_ACCESS] == "alt"
    assert os.environ[_SECRET] == "altp"


def test_partial_minio_credentials_are_ignored(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, "env/.env.database", "MINIO_ROOT_USER=u\n")
    _mark_checkout(tmp_path)
    _clear_object_store(monkeypatch)

    assert load_project_environment(tmp_path) == ()
    assert _ACCESS not in os.environ


def test_unreadable_database_env_file_is_skipped(tmp_path: Path, monkeypatch) -> None:
    from harborrag_app.cli import environment

    _write(tmp_path, "env/.env.database", "MINIO_ROOT_USER=u\nMINIO_ROOT_PASSWORD=p\n")
    _mark_checkout(tmp_path)
    _clear_object_store(monkeypatch)

    def denied(path: Path) -> dict[str, str]:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(environment, "checkout_object_store_credentials", denied)

    assert load_project_environment(tmp_path) == ()
    assert _ACCESS not in os.environ


def test_minio_credentials_map_only_in_a_repo_checkout(tmp_path: Path, monkeypatch) -> None:
    # A plain directory or a `harborrag init` project with an env/.env.database of its
    # own is not the dev stack; its MinIO pair must not become the CLI's credentials.
    _write(tmp_path, "env/.env.database", "MINIO_ROOT_USER=u\nMINIO_ROOT_PASSWORD=p\n")
    _clear_object_store(monkeypatch)

    assert load_project_environment(tmp_path) == ()
    assert _ACCESS not in os.environ

    _mark_checkout(tmp_path)
    _activate_project(monkeypatch)

    assert load_project_environment(tmp_path) == ()
    assert _ACCESS not in os.environ


def test_explicit_database_env_file_maps_in_project_mode(tmp_path: Path, monkeypatch) -> None:
    custom = _write(tmp_path, "elsewhere/db.env", "MINIO_ROOT_USER=alt\nMINIO_ROOT_PASSWORD=altp\n")
    _clear_object_store(monkeypatch)
    _activate_project(monkeypatch)
    monkeypatch.setenv("DATABASE_ENV_FILE", str(custom))

    loaded = load_project_environment(tmp_path)

    assert os.environ[_ACCESS] == "alt"
    assert loaded == (custom,)


def test_applied_minio_credentials_are_logged_by_path_only(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    _mark_checkout(tmp_path)
    _write(tmp_path, "env/.env.database", "MINIO_ROOT_USER=u\nMINIO_ROOT_PASSWORD=hunter2\n")
    _clear_object_store(monkeypatch)

    with caplog.at_level("INFO", logger="harborrag.app.cli.environment"):
        load_project_environment(tmp_path)

    info = [record for record in caplog.records if record.levelname == "INFO"]
    assert len(info) == 1
    assert ".env.database" in info[0].getMessage()
    assert "hunter2" not in caplog.text


def _checkout_cwd(tmp_path: Path, monkeypatch) -> None:
    _mark_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "sqlite+aiosqlite:///./harborrag_control.db",
        "sqlite+aiosqlite:///harborrag_control.db",
        "sqlite+aiosqlite:///./sub/../harborrag_control.db",
        "sqlite:///harborrag_control.db",
    ],
)
def test_checkout_refuses_every_spelling_of_the_default_sqlite_file(
    url: str, tmp_path: Path, monkeypatch
) -> None:
    from harborrag_app.cli.environment import checkout_write_refusal

    _checkout_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", url)

    assert checkout_write_refusal() is not None


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./other_control.db",
        "sqlite+aiosqlite:///./sub/harborrag_control.db",
        "postgresql+asyncpg://u@db.example/harbor",
    ],
)
def test_checkout_allows_another_control_db(url: str, tmp_path: Path, monkeypatch) -> None:
    from harborrag_app.cli.environment import checkout_write_refusal

    _checkout_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", url)

    assert checkout_write_refusal() is None


def test_checkout_refuses_an_absolute_path_to_the_default_file(tmp_path: Path, monkeypatch) -> None:
    from harborrag_app.cli.environment import checkout_write_refusal

    _checkout_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "HARBORRAG_CONTROL_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'harborrag_control.db'}"
    )

    assert checkout_write_refusal() is not None
