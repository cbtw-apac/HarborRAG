from __future__ import annotations

import math

import pytest
from harborrag_adapters.models.rerank import HarborRerankingClient
from harborrag_core.models.rerank import (
    HarborRerankDocument,
)

from ._config import rerank_config


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_real_reranking_request_from_dotenv() -> None:
    documents = [
        HarborRerankDocument.text(
            "Fourier neural operators apply learned spectral transformations.",
            document_id="neural-operator",
        ),
        HarborRerankDocument.text(
            "Bananas are a fruit commonly grown in tropical climates.",
            document_id="banana",
        ),
        HarborRerankDocument.text(
            "DeepONet learns operators with branch and trunk networks.",
            document_id="deeponet",
        ),
    ]
    async with HarborRerankingClient(rerank_config()) as client:
        response = await client.arerank(
            "Which documents discuss operator learning?",
            documents,
            top_n=2,
        )

    assert len(response.results) == 2
    assert len({result.index for result in response.results}) == 2
    assert all(0 <= result.index < len(documents) for result in response.results)
    assert all(math.isfinite(result.relevance_score) for result in response.results)
    assert response.provider_model
    assert response.request_id
