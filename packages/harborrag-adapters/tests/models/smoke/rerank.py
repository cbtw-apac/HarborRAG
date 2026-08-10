from __future__ import annotations

import asyncio
import math

from bootstrap import load_env, safe_error, set_env_overrides
from config import SmokeNotConfigured, rerank_config

from harborrag_adapters.models.rerank import HarborRerankingClient

RERANK_PROVIDER: str | None = None  # e.g. "cohere"
RERANK_MODEL: str | None = None  # e.g. "cohere/rerank-english-v3.0"
RERANK_API_KEY: str | None = None
RERANK_API_BASE: str | None = None

QUERY = "Which documents discuss operator learning?"
DOCUMENTS = [
    "Fourier neural operators apply learned spectral transformations.",
    "Bananas are a fruit commonly grown in tropical climates.",
    "DeepONet learns operators with branch and trunk networks.",
]
TOP_N = 2


async def _run() -> None:
    async with HarborRerankingClient.from_config(rerank_config()) as client:
        response = await client.arerank(
            QUERY,
            DOCUMENTS,
            top_n=TOP_N,
            return_documents=False,
        )
    if len(response.results) != TOP_N:
        raise AssertionError("provider returned the wrong reranking result count")
    indexes = {result.index for result in response.results}
    if len(indexes) != TOP_N or not all(0 <= index < len(DOCUMENTS) for index in indexes):
        raise AssertionError("provider returned invalid reranking indexes")
    if not all(math.isfinite(result.relevance_score) for result in response.results):
        raise AssertionError("provider returned a non-finite reranking score")
    print(
        "[models/rerank] passed "
        f"provider_model={response.provider_model!r} results={len(response.results)}"
    )


def main() -> int:
    load_env()
    set_env_overrides(
        {
            "HARBOR_SMOKE_RERANK_PROVIDER": RERANK_PROVIDER,
            "HARBOR_SMOKE_RERANK_MODEL": RERANK_MODEL,
            "HARBOR_SMOKE_RERANK_API_KEY": RERANK_API_KEY,
            "HARBOR_SMOKE_RERANK_API_BASE": RERANK_API_BASE,
        }
    )
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
