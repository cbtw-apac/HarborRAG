from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[5]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-core" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)

from harborrag_core.schemas.storage import HealthStatus, RepositoryHealth  # noqa: E402
from harborrag_core.security.redaction import redact_secrets  # noqa: E402


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env() -> Path:
    """Load a smoke dotenv file without replacing exported variables."""

    configured_path = os.getenv("HARBOR_SMOKE_ENV_FILE")
    env_path = Path(configured_path).expanduser() if configured_path else REPO_ROOT / ".env"
    if not env_path.is_absolute():
        env_path = REPO_ROOT / env_path
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


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def env_int(name: str, default: int) -> int:
    return int(env(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    fallback = "true" if default else "false"
    value = env(name, fallback).lower()
    if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(f"{name} must be a boolean value")
    return value in {"1", "true", "yes", "on"}


def dependency_available(module: str, install_hint: str) -> bool:
    if find_spec(module) is not None:
        return True
    print(f"Unavailable: Python module {module!r} is not installed. {install_hint}")
    return False


def require_healthy(health: RepositoryHealth) -> None:
    if health.status is not HealthStatus.HEALTHY:
        raise RuntimeError(
            f"{health.backend} health check returned {health.status.value} "
            f"({health.details.get('error_type', 'no error type')})"
        )


def probe_suffix() -> str:
    return uuid4().hex[:12]


def safe_error(error: BaseException) -> str:
    detail = redact_secrets(str(error)).replace("\r", " ").replace("\n", " ")
    return f"{type(error).__name__}: {detail[:500]}"
