"""The data stack's Redis image is pinned to one release."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATABASE_COMPOSE = ROOT / "deploy/compose/docker-compose.database.yml"
_EXACT_RELEASE = re.compile(r"redis:\d+\.\d+\.\d+-alpine")


def test_the_redis_image_names_an_exact_release() -> None:
    services = yaml.safe_load(DATABASE_COMPOSE.read_text(encoding="utf-8"))["services"]

    assert _EXACT_RELEASE.fullmatch(services["redis"]["image"])
