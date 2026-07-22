from __future__ import annotations

import asyncio
import math

from bootstrap import load_env, safe_error
from config import SmokeNotConfigured, rerank_config

from harborrag_adapters.models.rerank import HarborRerankingClient

DOCUMENTS = [
    "Fourier neural operators apply learned spectral transformations.",
    "Bananas are a fruit commonly grown in tropical climates.",
    "DeepONet learns operators with branch and trunk networks.",
]


async def _run() -> None:
    async with HarborRerankingClient.from_config(rerank_config()) as client:
        response = await client.arerank(
            "Which documents discuss operator learning?",
            DOCUMENTS,
            top_n=2,
            return_documents=False,
        )
    if len(response.results) != 2:
        raise AssertionError("provider returned the wrong reranking result count")
    indexes = {result.index for result in response.results}
    if len(indexes) != 2 or not all(0 <= index < len(DOCUMENTS) for index in indexes):
        raise AssertionError("provider returned invalid reranking indexes")
    if not all(math.isfinite(result.relevance_score) for result in response.results):
        raise AssertionError("provider returned a non-finite reranking score")
    print(
        "[models/rerank] passed "
        f"provider_model={response.provider_model!r} results={len(response.results)}"
    )


def main() -> int:
    load_env()
    try:
        asyncio.run(_run())
    except SmokeNotConfigured as exc:
        print(f"[models/rerank] not configured: {safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[models/rerank] failed: {safe_error(exc)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
