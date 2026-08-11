from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-core" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)

from harborrag_core.security.redaction import redact_secrets  # noqa: E402


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env() -> Path:
    """Load a smoke dotenv file without overriding explicitly exported values."""

    configured_path = os.getenv("HARBOR_SMOKE_ENV_FILE")
    env_path = Path(configured_path).expanduser() if configured_path else REPO_ROOT / ".env"
    if not env_path.is_file():
        return env_path
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = _unquote(value)
    return env_path


def set_env_overrides(values: Mapping[str, Any]) -> None:
    """Apply per-file quick-config constants as env vars, skipping unset (None) values.

    Lets each smoke script expose plain top-of-file constants (e.g. EMBED_MODEL)
    as the easiest way to configure a run, without editing a dotenv file.
    """

    for name, value in values.items():
        if value is None:
            continue
        os.environ[name] = str(value)


def safe_error(error: BaseException) -> str:
    """Return a redacted one-line failure without credentials or response bodies."""

    detail = redact_secrets(str(error)).replace("\r", " ").replace("\n", " ")
    return f"{type(error).__name__}: {detail[:500]}"
