"""A connection test builds its connector with the service's destination policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_adapters.connectors.policies.network import DestinationPolicy
from harborrag_runtime.application.control_plane import connection_testing
from harborrag_runtime.config.errors import ConnectorConfigurationError


@pytest.mark.asyncio
async def test_run_checks_builds_the_connector_with_the_given_destination_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def refuse(*_args: object, **kwargs: Any) -> None:
        captured.update(kwargs)
        raise ConnectorConfigurationError("stop before connecting")

    monkeypatch.setattr(connection_testing, "build_connector_from_parts", refuse)
    spec = SimpleNamespace(source_type="github", tuning_fields=(), secret_fields=())
    validated = SimpleNamespace(credentials={}, config={}, tuning={})
    policy = DestinationPolicy(hosts=frozenset({"jira.example.com"}))

    await connection_testing.run_checks(
        spec,  # type: ignore[arg-type]
        validated,  # type: ignore[arg-type]
        {},
        {},
        "tenant-1",
        policy,
    )

    assert captured["destination_policy"] is policy
