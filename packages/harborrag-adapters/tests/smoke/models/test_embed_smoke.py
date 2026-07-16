from __future__ import annotations

import math

import pytest
from harborrag_adapters.models.embed import HarborEmbedClient

from ._config import embed_config


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_real_embedding_batch_from_dotenv() -> None:
    async with HarborEmbedClient(embed_config()) as client:
        response = await client.aembed(
            [
                "HarborRAG embedding smoke test.",
                "Neural operators learn mappings between function spaces.",
            ]
        )

    assert len(response.embeddings) == 2
    assert response.dimensions > 0
    assert response.request_id
    assert response.provider_model
    for vector in response.vectors:
        assert isinstance(vector, tuple)
        assert len(vector) == response.dimensions
        assert all(math.isfinite(value) for value in vector)
