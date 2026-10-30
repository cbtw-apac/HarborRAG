"""local_file connections stay inside the configured source root."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.config.connectors.models import ConnectionValidationError, validate
from harborrag_runtime.config.connectors.models.local_file import (
    LOCAL_SOURCE_PATH_VARIABLE,
    LOCAL_SOURCE_ROOT_VARIABLE,
    local_source_root,
)
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.ingestion.connections import build_connector_from_parts


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    allowed = tmp_path / "sources"
    (allowed / "handbook").mkdir(parents=True)
    monkeypatch.delenv(LOCAL_SOURCE_PATH_VARIABLE, raising=False)
    monkeypatch.setenv(LOCAL_SOURCE_ROOT_VARIABLE, str(allowed))
    return allowed


def _issue(source_path: str) -> str:
    with pytest.raises(ConnectionValidationError) as caught:
        validate("local_file", {}, {"source_path": source_path})
    assert caught.value.issues[0].field == "config.source_path"
    return caught.value.issues[0].issue


def test_paths_inside_the_root_are_accepted(root: Path) -> None:
    validate("local_file", {}, {"source_path": str(root)})
    validate("local_file", {}, {"source_path": str(root / "handbook")})


def test_paths_outside_relative_or_escaping_through_a_link_are_refused(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "secrets"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    assert _issue("/proc/self").startswith("must be inside")
    assert _issue(str(root / ".." / "secrets")).startswith("must be inside")
    assert _issue(str(root / "escape")).startswith("must be inside")
    assert _issue("handbook") == "must be an absolute path"


def test_the_root_defaults_to_local_source_path_and_is_open_when_neither_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LOCAL_SOURCE_ROOT_VARIABLE, raising=False)
    monkeypatch.setenv(LOCAL_SOURCE_PATH_VARIABLE, str(tmp_path))
    assert local_source_root() == tmp_path.resolve()

    monkeypatch.delenv(LOCAL_SOURCE_PATH_VARIABLE)
    assert local_source_root() is None
    validate("local_file", {}, {"source_path": "/anywhere"})


def test_workers_refuse_to_build_a_connection_outside_the_root(root: Path) -> None:
    with pytest.raises(ConnectorConfigurationError, match="must be inside"):
        build_connector_from_parts(
            "local_file",
            credentials={},
            config={"source_path": "/proc/self"},
            tuning={},
            secret_values={},
        )
