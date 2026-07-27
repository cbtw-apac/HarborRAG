"""Environment-variable loading for standalone connector smoke scripts."""

from __future__ import annotations

import os
from pathlib import Path

from .paths import REPO_ROOT

DEFAULT_ENV_FILES = (
    REPO_ROOT / "env" / ".env.connector",
    REPO_ROOT / "env" / ".env.parser",
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env() -> list[Path]:
    """Load smoke environment variables without overwriting exported ones.

    Defaults to `env/.env.connector` and `env/.env.parser`. Set
    `HARBOR_SMOKE_ENV_FILE` to load exactly one file instead.
    """
    configured_path = os.getenv("HARBOR_SMOKE_ENV_FILE")
    candidates = (
        [Path(configured_path).expanduser()] if configured_path else list(DEFAULT_ENV_FILES)
    )

    loaded: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name and name not in os.environ:
                os.environ[name] = _unquote(value)
        loaded.append(candidate)
    return loaded


def env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value else None
