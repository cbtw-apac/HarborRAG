"""Contracts that used to hold only by accident."""

from __future__ import annotations

import pytest

from harborrag_core.base import StrictModel
from harborrag_core.chunking.metadata import FrozenMetadata
from harborrag_core.models.chat import (
    FinishReason,
    HarborChatMessage,
    HarborChatResponse,
)
from harborrag_core.ports.agent_runs import AgentEvidenceReference, AgentRunIdentity
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.security.url_policy import URLPolicy, URLPolicyError


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:99999/",
        "http://[::1/",
        "http://host:notaport/",
        "http://example.com:-1/",
    ],
)
def test_a_malformed_url_raises_the_policy_error(url: str) -> None:
    """urlparse and .port raise a bare ValueError that used to escape.

    A caller mapping URLPolicyError to a 400 got an unhandled 500, and one
    using ``except URLPolicyError`` as its deny path treated the URL as fine.
    """

    with pytest.raises(URLPolicyError):
        URLPolicy().validate(url)


def test_a_valid_url_still_resolves_through_the_policy() -> None:
    """The malformed-input guard must not swallow the checks after it."""

    public = URLPolicy(resolver=lambda host, port: ["93.184.216.34"])
    public.validate("https://example.com/path")
    public.validate("https://example.com:8443/path")

    metadata = URLPolicy(resolver=lambda host, port: ["169.254.169.254"])
    with pytest.raises(URLPolicyError, match="not allowed"):
        metadata.validate("http://metadata.internal/latest/meta-data/")

    with pytest.raises(URLPolicyError, match="scheme"):
        public.validate("file:///etc/passwd")


def _response(**overrides: object) -> HarborChatResponse:
    fields: dict[str, object] = {
        "id": "resp-1",
        "logical_model": "m",
        "provider": "p",
        "provider_model": "pm",
        "deployment": "d",
        "message": HarborChatMessage.assistant("hi"),
    }
    fields.update(overrides)
    return HarborChatResponse(**fields)  # type: ignore[arg-type]


def test_a_finish_reason_is_the_enum_not_a_lookalike_string() -> None:
    """``str | FinishReason`` always resolved to ``str`` under a smart union.

    So ``response.finish_reason is FinishReason.STOP`` was False for every
    response ever built, and the enum half of the annotation was decoration.
    """

    assert _response(finish_reason="stop").finish_reason is FinishReason.STOP
    assert _response(finish_reason=FinishReason.LENGTH).finish_reason is FinishReason.LENGTH


def test_an_unfamiliar_finish_reason_does_not_fail_a_successful_call() -> None:
    """Metadata about a completed call must not turn it into an error."""

    assert _response(finish_reason="a_new_provider_reason").finish_reason is FinishReason.UNKNOWN
    assert _response(finish_reason=None).finish_reason is FinishReason.UNKNOWN
    assert _response().finish_reason is FinishReason.UNKNOWN


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_isolation_key_cannot_be_blank(blank: str) -> None:
    """A blank key silently pooled every such caller into one "" bucket."""

    with pytest.raises(ValueError, match="non-empty"):
        ConversationIdentity("tenant", "principal", blank, "user")
    with pytest.raises(ValueError, match="non-empty"):
        ConversationIdentity(blank, "principal", "session", "user")
    with pytest.raises(ValueError, match="non-empty"):
        AgentRunIdentity("tenant", "principal", "session", blank, "user")


def test_a_blank_principal_stays_allowed_as_a_lookup_key() -> None:
    """``principal_id`` is audit provenance, excluded from comparison.

    Callers build an identity with it blank purely to look a session up, which
    works precisely because it is not part of equality.
    """

    keyed = ConversationIdentity("tenant", "", "session", "user")

    assert keyed == ConversationIdentity("tenant", "whoever", "session", "user")


def test_frozen_metadata_survives_deep_freeze_by_type_not_by_name() -> None:
    """Recognising it by ``__name__``/``__module__`` broke on any move.

    A rename, a relocation, or a subclass silently resumed re-wrapping it in a
    FrozenDict and losing its behaviour.
    """

    class Holder(StrictModel):
        metadata: object

    original = FrozenMetadata({"a": 1})

    assert Holder(metadata=original).metadata is original


def test_an_evidence_marker_survives_optimized_bytecode() -> None:
    """The marker was guarded by ``assert``, which -O erases."""

    reference = AgentEvidenceReference(tool="vector_search", chunk_id="c1", document_id="d1")

    assert reference.marker
    assert reference.marker == reference.canonical_marker
