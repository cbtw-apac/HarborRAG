from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-core" / "src",
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-engine" / "src",
    REPO_ROOT / "packages" / "harborrag-runtime" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)

from harborrag_core.schemas.storage import HealthStatus, RepositoryHealth  # noqa: E402
from harborrag_core.security.redaction import redact_secrets  # noqa: E402


class SmokeConfigurationError(ValueError):
    """Report invalid smoke configuration without exposing its value."""


class SmokeNotConfigured(SmokeConfigurationError):
    """Report that a required real smoke target is unavailable."""


def load_env_file(path: Path) -> bool:
    """Load one dotenv file without replacing exported variables."""

    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    if not resolved.is_file():
        return False
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = _unquote(value)
    return True


def load_env() -> Path:
    """Load a smoke dotenv file without replacing exported variables."""

    configured = os.getenv("HARBOR_SMOKE_ENV_FILE")
    path = Path(configured).expanduser() if configured else REPO_ROOT / ".env"
    if not path.is_absolute():
        path = REPO_ROOT / path
    load_env_file(path)
    return path


def env(name: str, default: str | None = None) -> str | None:
    """Read one non-empty environment variable or return its default."""

    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def env_bool(name: str, default: bool = False) -> bool:
    """Read one conventional boolean environment variable."""

    value = env(name)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SmokeConfigurationError(f"{name} must be a boolean value")


def env_int(name: str, default: int | None = None) -> int:
    """Read one positive integer environment variable."""

    value = env(name, str(default) if default is not None else None)
    if value is None:
        raise SmokeNotConfigured(f"missing required smoke variable: {name}")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise SmokeConfigurationError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise SmokeConfigurationError(f"{name} must be greater than zero")
    return parsed


def require_env(name: str) -> str:
    """Read a required non-placeholder smoke variable."""

    value = env(name)
    if value is None or "REPLACE_WITH_REAL" in value:
        raise SmokeNotConfigured(f"missing usable smoke variable: {name}")
    return value


def dependency_available(module: str, install_hint: str) -> bool:
    """Report whether one optional real-provider dependency is installed."""

    if find_spec(module) is not None:
        return True
    print(f"Unavailable: Python module {module!r} is not installed. {install_hint}")
    return False


def require_healthy(health: RepositoryHealth) -> None:
    """Require a connected repository to report healthy provider state."""

    if health.status is not HealthStatus.HEALTHY:
        error_type = health.details.get("error_type", "no error type")
        raise RuntimeError(
            f"{health.backend} health check returned {health.status.value} ({error_type})"
        )


def safe_error(error: BaseException) -> str:
    """Render a bounded, redacted, single-line error for terminal output."""

    detail = redact_secrets(str(error)).replace("\r", " ").replace("\n", " ")
    return f"{type(error).__name__}: {detail[:500]}"


def _unquote(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        return normalized[1:-1]
    return normalized
