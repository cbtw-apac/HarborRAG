"""doctor reports layered readiness and keeps the JSON envelope stable."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.cli.doctor import probes
from harborrag_app.cli.doctor.checks import Check, DoctorReport


def test_report_is_ok_only_when_required_checks_pass() -> None:
    report = DoctorReport(
        (
            Check("a", "config", "ok", "fine"),
            Check("b", "services", "warn", "slow", required=False),
            Check("c", "durable", "skip", "not requested", required=False),
        )
    )
    assert report.ok is True
    assert report.as_payload()["summary"] == {"ok": 1, "fail": 0, "warn": 1, "skip": 1}

    failed = DoctorReport((Check("a", "config", "fail", "missing"),))
    assert failed.ok is False


def test_probes_report_unreachable_ports() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert probes.tcp_reachable("127.0.0.1", free_port, timeout=0.2) is not None
    assert probes.redis_ping("127.0.0.1", free_port, timeout=0.2) is not None
    assert probes.http_ok(f"http://127.0.0.1:{free_port}/readyz", timeout=0.2) is not None


def _project(tmp_path: Path, monkeypatch, capsys, *, api_key: str | None) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--api-key", api_key or ""]) == 0
    capsys.readouterr()  # drop init's output so the doctor payload is all that remains
    (tmp_path / "docs").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    for name in (
        "OPENAI_API_KEY",
        "LOCAL_SOURCE_PATH",
        "HARBORRAG_QDRANT_URL",
        "HARBORRAG_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)


def _unreachable(monkeypatch) -> None:
    monkeypatch.setattr(probes, "tcp_reachable", lambda *a, **k: "connection refused")
    monkeypatch.setattr(probes, "http_ok", lambda *a, **k: "connection refused")
    monkeypatch.setattr(probes, "redis_ping", lambda *a, **k: "connection refused")


def test_doctor_json_lists_checks_and_fails_on_missing_services(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    code = cli.main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    names = {check["name"]: check for check in payload["data"]["checks"]}
    assert names["project"]["status"] == "ok"
    assert names["models catalog"]["status"] == "ok"
    assert names["source path"]["status"] == "ok"
    assert names["qdrant"]["status"] == "fail"
    assert names["temporal"]["status"] == "skip"
    assert payload["data"]["diagnostics"]["runtime"]["ready"] is True


def test_doctor_flags_a_blank_provider_key(tmp_path: Path, monkeypatch, capsys) -> None:
    _project(tmp_path, monkeypatch, capsys, api_key=None)
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["models catalog"]["status"] == "fail"
    assert "OPENAI_API_KEY" in checks["models catalog"]["detail"]


def test_doctor_temporal_flag_runs_the_runtime_health_check(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json", "--temporal"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["temporal"]["status"] == "ok"


def test_doctor_without_a_project_still_runs_legacy_checks(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["project"]["status"] == "warn"
    assert checks["connectors catalog"]["status"] == "fail"


def test_doctor_human_output_lists_every_check(tmp_path: Path, monkeypatch, capsys) -> None:
    _project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["--no-color", "doctor"])

    out = capsys.readouterr().out
    assert "qdrant" in out and "docker compose up -d" in out


def test_doctor_reports_missing_optional_clients_with_the_extra_to_install(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import importlib.util

    from harborrag_app.cli.doctor import environment as suite

    _project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        suite.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "qdrant_client" else real_find_spec(name, *a),
    )

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["python packages"]["status"] == "fail"
    assert "qdrant-client" in checks["python packages"]["detail"]
    assert 'pip install "harborrag[local]"' in checks["python packages"]["hint"]


def test_doctor_reports_installed_clients_as_ok(tmp_path: Path, monkeypatch, capsys) -> None:
    _project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["python packages"]["status"] == "ok"


def test_doctor_flags_blank_connector_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    assert (
        cli.main(["init", str(tmp_path), "--yes", "--api-key", "sk-test", "--connectors", "github"])
        == 0
    )
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY_URL", "HARBORRAG_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["connector credentials"]["status"] == "fail"
    assert "GITHUB_TOKEN" in checks["connector credentials"]["detail"]
    assert "source path" not in checks


def test_doctor_never_prints_the_value_of_a_bad_source_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)
    monkeypatch.setenv("LOCAL_SOURCE_PATH", "sk-live-secret-looking-value")

    cli.main(["doctor", "--json"])

    out = capsys.readouterr().out
    checks = {c["name"]: c for c in json.loads(out)["data"]["checks"]}
    assert checks["source path"]["status"] == "fail"
    assert "sk-live-secret-looking-value" not in out


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


def test_doctor_builds_the_application_service_once(tmp_path: Path, monkeypatch, capsys) -> None:
    _project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _unreachable(monkeypatch)
    calls: list[int] = []

    def factory() -> MockAppService:
        calls.append(1)
        return MockAppService()

    monkeypatch.setattr(cli_runner, "runtime_app_service", factory)

    cli.main(["doctor", "--json"])

    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["data"]["diagnostics"]["runtime"]["ready"] is True


def test_doctor_reports_the_project_main_activated_even_when_it_is_world_writable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`--project` is the opt-in; doctor must not re-run discovery and trip the safety rule."""

    assert cli.main(["init", str(tmp_path), "--yes", "--api-key", "sk-test"]) == 0
    capsys.readouterr()
    tmp_path.chmod(0o777)
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    _unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["--project", str(tmp_path), "doctor", "--json"])

    out = capsys.readouterr().out
    checks = {c["name"]: c for c in json.loads(out)["data"]["checks"]}
    assert checks["project"]["status"] == "ok"


def test_doctor_inside_an_unsafe_directory_fails_cleanly(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--api-key", "sk-test"]) == 0
    capsys.readouterr()
    tmp_path.chmod(0o777)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)

    assert cli.main(["doctor", "--json"]) == 1

    captured = capsys.readouterr()
    assert "world-writable" in captured.err and "Traceback" not in captured.err
