"""Regression checks for the supported local development topology."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_COMPOSE = ROOT / "deploy/compose/docker-compose.dev.yml"
DEV_UP = ROOT / "scripts/deployment/dev_up.sh"
DEV_DOWN = ROOT / "scripts/deployment/dev_down.sh"


def test_dev_api_uses_the_temporal_development_network() -> None:
    compose = DEV_COMPOSE.read_text(encoding="utf-8")

    assert "dockerfile: deploy/docker/Dockerfile.api" in compose
    assert "HARBORRAG_TEMPORAL_TARGET: temporal:7233" in compose
    assert 'HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE: "true"' in compose
    assert "harborrag-data-network" in compose
    assert "external: true" in compose
    assert "../../.env" not in compose


def test_dev_api_binds_to_loopback_by_default() -> None:
    compose = DEV_COMPOSE.read_text(encoding="utf-8")

    assert "HARBORRAG_API_BIND_ADDRESS:-127.0.0.1" in compose
    assert "HARBORRAG_API_PORT:-8000" in compose


def test_data_and_temporal_ports_bind_to_loopback_by_default() -> None:
    database = (ROOT / "deploy/compose/docker-compose.database.yml").read_text(encoding="utf-8")
    temporal = (ROOT / "deploy/compose/docker-compose.temporal.yml").read_text(encoding="utf-8")

    assert database.count("HARBORRAG_DATA_BIND_ADDRESS:-127.0.0.1") == 6
    assert temporal.count("HARBORRAG_TEMPORAL_BIND_ADDRESS:-127.0.0.1") == 2


def test_dev_up_orchestrates_data_temporal_worker_and_api() -> None:
    script = DEV_UP.read_text(encoding="utf-8")

    assert ".env.example" not in script
    assert "env-example/.env.database.example" in script
    assert "env-example/.env.temporal.example" in script
    assert "scripts/deployment/database_up.sh" in script
    assert "scripts/deployment/temporal_up.sh" in script
    assert "TEMPORAL_START_WORKER=" in script
    assert "chmod 600" in script
    assert "docker-compose.dev.yml" in script


def test_dev_down_stops_the_composed_projects_in_reverse_order() -> None:
    script = DEV_DOWN.read_text(encoding="utf-8")

    api_position = script.index("docker-compose.dev.yml")
    temporal_position = script.index("docker-compose.temporal.yml")
    database_position = script.index("docker-compose.database.yml")

    assert api_position < temporal_position < database_position
