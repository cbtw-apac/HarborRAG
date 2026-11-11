"""``harborrag settings``: levels, ``key=value`` parsing, the read for the ETag and refusals."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from cli_fakes import FakeOperations, serving

from harborrag_app.api.v1.admin.settings_schemas import (
    SettingChangeOut,
    SettingsOut,
    SettingsPatchOut,
)
from harborrag_app.cli import invoke
from harborrag_app.cli import main as cli
from harborrag_app.cli.commands.settings import parse_setting
from harborrag_app.operations import Tagged
from harborrag_core.config_store import SettingsReindexRequiredError

pytestmark = pytest.mark.unit

_ETAG = 'W/"rev-4"'
_READ = SettingsOut(level="tenant", revision="rev_4", values={"retrieval.top_k": 8})
_PATCHED = SettingsPatchOut(
    revision="rev_5",
    dry_run=False,
    changed=[SettingChangeOut(key="chunking.max_tokens", **{"from": 512}, to=384, applies="live")],
)


@pytest.fixture(autouse=True)
def _hermetic(cli_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(cli_project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("HARBORRAG_TENANT_ID", "HARBORRAG_OUTPUT", "HARBORRAG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging.getLogger("harborrag"), "handlers", [])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOperations:
    operations = FakeOperations(
        results={
            "settings_schema": {"groups": {"chunking": {}}},
            "read_settings": Tagged(_READ, _ETAG),
            "patch_settings": Tagged(_PATCHED, 'W/"rev-5"'),
        }
    )
    monkeypatch.setattr(invoke, "local_operations", serving(operations))
    return operations


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(["settings", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_parse_setting_reads_json_values_and_falls_back_to_the_raw_string() -> None:
    assert parse_setting("chunking.max_tokens=384") == ("chunking.max_tokens", 384)
    assert parse_setting("parsing.pdf_parser=liteparse") == ("parsing.pdf_parser", "liteparse")
    assert parse_setting("retrieval.top_k=null") == ("retrieval.top_k", None)
    assert parse_setting("a=b=c") == ("a", "b=c")
    assert parse_setting('a="7"') == ("a", "7")
    assert parse_setting("a=[1, 2]") == ("a", [1, 2])


def test_parse_setting_without_an_equals_sign_is_a_usage_error() -> None:
    with pytest.raises(Exception, match="KEY=VALUE"):
        parse_setting("chunking.max_tokens")


def test_schema_prints_the_body(fake: FakeOperations, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run(capsys, "schema", "-o", "json")

    assert code == 0
    assert json.loads(out) == {"groups": {"chunking": {}}}
    assert fake.calls[0].operation_id == "settings_schema"


def test_get_defaults_to_the_tenant_level(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "get", "-o", "json")

    assert code == 0
    assert SettingsOut.model_validate_json(out) == _READ
    assert fake.calls[0].kwargs == {"level": "tenant", "effective": False}


def test_get_passes_the_level_and_effective(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _run(capsys, "get", "--level", "project:demo", "--effective")

    assert code == 0
    assert fake.calls[0].kwargs == {"level": "project:demo", "effective": True}


def test_set_sends_parsed_changes_with_the_etag_of_a_read(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(
        capsys,
        "set",
        "chunking.max_tokens=384",
        "parsing.pdf_parser=liteparse",
        "retrieval.top_k=null",
        "--level",
        "project:demo",
        "-o",
        "json",
    )

    assert code == 0
    assert SettingsPatchOut.model_validate_json(out).revision == "rev_5"
    read, patch = fake.calls
    assert read.operation_id == "read_settings"
    assert read.kwargs["level"] == "project:demo"
    assert patch.args[1] == {
        "chunking.max_tokens": 384,
        "parsing.pdf_parser": "liteparse",
        "retrieval.top_k": None,
    }
    assert patch.kwargs == {
        "level": "project:demo",
        "dry_run": False,
        "reindex": False,
        "if_match": _ETAG,
    }


def test_set_with_if_match_skips_the_read(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _run(capsys, "set", "a=1", "--if-match", '"rev-9"', "--dry-run")

    assert code == 0
    [patch] = fake.calls
    assert patch.kwargs["if_match"] == '"rev-9"'
    assert patch.kwargs["dry_run"] is True


def test_set_reads_the_changes_of_a_file(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    changes = tmp_path / "changes.json"
    changes.write_text(json.dumps({"chunking.max_tokens": 256}), encoding="utf-8")

    code, _, _ = _run(capsys, "set", "-f", str(changes))

    assert code == 0
    assert fake.calls[-1].args[1] == {"chunking.max_tokens": 256}


def test_set_with_pairs_and_a_file_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    changes = tmp_path / "c.json"
    changes.write_text("{}", encoding="utf-8")

    code, _, err = _run(capsys, "set", "a=1", "-f", str(changes))

    assert code == 2
    assert "either" in err
    assert fake.calls == []


def test_set_without_changes_is_a_usage_error(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _run(capsys, "set")

    assert code == 2
    assert fake.calls == []


def test_a_reindex_required_change_exits_4(
    fake: FakeOperations, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.errors["patch_settings"] = SettingsReindexRequiredError("embedding.model needs a reindex")

    code, _, err = _run(capsys, "set", "embedding.model=x", "-o", "table")

    assert code == 4
    assert err.startswith("error 409 reindex_required: ")
