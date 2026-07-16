from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-smoke",
        action="store_true",
        default=False,
        help="Run live Harbor model-client smoke tests using credentials from .env.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-smoke"):
        return
    marker = pytest.mark.skip(
        reason="live smoke tests require --run-smoke and provider credentials in .env"
    )
    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(marker)


@pytest.fixture(scope="session", autouse=True)
def load_smoke_environment(pytestconfig: pytest.Config) -> None:
    """Load the requested dotenv file only when live smoke tests are enabled."""

    if not pytestconfig.getoption("--run-smoke"):
        return

    configured_path = os.getenv("HARBOR_SMOKE_ENV_FILE")
    repository_root = Path(__file__).resolve().parents[5]
    env_path = Path(configured_path).expanduser() if configured_path else repository_root / ".env"
    if not env_path.is_file():
        return
    load_dotenv(env_path, override=False)
