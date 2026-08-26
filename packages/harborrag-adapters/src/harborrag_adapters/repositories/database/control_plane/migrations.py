"""Programmatic Alembic runner (ST5).

Migrations run at composition time — no CLI, no cwd dependence (the
qdrant-loader 1.0.3 alembic-path lesson). env.py drives the async engine via
asyncio.run, so when run_migrations is itself called from inside a running
event loop it hops to a worker thread first.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic import command
from alembic.config import Config

_SCRIPT_LOCATION = Path(__file__).parent / "alembic"


def _build_config(dsn: str) -> Config:
    """Alembic config pointing at the packaged migration scripts."""
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    # Alembic stores main options in a ConfigParser. Percent-encoded
    # credentials must therefore escape percent signs at the configuration
    # boundary; reading the option in env.py restores the original DSN.
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    return config


def run_migrations(dsn: str) -> None:
    """Upgrade the control-plane schema to head; idempotent.

    Safe to call from sync code and from within a running event loop
    (lifespan startup): the loop case runs Alembic on a worker thread so
    env.py's asyncio.run gets a clean thread.
    """
    config = _build_config(dsn)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        command.upgrade(config, "head")
        return
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(command.upgrade, config, "head").result()
