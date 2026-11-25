from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from harborrag_app.cli import main as cli
from harborrag_app.cli.globals import OUTPUT_ENV, requested_output
from harborrag_app.cli.output import OutputFormat

pytestmark = pytest.mark.unit


@pytest.fixture
def malformed_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "harborrag.yaml").write_text("runtime: {oops\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.delenv(OUTPUT_ENV, raising=False)
    return root


@pytest.mark.usefixtures("malformed_project")
def test_a_project_error_under_json_output_is_the_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["me", "-o", "json"]) == 1

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "internal_error"
    assert "could not be read" in error["message"]
    assert error["details"] is None
    assert len(error["trace_id"]) == 32


@pytest.mark.usefixtures("malformed_project")
def test_a_project_error_under_yaml_output_is_the_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["me", "--output=YAML"]) == 1

    error = yaml.safe_load(capsys.readouterr().err)["error"]
    assert error["code"] == "internal_error"
    assert "could not be read" in error["message"]


@pytest.mark.usefixtures("malformed_project")
def test_a_project_error_in_a_terminal_without_a_format_stays_plain_text(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert cli.main(["me"]) == 1

    err = capsys.readouterr().err
    assert err.startswith("harborrag: ")
    assert "could not be read" in err


@pytest.fixture
def outside_any_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.delenv(OUTPUT_ENV, raising=False)
    return tmp_path


@pytest.mark.usefixtures("outside_any_project")
def test_no_project_under_json_output_is_the_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["connections", "list", "-o", "json"]) == 1

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "internal_error"
    assert error["message"].startswith("no project found.")
    assert "--project DIR" in error["message"]


@pytest.mark.usefixtures("outside_any_project")
def test_no_project_under_yaml_output_is_the_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["connections", "list", "--output=yaml"]) == 1

    error = yaml.safe_load(capsys.readouterr().err)["error"]
    assert error["message"].startswith("no project found.")


@pytest.mark.usefixtures("outside_any_project")
def test_no_project_in_a_terminal_without_a_format_keeps_the_documented_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert cli.main(["connections", "list"]) == 1

    assert capsys.readouterr().err == (
        "harborrag: no project found. Run it inside a HarborRAG repository checkout, or pass "
        "--project DIR for a directory containing harborrag.yaml.\n"
    )


@pytest.mark.parametrize(
    ("args", "environ", "expected"),
    [
        (["me", "-o", "json"], {}, OutputFormat.JSON),
        (["me", "-oyaml"], {}, OutputFormat.YAML),
        (["me", "--output", "TABLE"], {}, OutputFormat.TABLE),
        (["-o", "json", "me", "--output=yaml"], {}, OutputFormat.YAML),
        (["me", "-o", "xml"], {}, OutputFormat.TABLE),
        (["me", "--", "-o", "json"], {}, OutputFormat.TABLE),
        (["me"], {OUTPUT_ENV: " Json "}, OutputFormat.JSON),
        (["me", "-o", "yaml"], {OUTPUT_ENV: "json"}, OutputFormat.YAML),
        (["me"], {OUTPUT_ENV: "xml"}, OutputFormat.TABLE),
    ],
)
def test_requested_output_reads_the_flag_then_the_environment(
    args: list[str], environ: dict[str, str], expected: OutputFormat
) -> None:
    assert requested_output(args, environ, stdout_is_tty=True) is expected


def test_requested_output_defaults_to_json_away_from_a_terminal() -> None:
    assert requested_output(["me"], {}, stdout_is_tty=False) is OutputFormat.JSON
