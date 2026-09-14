"""Shared scaffolding for the doctor test modules."""

from __future__ import annotations

from pathlib import Path

from harborrag_app.cli import main as cli
from harborrag_app.cli.doctor import probes


def project(tmp_path: Path, monkeypatch, capsys, *, api_key: str | None) -> None:
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


def unreachable(monkeypatch) -> None:
    monkeypatch.setattr(probes, "tcp_reachable", lambda *a, **k: "connection refused")
    monkeypatch.setattr(probes, "http_ok", lambda *a, **k: "connection refused")
    monkeypatch.setattr(probes, "redis_ping", lambda *a, **k: "connection refused")


__all__ = ["project", "unreachable"]
