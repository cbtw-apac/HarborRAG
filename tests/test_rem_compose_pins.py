"""The data stack's Redis and Postgres images are pinned to one release."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DATABASE_COMPOSE = ROOT / "deploy/compose/docker-compose.database.yml"


@pytest.mark.parametrize(
    ("service", "exact_release"),
    [
        ("redis", r"redis:\d+\.\d+\.\d+-alpine"),
        ("postgres", r"postgres:15\.\d+-alpine"),
    ],
)
def test_a_data_service_image_names_an_exact_release(service: str, exact_release: str) -> None:
    services = yaml.safe_load(DATABASE_COMPOSE.read_text(encoding="utf-8"))["services"]

    assert re.fullmatch(exact_release, services[service]["image"])
