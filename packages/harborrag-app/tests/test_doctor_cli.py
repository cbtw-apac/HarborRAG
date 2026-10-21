"""doctor reports layered readiness and keeps the JSON envelope stable."""

from __future__ import annotations

import json
import socket
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


def test_doctor_ignores_variables_in_commented_out_model_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    models = tmp_path / "config" / "models.yaml"
    models.write_text(
        models.read_text(encoding="utf-8")
        + "# memory:\n#   deployments:\n#     - api_key: ${HARBOR_MEMORY_API_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARBOR_MEMORY_API_KEY", raising=False)
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["models catalog"]["status"] == "ok"


def test_doctor_scans_model_config_the_way_the_model_loader_reads_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # The model loader uses yaml.safe_load, which accepts a repeated key (last wins).
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    models = tmp_path / "config" / "models.yaml"
    models.write_text(
        models.read_text(encoding="utf-8") + "notes: first\nnotes: second\n",
        encoding="utf-8",
    )
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    assert checks["models catalog"]["status"] == "ok"


def test_doctor_fails_the_connector_catalog_when_one_definition_is_invalid(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    connectors = tmp_path / "config" / "connectors.yaml"
    connectors.write_text(
        connectors.read_text(encoding="utf-8")
        + "  broken:\n    provider: jira\n    settings:\n      not_a_field: 1\n",
        encoding="utf-8",
    )
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}
    catalog = checks["connectors catalog"]
    assert catalog["status"] == "fail"
    assert "broken" in catalog["detail"]
    assert "workspace" in catalog["detail"]


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
    monkeypatch.setattr(probes, "s3_credentials", lambda *a, **k: None)
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


def _reachable(monkeypatch) -> None:
    monkeypatch.setattr(probes, "tcp_reachable", lambda *a, **k: None)
    monkeypatch.setattr(probes, "http_ok", lambda *a, **k: None)
    monkeypatch.setattr(probes, "redis_ping", lambda *a, **k: None)
    monkeypatch.setattr(probes, "s3_credentials", lambda *a, **k: None)


def _doctor_checks(capsys) -> dict[str, dict]:
    return {c["name"]: c for c in json.loads(capsys.readouterr().out)["data"]["checks"]}


def _checkout(tmp_path: Path, monkeypatch, capsys, *, api_key: str | None) -> None:
    """A repository checkout: scaffolded catalogs, but no harborrag.yaml."""

    project(tmp_path, monkeypatch, capsys, api_key=api_key)
    (tmp_path / "harborrag.yaml").unlink()
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)


def test_doctor_fails_object_store_with_bad_credentials(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _reachable(monkeypatch)
    monkeypatch.setattr(probes, "s3_credentials", lambda *a, **k: "InvalidAccessKeyId")
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    store = _doctor_checks(capsys)["object store"]
    assert store["status"] == "fail"
    assert "InvalidAccessKeyId" in store["detail"]
    assert "HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID" in store["hint"]


def test_doctor_probes_qdrant_grpc_when_preferred(tmp_path: Path, monkeypatch, capsys) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _reachable(monkeypatch)
    monkeypatch.setenv("HARBORRAG_QDRANT_PREFER_GRPC", "true")
    monkeypatch.setattr(
        probes,
        "tcp_reachable",
        lambda host, port, **k: f"{host}:{port} unreachable" if port == 6334 else None,
    )
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    qdrant = _doctor_checks(capsys)["qdrant"]
    assert qdrant["status"] == "fail"
    assert "6334" in qdrant["detail"]
    assert "HARBORRAG_QDRANT_PREFER_GRPC=false" in qdrant["hint"]


def test_doctor_warns_on_checkout_sqlite_fallback(tmp_path: Path, monkeypatch, capsys) -> None:
    _checkout(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _reachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    control = _doctor_checks(capsys)["control db"]
    assert control["status"] == "warn"
    assert control["required"] is False
    assert "env/.env.database" in control["hint"]


def test_doctor_has_no_control_db_check_in_project_mode(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project(tmp_path, monkeypatch, capsys, api_key="sk-test")
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    _reachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    assert "control db" not in _doctor_checks(capsys)


def test_doctor_hints_name_the_checkout_files(tmp_path: Path, monkeypatch, capsys) -> None:
    _checkout(tmp_path, monkeypatch, capsys, api_key=None)
    unreachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    cli.main(["doctor", "--json"])

    checks = _doctor_checks(capsys)
    assert "env/.env.models" in checks["models catalog"]["hint"]
    assert "scripts/deployment/dev.sh data" in checks["qdrant"]["hint"]
    assert "docker compose up -d" not in checks["qdrant"]["hint"]


def test_s3_credentials_probe_reports_only_a_code_for_an_unreachable_endpoint() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    result = probes.s3_credentials(f"http://127.0.0.1:{port}", None, "AKIDSECRET", "hunter2", None)

    assert result == "unreachable"


def test_s3_access_denied_counts_as_authenticated(monkeypatch) -> None:
    import botocore.session
    from botocore.exceptions import ClientError

    class Client:
        def list_buckets(self):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListBuckets")

    class Session:
        def create_client(self, *a, **k):
            return Client()

    monkeypatch.setattr(botocore.session, "get_session", lambda: Session())

    assert probes.s3_credentials("http://x:9000", None, "k", "s", None) is None


def test_s3_rejected_credentials_report_the_code(monkeypatch) -> None:
    import botocore.session
    from botocore.exceptions import ClientError

    class Client:
        def list_buckets(self):
            raise ClientError({"Error": {"Code": "SignatureDoesNotMatch"}}, "ListBuckets")

    class Session:
        def create_client(self, *a, **k):
            return Client()

    monkeypatch.setattr(botocore.session, "get_session", lambda: Session())

    assert probes.s3_credentials("http://x:9000", None, "k", "s", None) == "SignatureDoesNotMatch"


def test_doctor_control_plane_hint_says_shell_in_checkout_mode(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    class Broken:
        def __init__(self) -> None:
            raise RuntimeError("boom")

    _checkout(tmp_path, monkeypatch, capsys, api_key="sk-test")
    _reachable(monkeypatch)
    monkeypatch.setattr(cli_runner, "runtime_app_service", Broken)

    cli.main(["doctor", "--json"])

    plane = _doctor_checks(capsys)["control plane"]
    assert "shell" in plane["hint"]
    assert "env/.env.database" not in plane["hint"]
