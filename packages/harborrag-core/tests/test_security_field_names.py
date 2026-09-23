"""One definition of what a field name says it carries."""

from __future__ import annotations

import pytest

from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.validation import validate_secret_free_config
from harborrag_core.models.errors import HarborModelError
from harborrag_core.security.field_names import (
    SENSITIVE_FIELD_TOKENS,
    is_sensitive_field_name,
)
from harborrag_core.security.redaction import redact_secrets


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
