from __future__ import annotations

import asyncio
import math

from bootstrap import load_env, safe_error
from config import SmokeNotConfigured, embed_config
from harborrag_adapters.models.embed import HarborEmbedClient


async def _run() -> None:
    async with HarborEmbedClient.from_config(embed_config()) as client:
        response = await client.aembed(
            [
                "HarborRAG embedding smoke test.",
                "Neural operators learn mappings between function spaces.",
            ]
        )
    if len(response.embeddings) != 2 or not response.dimensions:
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
