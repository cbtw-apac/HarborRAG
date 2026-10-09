"""doctor reports layered readiness and keeps the JSON envelope stable."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app_test_fixtures import MockAppService
from doctor_test_support import project, unreachable

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


def test_doctor_json_lists_checks_and_fails_on_missing_services(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
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
    project(tmp_path, monkeypatch, capsys, api_key=None)
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["models catalog"]["status"] == "fail"
    assert "OPENAI_API_KEY" in checks["models catalog"]["detail"]


def test_doctor_temporal_flag_runs_the_runtime_health_check(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json", "--temporal"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["temporal"]["status"] == "ok"


def test_doctor_without_a_project_still_runs_legacy_checks(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["project"]["status"] == "warn"
    assert checks["connectors catalog"]["status"] == "fail"


def test_doctor_human_output_lists_every_check(tmp_path: Path, monkeypatch, capsys) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["--no-color", "doctor"])

    out = capsys.readouterr().out
    assert "qdrant" in out and "docker compose up -d" in out


def test_doctor_reports_missing_optional_clients_with_the_extra_to_install(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import importlib.util

    from harborrag_app.cli.doctor import environment as suite

    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
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
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
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
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["connector credentials"]["status"] == "fail"
    assert "GITHUB_TOKEN" in checks["connector credentials"]["detail"]
    assert "source path" not in checks


def test_report_separates_readiness_from_the_raw_fail_count() -> None:
    """`ready` is the exit-code decision; `summary.fail` counts optional failures too."""

    report = DoctorReport(
        (
            Check("a", "config", "ok", "fine"),
            Check("temporal", "durable", "fail", "unreachable", required=False),
        )
    )

    payload = report.as_payload()
    assert report.ok is True
    assert payload["ready"] is True
    assert payload["summary"]["fail"] == 1


def test_doctor_converts_an_escaping_exception_into_the_envelope(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`service.health()` is called outside any except block inside run_doctor.

    A factory failure is already reported as a check; a raising `health()` is not, so
    without the boundary in the command it reaches the interpreter as a traceback and
    `--json` consumers get no envelope at all.
    """

    class UnreportableHealth(MockAppService):
        def health(self):
            raise RuntimeError("postgresql://harbor:hunter2@db:5432/harborrag is unreachable")

    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", UnreportableHealth)

    assert cli.main(["doctor", "--json"]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["data"]["error_type"] == "RuntimeError"
    assert "hunter2" not in captured.out
    assert "Traceback" not in captured.err


def test_requested_temporal_failure_blocks_readiness(tmp_path: Path, monkeypatch, capsys) -> None:
    """`--temporal` is an explicit request, so its failure has to fail the report."""

    class UnhealthyTemporal(MockAppService):
        async def runtime_health(self):
            from harborrag_app.workflow_control import AppResponse

            return AppResponse(False, {}, "temporal is unreachable")

    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", UnhealthyTemporal)

    assert cli.main(["doctor", "--json", "--temporal"]) == 1

    payload = json.loads(capsys.readouterr().out)["data"]
    temporal = {c["name"]: c for c in payload["checks"]}["temporal"]
    assert temporal["status"] == "fail"
    assert temporal["required"] is True
    assert payload["ready"] is False


def test_unrequested_temporal_stays_non_blocking(tmp_path: Path, monkeypatch, capsys) -> None:
    """Without `--temporal` the skipped check must never decide the exit code."""

    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)
    monkeypatch.setattr(probes, "tcp_reachable", lambda *a, **k: None)
    monkeypatch.setattr(probes, "http_ok", lambda *a, **k: None)
    monkeypatch.setattr(probes, "redis_ping", lambda *a, **k: None)
    monkeypatch.setattr(probes, "qdrant_authorized", lambda *a, **k: None)
    monkeypatch.setenv("LOCAL_SOURCE_PATH", str(tmp_path / "docs"))

    assert cli.main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert {c["name"]: c for c in payload["checks"]}["temporal"]["required"] is False
    assert payload["ready"] is True


def test_doctor_builds_the_application_service_once(tmp_path: Path, monkeypatch, capsys) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    unreachable(monkeypatch)
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
    unreachable(monkeypatch)
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


class _QdrantStub(BaseHTTPRequestHandler):
    expected_key = "synthetic-qdrant-key"

    def do_GET(self) -> None:  # noqa: N802
        authorised = self.headers.get("api-key") == self.expected_key
        ready = self.path == "/readyz"
        self.send_response(200 if ready or authorised else 401)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


def _serve_qdrant_stub():
    server = HTTPServer(("127.0.0.1", 0), _QdrantStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_qdrant_probe_accepts_the_right_key_and_never_echoes_a_wrong_one() -> None:
    server, url = _serve_qdrant_stub()
    try:
        assert probes.qdrant_authorized(url, _QdrantStub.expected_key) is None
        wrong = probes.qdrant_authorized(url, "synthetic-wrong-key")
        missing = probes.qdrant_authorized(url, None)
    finally:
        server.shutdown()
    assert wrong is not None and "rejected the API key" in wrong
    assert "synthetic-wrong-key" not in wrong
    assert missing is not None
    assert "HARBORRAG_QDRANT_API_KEY" in missing and "none is configured" in missing


def test_doctor_fails_qdrant_when_the_server_requires_a_key_that_is_not_configured(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    server, url = _serve_qdrant_stub()
    try:
        monkeypatch.setenv("HARBORRAG_QDRANT_URL", url)
        monkeypatch.delenv("HARBORRAG_QDRANT_API_KEY", raising=False)
        monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)
        cli.main(["doctor", "--json"])
        missing = json.loads(capsys.readouterr().out)["data"]["checks"]
        monkeypatch.setenv("HARBORRAG_QDRANT_API_KEY", "synthetic-wrong-key")
        cli.main(["doctor", "--json"])
        raw = capsys.readouterr().out
    finally:
        server.shutdown()
    qdrant = {c["name"]: c for c in missing}["qdrant"]
    assert qdrant["status"] == "fail"
    assert "HARBORRAG_QDRANT_API_KEY" in qdrant["detail"]
    rejected = {c["name"]: c for c in json.loads(raw)["data"]["checks"]}["qdrant"]
    assert "rejected the API key" in rejected["detail"]
    assert "synthetic-wrong-key" not in raw
