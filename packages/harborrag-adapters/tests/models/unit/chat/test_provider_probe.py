"""Unit tests for LiteLLMProviderProbe (ML4-P2 test-connection)."""

from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.models.chat.probe import LiteLLMProviderProbe
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.provider import Provider
from harborrag_core.testing.fakes import FakeSecrets


class _RecordingCompletion:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"choices": [{"message": {"content": "pong"}}]}


@pytest.mark.asyncio
async def test_probe_resolves_the_secret_and_calls_completion_with_it() -> None:
    secrets = FakeSecrets()
    ref = await secrets.put("sk-real-value", tenant_id="ACME")
    provider = Provider(
        id="prov_1",
        tenant_id="ACME",
        name="OpenAI",
        family="chat",
        config={"model": "openai/gpt-4o-mini"},
        secret_ref=ref,
    )
    completion = _RecordingCompletion()
    probe = LiteLLMProviderProbe(secrets=secrets, acompletion=completion)

    result = await probe.probe(provider)

    assert result.ok is True
    assert result.latency_ms >= 0
    assert completion.calls[-1]["api_key"] == "sk-real-value"
    assert completion.calls[-1]["model"] == "openai/gpt-4o-mini"
    assert completion.calls[-1]["max_tokens"] == 1


@pytest.mark.asyncio
async def test_probe_without_a_secret_ref_passes_no_api_key() -> None:
    provider = Provider(
        id="prov_1",
        tenant_id="ACME",
        name="Local",
        family="chat",
        config={"model": "ollama/llama3"},
    )
    completion = _RecordingCompletion()
    probe = LiteLLMProviderProbe(secrets=FakeSecrets(), acompletion=completion)

    result = await probe.probe(provider)

    assert result.ok is True
    assert completion.calls[-1]["api_key"] is None


@pytest.mark.asyncio
async def test_probe_reports_failure_without_raising() -> None:
    provider = Provider(
        id="prov_1",
        tenant_id="ACME",
        name="OpenAI",
        family="chat",
        config={"model": "openai/gpt-4o-mini"},
    )
    completion = _RecordingCompletion(error=RuntimeError("connection refused"))
    probe = LiteLLMProviderProbe(secrets=FakeSecrets(), acompletion=completion)

    result = await probe.probe(provider)

    assert result.ok is False
    assert result.message == "Provider probe failed: RuntimeError"


@pytest.mark.asyncio
async def test_probe_failure_does_not_leak_the_raw_exception_message() -> None:
    """The raw exception can carry endpoint URLs or response bodies -- only the
    exception type name is public; the detail belongs in logs, not the API."""
    provider = Provider(
        id="prov_1",
        tenant_id="ACME",
        name="OpenAI",
        family="chat",
        config={"model": "openai/gpt-4o-mini"},
    )
    completion = _RecordingCompletion(
        error=RuntimeError("connection refused to https://internal.example/secret-path")
    )
    probe = LiteLLMProviderProbe(secrets=FakeSecrets(), acompletion=completion)

    result = await probe.probe(provider)

    assert "internal.example" not in result.message
    assert "secret-path" not in result.message


@pytest.mark.asyncio
async def test_probe_rejects_a_provider_with_no_model_configured_without_calling_out() -> None:
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat", config={})
    completion = _RecordingCompletion()
    probe = LiteLLMProviderProbe(secrets=FakeSecrets(), acompletion=completion)

    with pytest.raises(HarborValidationError):
        await probe.probe(provider)

    assert completion.calls == []


@pytest.mark.asyncio
async def test_probe_accepts_a_loopback_http_api_base() -> None:
    """A self-hosted model on the same host (e.g. Ollama) is a legitimate use case."""
    provider = Provider(
        id="prov_1",
        tenant_id="ACME",
        name="Local",
        family="chat",
        config={"model": "ollama/llama3", "api_base": "http://localhost:11434"},
    )
    completion = _RecordingCompletion()
    probe = LiteLLMProviderProbe(secrets=FakeSecrets(), acompletion=completion)

    result = await probe.probe(provider)

    assert result.ok is True
    assert completion.calls[-1]["api_base"] == "http://localhost:11434"


@pytest.mark.asyncio
async def test_probe_rejects_a_non_https_remote_api_base_without_calling_out() -> None:
    provider = Provider(
        id="prov_1",
        tenant_id="ACME",
        name="OpenAI",
        family="chat",
        config={"model": "openai/gpt-4o-mini", "api_base": "http://attacker.example/steal"},
    )
    completion = _RecordingCompletion()
    probe = LiteLLMProviderProbe(secrets=FakeSecrets(), acompletion=completion)

    with pytest.raises(HarborValidationError):
        await probe.probe(provider)

    assert completion.calls == []


@pytest.mark.asyncio
async def test_probe_rejects_an_api_base_with_embedded_credentials_without_calling_out() -> None:
    provider = Provider(
        id="prov_1",
        tenant_id="ACME",
        name="OpenAI",
        family="chat",
        config={
            "model": "openai/gpt-4o-mini",
            "api_base": "https://user:pass@attacker.example",
        },
    )
    completion = _RecordingCompletion()
    probe = LiteLLMProviderProbe(secrets=FakeSecrets(), acompletion=completion)

    with pytest.raises(HarborValidationError):
        await probe.probe(provider)

    assert completion.calls == []
