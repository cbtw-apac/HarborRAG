from __future__ import annotations

import asyncio
import math

from bootstrap import load_env, safe_error, set_env_overrides
from config import SmokeNotConfigured, embed_config

from harborrag_adapters.models.embed import HarborEmbedClient

EMBED_PROVIDER: str | None = None  # e.g. "openai"
EMBED_MODEL: str | None = None  # e.g. "openai/text-embedding-3-small"
EMBED_API_KEY: str | None = None
EMBED_API_BASE: str | None = None
EMBED_SPACE: str | None = None
EMBED_EXPECTED_DIMENSIONS: int | None = None

BATCH: list[str] = [
    "HarborRAG embedding smoke test.",
    "Neural operators learn mappings between function spaces.",
]


async def _run() -> None:
    async with HarborEmbedClient.from_config(embed_config()) as client:
        response = await client.aembed(BATCH)
    if len(response.embeddings) != len(BATCH) or not response.dimensions:
        raise AssertionError("provider returned an invalid embedding batch")
    for embedding in response.embeddings:
        if len(embedding.value) != response.dimensions:
            raise AssertionError("embedding dimensions are inconsistent")
        if not all(math.isfinite(value) for value in embedding.value):
            raise AssertionError("embedding contains a non-finite value")
    print(
        "[models/embed] passed "
        f"provider_model={response.provider_model!r} dimensions={response.dimensions}"
    )
    print(f"[models/embed] embeddings: {[e.value[:5] for e in response.embeddings]!r}")


def main() -> int:
    load_env()
    set_env_overrides(
        {
            "HARBOR_EMBED_PROVIDER": EMBED_PROVIDER,
            "HARBOR_EMBED_MODEL": EMBED_MODEL,
            "HARBOR_EMBED_API_KEY": EMBED_API_KEY,
            "HARBOR_EMBED_API_BASE": EMBED_API_BASE,
            "HARBOR_EMBED_SPACE": EMBED_SPACE,
            "HARBOR_EMBED_EXPECTED_DIMENSIONS": EMBED_EXPECTED_DIMENSIONS,
        }
    )
    try:
        asyncio.run(_run())
    except SmokeNotConfigured as exc:
        print(f"[models/embed] not configured: {safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[models/embed] failed: {safe_error(exc)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
