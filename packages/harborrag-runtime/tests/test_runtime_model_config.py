from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_core.models.embed import EmbeddingPurpose

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_repository_embedding_config_supports_index_and_query_purposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBOR_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("HARBOR_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("HARBOR_EMBED_API_KEY", "test-secret")

    config = HarborEmbedClientConfig.from_file(REPO_ROOT / "config" / "models.yaml")
    deployment = config.models[config.default_model].deployments[0]

    assert deployment.capabilities.purpose is True
    assert deployment.capabilities.supported_purposes == {
        EmbeddingPurpose.QUERY,
        EmbeddingPurpose.DOCUMENT,
    }
