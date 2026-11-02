"""Doctor's chunking-policy check: file validity and code-grammar availability."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_app.cli.doctor import suite
from harborrag_runtime.config.settings import RuntimeSettings


def _settings(tmp_path: Path, text: str) -> RuntimeSettings:
    path = tmp_path / "chunking.yaml"
    path.write_text(text, encoding="utf-8")
    return RuntimeSettings(chunking_config_path=path)


@pytest.mark.parametrize(
    ("text", "problems", "status"),
    [
        ("version: 1\n", [], "ok"),
        ("version: 1\n", ["tree-sitter-go missing"], "warn"),
        ("version: 1\nrefiner: {require_code_grammars: true}\n", ["tree-sitter missing"], "fail"),
        ("version: 1\nrefiner: {strategy: recursive}\n", ["tree-sitter missing"], "ok"),
        ("version: 1\nprofiles: nope\n", [], "fail"),
    ],
)
def test_chunking_check_reports_policy_and_grammar_state(
    tmp_path: Path, monkeypatch, text: str, problems: list[str], status: str
) -> None:
    from harborrag_runtime.ingestion import chunking_profile

    monkeypatch.setattr(chunking_profile, "grammar_set_problems", lambda: problems)

    check = suite._chunking_check(_settings(tmp_path, text))

    assert check.name == "chunking policy"
    assert check.status == status


def test_chunking_check_falls_back_to_built_in_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    check = suite._chunking_check(RuntimeSettings())

    assert "built-in defaults" in check.detail
