"""doctor output must never carry a credential, a URL secret, or a raw config value.

Split from test_doctor_cli.py to keep both modules inside the file-length gate.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from app_test_fixtures import MockAppService
from doctor_test_support import project, unreachable

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.cli.doctor import probes


def test_probe_errors_never_echo_url_credentials_or_query() -> None:
    """A configured endpoint may embed userinfo or a token; Check.detail must not."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    url = f"http://user:hunter2@127.0.0.1:{free_port}/readyz?token=abc123"

    error = probes.http_ok(url, timeout=0.2)

    assert error is not None
    for secret in ("hunter2", "abc123", "user:"):
        assert secret not in error
    assert f"127.0.0.1:{free_port}/readyz" in error


def test_doctor_never_prints_the_value_of_a_bad_source_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)
    monkeypatch.setenv("LOCAL_SOURCE_PATH", "sk-live-secret-looking-value")

    cli.main(["doctor", "--json"])

    out = capsys.readouterr().out
    checks = {c["name"]: c for c in json.loads(out)["data"]["checks"]}
    assert checks["source path"]["status"] == "fail"
    assert "sk-live-secret-looking-value" not in out


def test_doctor_does_not_echo_a_valid_source_path_either(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The ok branch used to print `VAR=value`; a catalog can name any variable here."""

    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)
    secret_looking = tmp_path / "sk-live-looking-folder"
    secret_looking.mkdir()
    monkeypatch.setenv("LOCAL_SOURCE_PATH", str(secret_looking))

    cli.main(["doctor", "--json"])

    out = capsys.readouterr().out
    assert {c["name"]: c for c in json.loads(out)["data"]["checks"]}["source path"][
        "status"
    ] == "ok"
    assert "sk-live-looking-folder" not in out


def test_model_catalog_errors_do_not_echo_expanded_values(tmp_path: Path, monkeypatch) -> None:
    """models.yaml expands ${VAR} eagerly, so a validation dump can carry the key."""

    from harborrag_app.cli.doctor.environment import models_check

    catalog = tmp_path / "models.yaml"
    catalog.write_text("chat:\n  default_model: [not, a, string]\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-do-not-leak")

    check = models_check(catalog)

    assert check.status == "fail"
    assert "sk-live-do-not-leak" not in check.detail


def test_settings_errors_are_reported_without_input_values() -> None:
    from pydantic import ValidationError

    from harborrag_app.cli.doctor.environment import settings_error_detail
    from harborrag_runtime.config.settings import RuntimeSettings

    with pytest.raises(ValidationError) as caught:
        RuntimeSettings(
            control_db_url="postgresql+asyncpg://u:hunter2@db/x", secrets_encryption_key=None
        )

    detail = settings_error_detail(caught.value)

    assert "HARBORRAG_SECRETS_ENCRYPTION_KEY" in detail
    assert "hunter2" not in detail and "input_value" not in detail


def test_in_house_configuration_messages_survive_sanitization() -> None:
    """The migration-skew message is the operator's whole diagnostic; keep it.

    Collapsing it to a bare class name is the over-correction this pins against: the
    message is authored in-repo and names HARBORRAG_* variables, never their values.
    """

    from harborrag_app.cli.doctor.environment import check_error_detail
    from harborrag_core.contracts.errors import HarborConfigurationError

    detail = check_error_detail(
        HarborConfigurationError("control-plane migrations failed; inspect the startup logs")
    )

    assert detail == (
        "HarborConfigurationError: control-plane migrations failed; inspect the startup logs"
    )


def test_foreign_exception_text_is_still_collapsed() -> None:
    """A composition failure carries HARBORRAG_CONTROL_DB_URL, password included."""

    from harborrag_app.cli.doctor.environment import check_error_detail

    detail = check_error_detail(RuntimeError("postgresql://harbor:hunter2@db/harborrag is down"))

    assert detail == "RuntimeError"
    assert "hunter2" not in detail
