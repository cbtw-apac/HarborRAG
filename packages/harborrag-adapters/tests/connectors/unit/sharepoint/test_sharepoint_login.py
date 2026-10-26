"""The SharePoint login command finds its tenant and client IDs like the CLI does."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.connectors.sharepoint import login

_IDS = ("MICROSOFT_TENANT_ID", "MICROSOFT_CLIENT_ID")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout-like CWD with HOME inside it, so the default token path is predictable."""

    for name in (*_IDS, "MICROSOFT_CLIENT_SECRET", "CONNECTOR_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _token_path(root: Path, tenant: str, client: str) -> Path:
    return root / "home" / ".harborrag" / "sharepoint" / f"token-{tenant}-{client}.json"


def test_ids_are_read_from_the_connector_env_file(workspace: Path, capsys) -> None:
    _write(
        workspace,
        "env/.env.connector",
        "MICROSOFT_TENANT_ID=file-tenant\nMICROSOFT_CLIENT_ID=file-client\n",
    )

    assert login.main(["--logout"]) == 0

    assert str(_token_path(workspace, "file-tenant", "file-client")) in capsys.readouterr().out


def test_exported_ids_and_flags_outrank_the_file(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _write(
        workspace,
        "env/.env.connector",
        "MICROSOFT_TENANT_ID=file-tenant\nMICROSOFT_CLIENT_ID=file-client\n",
    )
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "shell-tenant")

    assert login.main(["--logout", "--client-id", "flag-client"]) == 0

    assert str(_token_path(workspace, "shell-tenant", "flag-client")) in capsys.readouterr().out


def test_connector_env_file_override_is_honoured(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    custom = _write(
        workspace,
        "custom/connectors.env",
        "MICROSOFT_TENANT_ID=moved-tenant\nMICROSOFT_CLIENT_ID=moved-client\n",
    )
    monkeypatch.setenv("CONNECTOR_ENV_FILE", str(custom))

    assert login.main(["--logout"]) == 0

    assert str(_token_path(workspace, "moved-tenant", "moved-client")) in capsys.readouterr().out


def test_only_the_ids_are_taken_from_the_file(workspace: Path) -> None:
    import os

    _write(
        workspace,
        "env/.env.connector",
        "MICROSOFT_TENANT_ID=t\nMICROSOFT_CLIENT_ID=c\nMICROSOFT_CLIENT_SECRET=hunter2\n",
    )

    assert login.main(["--logout"]) == 0

    assert "MICROSOFT_CLIENT_SECRET" not in os.environ
    assert not any(name in os.environ for name in _IDS)


def test_missing_ids_name_the_file_that_was_checked(workspace: Path, capsys) -> None:
    _write(workspace, "env/.env.connector", "# MICROSOFT_TENANT_ID=\n")

    assert login.main([]) == 2

    out = capsys.readouterr().out
    assert "--tenant-id and --client-id" in out
    assert "env/.env.connector" in out
