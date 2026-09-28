"""A description run settles at what the call actually cost, not its ceiling."""

from decimal import Decimal

import pytest

from harborrag_adapters.models.chat.structured import StructuredResult, _accumulated_cost
from harborrag_adapters.models.runtime.provider import DeploymentPricing
from harborrag_adapters.topology.descriptions import (
    BoundedDescriptionOutput,
    LLMDescriptionGenerator,
)
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.topology.derived import DescriptionPacket
from harborrag_core.topology.extraction import ExtractionProfile

PACKETS = (DescriptionPacket(packet_id="p0", text="Harbor is a service.", chunk_ids=("p0",)),)
OUTPUT = BoundedDescriptionOutput(
    description="Harbor is a service.", cited_packet_ids=("p0",), complete=True
)
USAGE = HarborChatUsage(prompt_tokens=3000, completion_tokens=500, total_tokens=3500)


class Client:
    def __init__(self, cost: float | None):
        self.cost = cost

    async def achat_structured_usage(self, *, request, response_model, max_repair_attempts):
        return StructuredResult(OUTPUT, USAGE, 1, self.cost)

    async def achat_structured(self, *, request, response_model, max_repair_attempts):
        return OUTPUT


def generator(client: Client, pricing: DeploymentPricing | None) -> LLMDescriptionGenerator:
    return LLMDescriptionGenerator(
        client,
        ExtractionProfile(model="test", deployment_revision="r1", prompt_digest="p1"),
        "tenant",
        "doc",
        pricing=pricing,
    )


@pytest.mark.asyncio
async def test_the_providers_own_price_wins_when_it_is_known() -> None:
    rates = DeploymentPricing(
        input_usd_per_million=Decimal("10"), output_usd_per_million=Decimal("30")
    )
    run = await generator(Client(0.0021), rates).generate_usage(PACKETS)
    assert run.cost_usd == Decimal("0.0021")
    assert run.usage == USAGE and run.provider_calls == 1


@pytest.mark.asyncio
async def test_configured_rates_price_an_unpriced_response() -> None:
    rates = DeploymentPricing(
        input_usd_per_million=Decimal("10"), output_usd_per_million=Decimal("30")
    )
    run = await generator(Client(None), rates).generate_usage(PACKETS)
    # 3000 in at $10/M + 500 out at $30/M
    assert run.cost_usd == Decimal("0.045")


@pytest.mark.asyncio
async def test_no_price_anywhere_stays_unknown_rather_than_guessed() -> None:
    run = await generator(Client(None), None).generate_usage(PACKETS)
    assert run.cost_usd is None


def _response(cost: float | None) -> HarborChatResponse:
    return HarborChatResponse(
        id="r",
        logical_model="m",
        provider="openai",
        provider_model="m",
        deployment="d",
        message=HarborChatMessage.assistant("{}"),
        finish_reason="stop",
        usage=USAGE,
        estimated_cost_usd=cost,
    )


def test_repair_rounds_sum_their_prices_and_one_unpriced_call_unprices_the_request() -> None:
    total = _accumulated_cost(None, _response(0.001), 0)
    total = _accumulated_cost(total, _response(0.002), 1)
    assert total == pytest.approx(0.003)
    assert _accumulated_cost(total, _response(None), 2) is None
    # An unpriced first call is never rescued by a priced repair.
    assert _accumulated_cost(_accumulated_cost(None, _response(None), 0), _response(0.5), 1) is None


def test_output_tokens_grow_with_the_word_budget_and_match_the_reservation() -> None:
    from harborrag_adapters.topology.descriptions import description_output_tokens

    # A navigation card keeps the configured base; a dossier gets room for its
    # prose, its facets and a reasoning model's thinking tokens.
    assert description_output_tokens(60, 1024) == 1024
    assert description_output_tokens(61, 1024) == 1207
    assert description_output_tokens(512, 1024) == 2560
    assert description_output_tokens(512, 4000) == 4000
    assert description_output_tokens(5000, 1024) == 8192


@pytest.mark.asyncio
async def test_the_request_asks_for_the_tokens_its_budget_implies() -> None:
    seen = {}

    class Recording(Client):
        async def achat_structured_usage(self, *, request, response_model, max_repair_attempts):
            seen["max_tokens"] = request.max_tokens
            return await super().achat_structured_usage(
                request=request,
                response_model=response_model,
                max_repair_attempts=max_repair_attempts,
            )

    await generator(Recording(None), None).generate_usage(PACKETS, max_words=512)
    assert seen["max_tokens"] == 2560
