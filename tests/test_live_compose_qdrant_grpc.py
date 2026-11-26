from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.unit


def _environment(compose_file: str, service: str) -> dict[str, Any]:
    compose = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
    environment: dict[str, Any] = compose["services"][service]["environment"]
    return environment


@pytest.mark.parametrize(
    ("compose_file", "service"),
    [
        ("deploy/compose/docker-compose.yml", "api"),
        ("deploy/compose/docker-compose.temporal.yml", "temporal-worker"),
    ],
)
def test_compose_passes_the_qdrant_grpc_settings_beside_the_url(
    compose_file: str, service: str
) -> None:
    environment = _environment(compose_file, service)

    assert environment["HARBORRAG_QDRANT_URL"] == "${HARBORRAG_QDRANT_URL:-http://qdrant:6333}"
    assert environment["HARBORRAG_QDRANT_GRPC_PORT"] == "${HARBORRAG_QDRANT_GRPC_PORT:-}"
    assert environment["HARBORRAG_QDRANT_PREFER_GRPC"] == "${HARBORRAG_QDRANT_PREFER_GRPC:-true}"
