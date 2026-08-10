"""Unit coverage for connection-ID smoke runner dispatch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import run as run_one
import run_all

from harborrag_runtime.config import ConnectorCatalog, ConnectorDefinition

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _catalog(*definitions: ConnectorDefinition) -> ConnectorCatalog:
    return ConnectorCatalog(
        connectors={definition.name: definition for definition in definitions},
        source_path=Path("connectors.yaml"),
        version=1,
    )


def test_run_one_dispatches_selected_connection_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = ConnectorDefinition(name="harborrag-workspace", provider="local")
    monkeypatch.setattr(
        run_one,
        "_arguments",
        lambda: SimpleNamespace(
            connector="harborrag-workspace",
            limit=2,
            output=None,
            output_dir=None,
        ),
    )
    monkeypatch.setattr(run_one, "connector_definition", lambda identifier: definition)
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(run_one, "RUNNERS", {"local": fake_run})

    assert run_one.main() == 0
    assert calls == [
        {
            "connection_id": "harborrag-workspace",
            "limit": 2,
            "output": None,
            "output_dir": None,
        }
    ]


def test_run_all_iterates_enabled_connection_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = ConnectorDefinition(name="harborrag-workspace", provider="local")
    disabled = ConnectorDefinition(
        name="jira-disabled",
        provider="jira",
        enabled=False,
    )
    monkeypatch.setattr(run_all, "load_env", lambda: [])
    monkeypatch.setattr(run_all, "connector_catalog", lambda: _catalog(local, disabled))
    calls: list[str] = []

    def fake_run(*, connection_id: str) -> int:
        calls.append(connection_id)
        return 0

    monkeypatch.setattr(run_all, "RUNNERS", {"local": fake_run})

    assert run_all.main() == 0
    assert calls == ["harborrag-workspace"]
