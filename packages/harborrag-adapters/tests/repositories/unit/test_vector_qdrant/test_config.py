from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.repositories.vector.qdrant.config import (
    QdrantDeployment,
    QdrantVectorConfig,
)


def test_remote_config_requires_url() -> None:
    with pytest.raises(ValidationError, match="requires url"):
        QdrantVectorConfig(deployment=QdrantDeployment.REMOTE)


def test_remote_config_rejects_path() -> None:
    with pytest.raises(ValidationError, match="does not accept path"):
        QdrantVectorConfig(url="http://qdrant.invalid", path="/data")


def test_embedded_config_rejects_url() -> None:
    with pytest.raises(ValidationError, match="does not accept url"):
        QdrantVectorConfig(deployment=QdrantDeployment.EMBEDDED, url="http://qdrant.invalid")


def test_embedded_config_rejects_api_key() -> None:
    with pytest.raises(ValidationError, match="does not accept api_key"):
        QdrantVectorConfig(deployment=QdrantDeployment.EMBEDDED, api_key="secret")


def test_embedded_config_with_no_url_or_api_key_succeeds() -> None:
    config = QdrantVectorConfig(deployment=QdrantDeployment.EMBEDDED)
    assert config.deployment == QdrantDeployment.EMBEDDED
    assert config.url is None
