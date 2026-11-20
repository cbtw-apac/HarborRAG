"""The application service gives the connection test the destination policy from the environment."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.policies.network import (
    ALLOWED_HOSTS_VARIABLE,
    USE_PROXY_VARIABLE,
    DestinationPolicy,
)
from harborrag_runtime.application.composition.service import AppService

from ..application_fixtures import FakeComposition

pytestmark = pytest.mark.unit


def test_connection_service_holds_the_policy_built_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VARIABLE, "jira.example.test,10.20.0.0/16")
    monkeypatch.setenv(USE_PROXY_VARIABLE, "true")

    service = AppService(FakeComposition({"runtime": {"ready": True}}))

    policy = service._connection_service._destination_policy
    assert policy == DestinationPolicy.from_environ()
    assert policy != DestinationPolicy()
    assert "jira.example.test" in policy.hosts
