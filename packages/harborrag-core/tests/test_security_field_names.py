"""One definition of what a field name says it carries."""

from __future__ import annotations

import time

import pytest
from pydantic import SecretStr

from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.validation import validate_secret_free_config
from harborrag_core.models.embed import HarborEmbedRequest
from harborrag_core.models.errors import HarborModelError
from harborrag_core.security.field_names import (
    SENSITIVE_FIELD_TOKENS,
    is_sensitive_field_name,
)
from harborrag_core.security.redaction import redact_mapping, redact_secrets


@pytest.mark.parametrize(
    "name",
    [
        "token",
        "password",
        "passwd",
        "secret",
        "credential",
        "authorization",
        "api_key",
        "apiKey",
        "api-key",
        "access_key",
        "access_token",
        "private_key",
        "PrivateKey",
        # Compound entries used to be compared against the whole name, so a
        # prefix was all it took to store a raw key.
        "my_api_key",
        "ssh_private_key",
        "customer_access_token",
    ],
)
def test_a_credential_field_is_recognized(name: str) -> None:
    assert is_sensitive_field_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        # "token" also means an LLM accounting unit. These are quantities.
        "token_budget",
        "max_tokens",
        "token_limit",
        "tokens_per_minute",
        "chunk_overlap_tokens",
        "token_count",
        # Unrelated names that merely contain a sensitive substring.
        "tokenizer_name",
        "nickname",
        "keyspace",
    ],
)
def test_a_quantity_is_not_mistaken_for_a_credential(name: str) -> None:
    assert is_sensitive_field_name(name) is False


def test_a_token_budget_is_storable_configuration() -> None:
    """The regression this exception exists for.

    ``token`` matched blindly, so ``{"token_budget": 8000}`` had to "use a
    secret reference" and could not be stored at all, while ``max_tokens``
    passed only because the plural happens not to match.
    """

    validate_secret_free_config({"token_budget": 8000, "max_tokens": 4096})
    WorkspaceSettings(tenant_id="ACME", data={"token_budget": 8000})


@pytest.mark.parametrize("name", ["private_key", "passwd", "ssh_private_key"])
def test_the_newly_shared_names_are_refused_by_configuration(name: str) -> None:
    """These were known to redaction but not to configuration validation."""

    with pytest.raises(ValueError, match="secret reference"):
        validate_secret_free_config({name: "-----BEGIN PRIVATE KEY-----"})


def test_a_secret_reference_is_still_the_way_to_carry_one() -> None:
    validate_secret_free_config({"private_key": "secret://vault/key"})
    validate_secret_free_config({"passwd": {"secret_ref": "secret://vault/pw"}})


def test_every_consumer_agrees_on_one_definition() -> None:
    """The three definitions had already drifted apart once.

    Configuration validation, model-error sanitisation and text redaction each
    kept their own list. Only redaction knew ``private_key``; none knew
    ``passwd``. A name one of them recognises and another does not is a
    credential that reaches a log or a database row.
    """

    for name in SENSITIVE_FIELD_TOKENS:
        assert is_sensitive_field_name(name) is True, name
        with pytest.raises(ValueError, match="secret reference"):
            validate_secret_free_config({name: "raw-value"})
        assert "raw-value" not in redact_secrets(f"{name}=raw-value"), name
        error = HarborModelError("failed", metadata={name: "raw-value"})
        assert error.metadata[name] == "<redacted>", name


@pytest.mark.parametrize(
    "name",
    [
        "APIKey",
        "ApiKey",
        "apikey",
        "X-API-KEY",
        "x-api-key",
        "api-key",
        "credentials",
        "secrets",
        "passwords",
        "privatekey",
        "accesskey",
        "bearer",
        "Bearer",
        "passwd",
        "clientsecret",
        "sessionToken",
        "sessiontoken",
        "dbPassword",
    ],
)
def test_every_spelling_of_a_credential_is_recognized(name: str) -> None:
    assert is_sensitive_field_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["author", "monkey", "keyword", "max_output_tokens", "tokenizer", "token_usage"],
)
def test_lookalikes_of_credentials_are_not_flagged(name: str) -> None:
    assert is_sensitive_field_name(name) is False


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "Proxy-Authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "ocp-apim-subscription-key",
        "x-auth-token",
        "X-Session-Token",
    ],
)
def test_credential_request_headers_are_wrapped_as_secrets(header: str) -> None:
    request = HarborEmbedRequest(inputs=("hello",), custom_headers={header: "raw-value"})
    assert isinstance(request.custom_headers[header], SecretStr), header


@pytest.mark.parametrize(
    "header", ["Idempotency-Key", "X-Request-Id", "Content-Type", "Accept", "User-Agent"]
)
def test_benign_request_headers_stay_plain_and_are_accepted(header: str) -> None:
    request = HarborEmbedRequest(inputs=("hello",), custom_headers={header: "value"})
    assert request.custom_headers[header] == "value"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('curl -H "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"', "eyJhbGciOiJIUzI1NiJ9"),
        ("clone https://alice:hunter2@git.example/repo.git", "hunter2"),
        ("clone https://deploytoken123@git.example/repo.git", "deploytoken123"),
        ("Cookie: session=abc123; theme=dark", "abc123"),
        ("Set-Cookie: sid=s3cr3t; Path=/; HttpOnly", "s3cr3t"),
        (
            "https://acct.blob.core.windows.net/c/b?sv=2022-11-02&sig=AbC%2Bxyz%3D&se=2026",
            "AbC%2Bxyz%3D",
        ),
        (
            "https://tenant.sharepoint.com/_layouts/download.aspx?tempauth=eyJ0eXAi.abc",
            "eyJ0eXAi.abc",
        ),
        ("https://bucket.s3.amazonaws.com/k?X-Amz-Signature=deadbeef01", "deadbeef01"),
    ],
)
def test_unlabeled_credentials_in_free_text_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact_secrets(text)


@pytest.mark.parametrize(
    "text", ["token_budget=500", "token_count: 12", "max_token_count=9", "tokens=5"]
)
def test_token_quantities_survive_redaction(text: str) -> None:
    assert redact_secrets(text) == text


def test_redact_mapping_uses_the_shared_definition() -> None:
    redacted = redact_mapping(
        {
            "token_budget": 500,
            "token_count": 3,
            "APIKey": "k1",
            "credentials": "c1",
            "Cookie": "sid=1",
            "Set-Cookie": "sid=2",
            "team": "platform",
        }
    )

    assert redacted == {
        "token_budget": 500,
        "token_count": 3,
        "APIKey": "<redacted>",
        "credentials": "<redacted>",
        "Cookie": "<redacted>",
        "Set-Cookie": "<redacted>",
        "team": "platform",
    }


@pytest.mark.parametrize(
    "line",
    ["a." * 50000, "a+" * 50000, "a-" * 50000 + "://"],
    ids=["dots", "plus", "dashes"],
)
def test_redaction_is_linear_on_adversarial_lines(line: str) -> None:
    started = time.perf_counter()
    redact_secrets(line)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com?email=a@b.com",
        "https://example.com/path#frag@x",
        "see https://example.com/a@b",
        "Bearer authentication is required",
    ],
)
def test_text_without_credentials_is_unchanged(text: str) -> None:
    assert redact_secrets(text) == text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('Cookie: "a=b"; other=c', "other=c"),
        ("https://b.storage.googleapis.com/o?X-Goog-Signature=cafe01", "cafe01"),
        ("https://b.s3.amazonaws.com/k?X-Amz-Security-Token=FwoGZXIv", "FwoGZXIv"),
    ],
)
def test_remaining_credential_shapes_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact_secrets(text)


def test_redaction_is_linear_on_a_long_whitespace_run() -> None:
    line = "password=x" + " " * 50000 + "y"
    started = time.perf_counter()
    redacted = redact_secrets(line)
    assert time.perf_counter() - started < 0.5
    assert "x" not in redacted
