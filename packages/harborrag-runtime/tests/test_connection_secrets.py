"""Connection secret writes and resolution: write, rotate, keep, clear, reference."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from harborrag_core.testing.fakes import FakeSecrets
from harborrag_runtime.config.connectors.models import (
    ConnectionValidationError,
    to_connector_config,
)
from harborrag_runtime.connections import (
    CredentialSecretWriter,
    SecretResolver,
    SecretUnavailableError,
    default_secret_resolver,
)

_NOW = datetime(2026, 10, 29, tzinfo=UTC)
_FIELDS = frozenset({"api_token"})
_SENTINEL = "ATATT3x-sentinel-value-9f2c"


def _writer(secrets: FakeSecrets, **kwargs: Any) -> CredentialSecretWriter:
    return CredentialSecretWriter(secrets, tenant_id="acme", now=_NOW, **kwargs)


@pytest.mark.asyncio
async def test_write_then_rotate_then_keep_then_clear() -> None:
    secrets = FakeSecrets()
    base = {"base_url": "https://acme.atlassian.net", "email": "bot@acme.com"}

    written = await _writer(secrets).apply({**base, "api_token": _SENTINEL}, secret_fields=_FIELDS)
    first_ref = written.credentials["api_token"]["secret_ref"]
    assert written.credentials["api_token"]["last4"] == "9f2c"
    assert written.created == [first_ref] and written.retired == []

    rotated = await _writer(secrets).apply(
        {**base, "api_token": "rotated-token-abcd"},
        secret_fields=_FIELDS,
        existing=written.credentials,
    )
    second_ref = rotated.credentials["api_token"]["secret_ref"]
    assert second_ref != first_ref
    assert (rotated.created, rotated.retired) == ([second_ref], [first_ref])

    kept = await _writer(secrets).apply(base, secret_fields=_FIELDS, existing=rotated.credentials)
    assert kept.credentials["api_token"] == rotated.credentials["api_token"]
    assert (kept.created, kept.retired) == ([], [])

    cleared = await _writer(secrets).apply(
        {**base, "api_token": None}, secret_fields=_FIELDS, existing=kept.credentials
    )
    assert "api_token" not in cleared.credentials
    assert cleared.retired == [second_ref]


@pytest.mark.asyncio
async def test_echoing_the_ref_or_the_status_keeps_the_stored_value() -> None:
    secrets = FakeSecrets()
    stored = await _writer(secrets).apply({"api_token": _SENTINEL}, secret_fields=_FIELDS)
    current = stored.credentials["api_token"]

    for echo in ({"secret_ref": current["secret_ref"]}, {"set": True, "last4": "9f2c"}):
        result = await _writer(secrets).apply(
            {"api_token": echo}, secret_fields=_FIELDS, existing=stored.credentials
        )
        assert result.credentials["api_token"] == current
        assert (result.created, result.retired) == ([], [])


@pytest.mark.asyncio
async def test_an_env_reference_replaces_a_stored_value_and_has_no_last4() -> None:
    secrets = FakeSecrets()
    stored = await _writer(secrets).apply({"api_token": _SENTINEL}, secret_fields=_FIELDS)

    result = await _writer(secrets).apply(
        {"api_token": {"secret_ref": "env:JIRA_TOKEN"}},
        secret_fields=_FIELDS,
        existing=stored.credentials,
    )

    assert result.credentials["api_token"] == {
        "secret_ref": "env:JIRA_TOKEN",
        "last4": None,
        "updated_at": "2026-10-29T00:00:00+00:00",
    }
    assert result.retired == stored.created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "issue"),
    [
        ({"secret_ref": "vault:kv/jira"}, "vault references are not supported here"),
        ({"secret_ref": "secret://db/someone-elses"}, "stored references are not supported here"),
        ({"secret_ref": "s3://bucket"}, "must start with"),
        ("", "must not be empty"),
        (42, "must be a string or a secret reference"),
        ({"set": True}, "has no stored value to keep"),
    ],
)
async def test_unusable_secret_values_are_field_errors(value: object, issue: str) -> None:
    with pytest.raises(ConnectionValidationError) as caught:
        await _writer(FakeSecrets()).apply({"api_token": value}, secret_fields=_FIELDS)

    assert caught.value.issues[0].field == "credentials.api_token"
    assert issue in caught.value.issues[0].issue


@pytest.mark.asyncio
async def test_a_failed_store_discards_the_secrets_it_already_wrote() -> None:
    class _FailsSecond(FakeSecrets):
        async def put(self, value: str, *, tenant_id: str) -> str:
            if self.counter >= 1:
                raise RuntimeError("store down")
            return await super().put(value, tenant_id=tenant_id)

    secrets = _FailsSecond()
    with pytest.raises(RuntimeError, match="store down"):
        await _writer(secrets).apply(
            {"client_secret": "first-secret-1", "access_token": "second-secret-2"},
            secret_fields=frozenset({"client_secret", "access_token"}),
        )

    assert secrets.values == {}


@pytest.mark.asyncio
async def test_resolver_reads_stored_and_env_references() -> None:
    secrets = FakeSecrets()
    stored = await _writer(secrets).apply(
        {"api_token": _SENTINEL, "client_secret": {"secret_ref": "env:SP_SECRET"}},
        secret_fields=frozenset({"api_token", "client_secret"}),
    )
    resolver = default_secret_resolver(secrets, environ={"SP_SECRET": "from-env"})

    values = await resolver.resolve_credentials(stored.credentials, tenant_id="acme")

    assert values == {"api_token": _SENTINEL, "client_secret": "from-env"}
    with pytest.raises(SecretUnavailableError):
        await resolver.resolve_credentials(stored.credentials, tenant_id="other-tenant")


@pytest.mark.asyncio
async def test_resolver_reports_unset_env_and_unconfigured_vault() -> None:
    resolver = default_secret_resolver(FakeSecrets(), environ={})
    with pytest.raises(SecretUnavailableError, match="JIRA_TOKEN is not set"):
        await resolver.resolve_credentials(
            {"api_token": {"secret_ref": "env:JIRA_TOKEN"}}, tenant_id="acme"
        )
    with pytest.raises(SecretUnavailableError, match="vault secrets are not configured"):
        await SecretResolver([]).resolve_credentials(
            {"api_token": {"secret_ref": "vault:kv/x"}}, tenant_id="acme"
        )


def test_connections_built_from_stored_values_never_borrow_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "operator-token")
    monkeypatch.setenv("JIRA_EMAIL", "operator@acme.com")

    public = to_connector_config("github", {"method": "none"}, {"owner": "a", "repo": "b"}, {}, {})
    data_center = to_connector_config(
        "jira",
        {"method": "pat", "base_url": "https://jira.acme.internal", "api_token": "t-12345678"},
        {"project_keys": ["ENG"]},
        {},
        {},
    )

    assert public.token is None
    assert data_center.email is None


@pytest.mark.asyncio
async def test_no_secret_value_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secrets = FakeSecrets()

    stored = await _writer(secrets).apply({"api_token": _SENTINEL}, secret_fields=_FIELDS)
    await _writer(secrets).apply(
        {"api_token": "another-sentinel-value"}, secret_fields=_FIELDS, existing=stored.credentials
    )
    await default_secret_resolver(secrets).resolve_credentials(stored.credentials, tenant_id="acme")
    with pytest.raises(ConnectionValidationError) as caught:
        await _writer(secrets).apply({"api_token": 7}, secret_fields=_FIELDS)

    assert _SENTINEL not in caplog.text
    assert "another-sentinel-value" not in caplog.text
    assert _SENTINEL not in repr(stored) and _SENTINEL not in str(caught.value.details)
