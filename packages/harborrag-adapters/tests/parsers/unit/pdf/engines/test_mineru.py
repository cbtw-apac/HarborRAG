"""Unit tests for the MinerU PDF provider engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.parsers.compat import MinerUBackend, MinerUBackendOptions

pytestmark = pytest.mark.unit


@pytest.mark.whitebox
def test_mineru_command_includes_advanced_cli_options():
    backend = MinerUBackend(
        MinerUBackendOptions(
            backend="hybrid-http-client",
            effort="high",
            method="ocr",
            language="ch",
            api_url="http://127.0.0.1:8000",
            server_url="http://127.0.0.1:30000",
            extra_args=("--debug", "true"),
        )
    )

    assert backend._command("mineru", Path("in.pdf"), Path("out")) == [
        "mineru",
        "-p",
        "in.pdf",
        "-o",
        "out",
        "-b",
        "hybrid-http-client",
        "--effort",
        "high",
        "--method",
        "ocr",
        "--lang",
        "ch",
        "--api-url",
        "http://127.0.0.1:8000",
        "--url",
        "http://127.0.0.1:30000",
        "--debug",
        "true",
    ]


@pytest.mark.whitebox
def test_mineru_cleans_up_custom_output_dir_when_run_fails(tmp_path, monkeypatch):
    base_dir = tmp_path / "custom-output"
    backend = MinerUBackend(MinerUBackendOptions(output_dir=base_dir, keep_output=False))

    def _raise(command: list[str]) -> None:
        raise RuntimeError("mineru exited non-zero")

    monkeypatch.setattr(backend, "_run", _raise)

    with pytest.raises(RuntimeError, match="mineru exited non-zero"):
        backend._parse_path("mineru", Path("in.pdf"))

    # The per-document subdirectory under output_dir must not leak even
    # though `_run` (not just `_read_output`) failed.
    assert list(base_dir.iterdir()) == []


def test_mineru_keeps_custom_output_dir_when_run_fails_and_keep_output_true(tmp_path, monkeypatch):
    base_dir = tmp_path / "custom-output"
    backend = MinerUBackend(MinerUBackendOptions(output_dir=base_dir, keep_output=True))

    def _raise(command: list[str]) -> None:
        raise RuntimeError("mineru exited non-zero")

    monkeypatch.setattr(backend, "_run", _raise)

    with pytest.raises(RuntimeError, match="mineru exited non-zero"):
        backend._parse_path("mineru", Path("in.pdf"))

    assert len(list(base_dir.iterdir())) == 1


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds must be greater than 0"),
        ({"method": "invalid"}, "method must be one of"),
        ({"effort": "invalid"}, "effort must be one of"),
    ],
)
def test_mineru_options_reject_invalid_cli_controls(overrides, match):
    with pytest.raises(ValueError, match=match):
        MinerUBackendOptions(**overrides)
