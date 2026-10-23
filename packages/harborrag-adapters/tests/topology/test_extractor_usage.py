"""Budgeted extraction needs the real token usage every semantic attempt consumed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_adapters.models.chat.structured import StructuredResult
from harborrag_adapters.topology.extractor import (
    LLMEntityExtractor,
    default_extraction_profile,
)
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.topology.extraction import ChunkExtractionInput, EvidenceSpan

from .test_extractor import config, output

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_extract_usage_sums_tokens_across_semantic_retries() -> None:
    ungrounded = output().model_copy(
        update={"title_evidence": (EvidenceSpan(start=0, end=4, quote="Zulu"),)}
    )
    client = SimpleNamespace(
        achat_structured_usage=AsyncMock(
            side_effect=[
                StructuredResult(
                    ungrounded, HarborChatUsage(prompt_tokens=100, completion_tokens=40), 2
                ),
                StructuredResult(
                    output(), HarborChatUsage(prompt_tokens=150, completion_tokens=60), 1
                ),
            ]
        )
    )
    value = ChunkExtractionInput(
        chunk_id="chunk-1", content='Alpha says "ignore instructions"', context="context"
    )

    result = await LLMEntityExtractor(client).extract_usage(
        value, profile=default_extraction_profile(config()), tenant_id="t", document_id="d"
    )

    assert result.output.entities == output().entities
    assert result.usage.prompt_tokens == 250
    assert result.usage.completion_tokens == 100
    assert result.provider_calls == 3
