"""Secret references: parsing, the stored form and the read-side status."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harborrag_core.domain.validation import validate_secret_free_config
from harborrag_core.security.secret_refs import (
    SecretRef,
    StoredSecret,
    all_refs,
    credentials_view,
    secret_status,
    stored_refs,
)

_NOW = datetime(2026, 10, 28, 9, 14, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "source", "target"),
    [
        ("env:JIRA_TOKEN", "env", "JIRA_TOKEN"),
        ("vault:kv/jira#token", "vault", "kv/jira#token"),
        ("secret://db/0f3a", "stored", "secret://db/0f3a"),
    ],
)
def test_parse_recognizes_each_scheme(raw: str, source: str, target: str) -> None:
    ref = SecretRef.parse(raw)

    assert (ref.source, ref.target) == (source, target)
    assert ref.is_stored is (source == "stored")


@pytest.mark.parametrize("raw", ["", "env:", "env:1BAD", "env:has space", "s3://bucket", "vault:"])
def test_parse_rejects_malformed_references(raw: str) -> None:
    with pytest.raises(ValueError):
        SecretRef.parse(raw)


def test_a_stored_value_reports_its_last_four_characters() -> None:
    stored = StoredSecret.for_value(SecretRef("secret://db/1"), "ATATT3xFfGF09f2c", _NOW)

    assert stored.to_json() == {
        "secret_ref": "secret://db/1",
        "last4": "9f2c",
        "updated_at": "2026-10-28T09:14:03+00:00",
    }
    assert stored.status() == {
        "set": True,
        "last4": "9f2c",
        "updated_at": "2026-10-28T09:14:03+00:00",
        "from": "stored",
    }


def test_a_short_value_never_exposes_last_four() -> None:
    assert StoredSecret.for_value(SecretRef("secret://db/1"), "abc12", _NOW).last4 is None


def test_a_reference_reports_where_it_lives_without_last_four() -> None:
    stored = StoredSecret.for_reference(SecretRef.parse("env:JIRA_TOKEN"), _NOW)

    assert secret_status(stored.to_json()) == {
        "set": True,
        "last4": None,
        "updated_at": "2026-10-28T09:14:03+00:00",
        "from": "env",
    }


def test_credentials_view_replaces_only_secret_fields() -> None:
    credentials = {
        "base_url": "https://acme.atlassian.net",
        "api_token": {"secret_ref": "secret://db/1", "last4": "9f2c", "updated_at": None},
        "email_ref": {"secret_ref": "env:JIRA_EMAIL"},
    }

    view = credentials_view(credentials)

    assert view["base_url"] == "https://acme.atlassian.net"
    assert view["api_token"]["from"] == "stored" and "secret_ref" not in view["api_token"]
    assert view["email_ref"]["from"] == "env"
    assert stored_refs(credentials) == ["secret://db/1"]
    assert all_refs(credentials) == ["secret://db/1", "env:JIRA_EMAIL"]


def test_domain_validation_accepts_stored_and_external_references() -> None:
    validate_secret_free_config(
        {
            "api_token": {"secret_ref": "secret://db/1", "last4": "9f2c", "updated_at": None},
            "client_secret": {"secret_ref": "env:MICROSOFT_CLIENT_SECRET"},
            "token": {"secret_ref": "vault:kv/github"},
        }
    )


@pytest.mark.parametrize(
    "value",
    [
        "raw-secret",
        {"secret_ref": "s3://bucket/key"},
        {"secret_ref": "secret://db/1", "value": "raw-secret"},
    ],
)
def test_domain_validation_rejects_raw_or_smuggled_values(value: object) -> None:
    with pytest.raises(ValueError, match="secret reference"):
        validate_secret_free_config({"api_token": value})
