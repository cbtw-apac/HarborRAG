"""Connector config dataclasses read the process environment only while allowed."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.environment import (
    environment_default,
    without_environment_defaults,
)
from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig


def test_environment_default_returns_the_first_non_empty_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST", "")
    monkeypatch.setenv("SECOND", "value")

    assert environment_default("FIRST", "SECOND") == "value"
    with without_environment_defaults():
        assert environment_default("FIRST", "SECOND") is None
    assert environment_default("MISSING") is None


def test_config_dataclasses_skip_the_fallback_inside_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "operator-token")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "operator-secret")

    assert GitHubRepositoryConfig(owner="a", repo="b").token == "operator-token"
    with without_environment_defaults():
        assert GitHubRepositoryConfig(owner="a", repo="b").token is None
        with pytest.raises(ValueError, match="client credentials"):
            SharePointSiteConfig(
                site_url="https://acme.sharepoint.com/sites/x",
                tenant_id="t",
                client_id="c",
                auth_mode="client_credentials",
            )
