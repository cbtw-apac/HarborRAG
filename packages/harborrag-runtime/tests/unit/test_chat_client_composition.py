"""The chat client is composed with telemetry, like every other model family.

Chat was the one family built with no sink at all, so its calls were invisible
to OpenTelemetry and Langfuse while the embed client's were not. These tests
pin the dispatcher onto the client and pin its ownership, so whatever the
client is handed is also disposed by ``aclose``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harborrag_adapters.models.runtime import ResourceOwnership
from harborrag_runtime.chat import composition as chat_composition
from harborrag_runtime.config.settings import RuntimeSettings


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, object]:
    """Stub the config, the factory, and the dispatcher builder."""

    config = SimpleNamespace(observability=SimpleNamespace(privacy=None))
    telemetry = object()
    factory = Mock(return_value=object())
    monkeypatch.setattr(
        chat_composition.HarborChatClientConfig,
        "from_file",
        Mock(return_value=config),
    )
    monkeypatch.setattr(
        chat_composition,
        "build_model_telemetry",
        Mock(return_value=telemetry),
    )
    monkeypatch.setattr(chat_composition.ChatClientFactory, "create_async", factory)
    return factory, chat_composition.build_model_telemetry, telemetry  # type: ignore[return-value]


def test_the_chat_client_is_built_with_owned_telemetry(
    composed: tuple[Mock, Mock, object],
) -> None:
    factory, build_telemetry, telemetry = composed

    client = chat_composition.build_chat_client(RuntimeSettings(langfuse_enabled=True))

    assert client is factory.return_value
    dependencies = factory.call_args.args[1]
    assert dependencies.telemetry is telemetry
    # OWNED so the client's aclose disposes it; nothing else holds a reference.
    assert dependencies.telemetry_ownership is ResourceOwnership.OWNED
    assert build_telemetry.call_args.kwargs == {"langfuse_enabled": True}


def test_langfuse_stays_off_unless_the_deployment_enabled_it(
    composed: tuple[Mock, Mock, object],
) -> None:
    _, build_telemetry, _ = composed

    chat_composition.build_chat_client(RuntimeSettings(langfuse_enabled=False))

    assert build_telemetry.call_args.kwargs == {"langfuse_enabled": False}
