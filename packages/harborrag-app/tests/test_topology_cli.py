"""Offline topology commands do not require endpoint calls or worker resources."""

import json

from typer.testing import CliRunner

from harborrag_app.cli.main import app


def test_topology_help_exposes_explicit_administration_controls():
    result = CliRunner().invoke(app, ["topology", "--help"])
    assert result.exit_code == 0
    for command in (
        "evaluate",
        "audit",
        "cleanup",
        "config",
        "enable",
        "disable",
        "inspect",
        "resolution",
        "worker",
    ):
        assert command in result.stdout


def test_topology_offline_evaluation_reports_counts_and_holdout_split(tmp_path):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "document_id": "doc-1",
                "split": "test",
                "input": {"chunk_id": "chunk-1", "content": "No entities."},
                "expected": {"entities": [], "assertions": []},
                "predicted": {"entities": [], "assertions": []},
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["topology", "evaluate", str(cases)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"]
    assert payload["data"]["units"] == 1
    assert payload["data"]["entities"]["expected"] == 0
    assert payload["data"]["by_split"]["test"]["documents"] == 1


def test_invalid_evaluation_does_not_echo_private_input(tmp_path):
    cases = tmp_path / "invalid.jsonl"
    cases.write_text('{"private": "not-for-logs"}', encoding="utf-8")
    result = CliRunner().invoke(app, ["topology", "evaluate", str(cases)])
    assert result.exit_code == 1
    assert "not-for-logs" not in result.output
    assert "error_code" in result.output


def test_topology_config_validate_is_offline_and_non_secret(tmp_path):
    policy = tmp_path / "graph_build.yaml"
    policy.write_text(
        "tenants:\n  - tenant_id: demo\n    mode: deterministic\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["topology", "config", "validate", "--path", str(policy)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["tenants"][0]["mode"] == "deterministic"
    assert payload["data"]["runtime"]["derived_enabled"] is False
