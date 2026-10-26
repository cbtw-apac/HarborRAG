"""Hashed, tenant-bound bearer keys for an internal MCP resource server."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from harborrag_core.ports.api_keys import AuthStoreUnavailable

if TYPE_CHECKING:
    from fastmcp.server.auth import TokenVerifier

    from harborrag_engine.security import ApiKeyVerificationService

logger = logging.getLogger("harborrag.mcp.server.api_keys")


class ReaderKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str = Field(min_length=1, max_length=128)
    principal_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=128)
    secret_hash_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    expires_at: datetime | None = None
    revoked: bool = False

    @field_validator("tenant_id")
    @classmethod
    def tenant_must_be_concrete(cls, value: str) -> str:
        # Grants are stripped when read, so " * " would widen to every tenant too.
        if value != value.strip():
            raise ValueError("MCP key tenant_id must not contain surrounding whitespace")
        if value == "*":
            raise ValueError("MCP key tenant_id must name one tenant, not the '*' wildcard")
        return value


class KeyFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    keys: tuple[ReaderKey, ...] = Field(min_length=1, max_length=1000)


class _KeyFileEnvelope(BaseModel):
    """The file-level shape, with each entry left raw so one bad entry is skippable.

    No ``min_length``: emptying the list on a running server revokes every key.
    Startup still validates ``KeyFile``, so an empty file cannot start serving.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    keys: tuple[dict[str, object], ...] = Field(max_length=1000)


@dataclass(frozen=True, slots=True)
class _ActiveKey:
    key: ReaderKey
    expected_digest: str


# A reload failure or a denied key is logged at most this often: a missing file is
# re-checked, and an expired key re-presented, on every request.
_LOG_INTERVAL_SECONDS = 60.0
# A file written this recently can be rewritten within the same mtime tick at the same
# size, so its cache entry is not trusted and the next request reads it again.
_RACY_MTIME_SECONDS = 2.0


def _key_problem(key: ReaderKey) -> str | None:
    value = os.environ.get(key.secret_hash_env, "")
    if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        return f"MCP key hash environment variable {key.secret_hash_env} is invalid"
    if key.expires_at is not None and key.expires_at.tzinfo is None:
        return "MCP key expiration must include a timezone"
    return None


def _load_keys(path: Path, *, strict: bool = True) -> tuple[_ActiveKey, ...]:
    """Parse the key file; ``strict`` raises on any bad entry, otherwise skips it.

    Startup is strict so a misconfigured file never starts serving. A reload is
    lenient: a process cannot pick up a hash variable exported after it started,
    so a newly appended key would otherwise lock out every existing key.
    File-level errors (unreadable, not YAML, wrong envelope) still raise.
    """

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    envelope = _KeyFileEnvelope.model_validate(data)
    if strict:
        key_file = KeyFile.model_validate(data)
        if len({key.key_id for key in key_file.keys}) != len(key_file.keys):
            raise ValueError("MCP key IDs must be unique")
        for key in key_file.keys:
            if (problem := _key_problem(key)) is not None:
                raise ValueError(problem)
        return tuple(
            _ActiveKey(key, os.environ[key.secret_hash_env].lower()) for key in key_file.keys
        )
    parsed: list[ReaderKey] = []
    for index, entry in enumerate(envelope.keys):
        try:
            parsed.append(ReaderKey.model_validate(entry))
        except ValidationError as exc:
            label = entry.get("key_id")
            fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
            logger.warning(
                "Skipping MCP key %s: invalid %s",
                label if isinstance(label, str) else f"#{index}",
                ", ".join(fields) or "entry",
            )
    counts = Counter(key.key_id for key in parsed)
    active: list[_ActiveKey] = []
    for key in parsed:
        # Drop every copy: keeping one could let a live duplicate shadow a revoked one.
        problem = "duplicate key_id" if counts[key.key_id] > 1 else _key_problem(key)
        if problem is not None:
            logger.warning("Skipping MCP key %s: %s", key.key_id, problem)
            continue
        active.append(_ActiveKey(key, os.environ[key.secret_hash_env].lower()))
    return tuple(active)


def _signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    # ctime as well as mtime: a chmod that makes an unreadable file readable again
    # changes only ctime, and must still trigger a reload.
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def _cacheable_signature(stat: os.stat_result) -> tuple[int, int, int, int] | None:
    """The signature to cache a parse under, or ``None`` if it is too fresh to trust."""

    if time.time() - stat.st_mtime < _RACY_MTIME_SECONDS:
        return None
    return _signature(stat)


class _KeyFileCache:
    """The parsed key set, re-read only when the file's stat signature changes.

    A reload skips bad entries but fails closed at file level: a file that cannot
    be read or parsed, or is missing, denies every key until it parses again.
    Keeping the previous set instead would leave a key live that the broken edit
    was revoking. Failures log at ERROR, throttled.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # Separate from ``_lock``, which a reload holds across file I/O: denial
        # logging runs on the event loop and must not wait for it.
        self._log_lock = threading.Lock()
        self._keys = _load_keys(path)  # Fail startup on bad configuration.
        self._signature = _cacheable_signature(path.stat())
        self._logged_at: dict[str, float] = {}

    def current(self) -> tuple[_ActiveKey, ...]:
        """The keys to verify against; blocking, so callers run it off the loop."""

        with self._lock:
            try:
                stat = self._path.stat()
            except OSError as exc:
                self._reload_failed(exc)
                self._signature = None
                return self._keys
            if _signature(stat) == self._signature:
                return self._keys
            try:
                self._keys = _load_keys(self._path, strict=False)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                self._reload_failed(exc)
            # Cached after a failure too, so a broken file is not re-parsed on every
            # request; every key stays denied until the file changes and parses.
            self._signature = _cacheable_signature(stat)
            return self._keys

    def _reload_failed(self, exc: Exception) -> None:
        self._keys = ()
        self.log_throttled(
            "reload",
            logging.ERROR,
            "MCP key file %s failed to load (%s); denying every key until it parses",
            self._path,
            type(exc).__name__,
        )

    def log_throttled(self, topic: str, level: int, message: str, *args: object) -> None:
        """Log ``message`` at most once per interval for each ``topic``."""

        now = time.monotonic()
        with self._log_lock:
            last = self._logged_at.get(topic)
            if last is not None and now - last < _LOG_INTERVAL_SECONDS:
                return
            self._logged_at[topic] = now
        logger.log(level, message, *args)


def create_api_key_verifier(path: str | Path) -> TokenVerifier:
    """Verify each request against current revocation/expiry state on disk."""
    from fastmcp.server.auth import AccessToken, TokenVerifier

    cache = _KeyFileCache(Path(path))

    class ApiKeyVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            if len(token) < 32 or len(token) > 4096:
                return None
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            for record in await asyncio.to_thread(cache.current):
                if not hmac.compare_digest(digest, record.expected_digest):
                    continue
                key = record.key
                if key.revoked or (key.expires_at and key.expires_at <= datetime.now(UTC)):
                    cache.log_throttled(
                        f"denied:{key.key_id}",
                        logging.WARNING,
                        "MCP key %s is revoked or expired; denying",
                        key.key_id,
                    )
                    return None
                return AccessToken(
                    token="",
                    client_id=key.key_id,
                    subject=key.principal_id,
                    scopes=["mcp:read"],
                    claims={
                        "sub": key.principal_id,
                        "role": "reader",
                        "tenants": [key.tenant_id],
                    },
                )
            return None

    return ApiKeyVerifier(required_scopes=["mcp:read"])


def create_postgres_api_key_verifier(service: ApiKeyVerificationService) -> TokenVerifier:
    """Verify CLI-issued keys against the control-plane table on every request.

    The claims keep the shape the rest of this server already authorises on:
    ``role`` and a one-element ``tenants`` grant taken from the stored row, so a
    caller can never widen its own tenant. An unreachable store denies the
    request; the outage is logged by type so it is not mistaken for a bad key.
    """

    from fastmcp.server.auth import AccessToken, TokenVerifier

    class PostgresApiKeyVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            try:
                key = await service.verify_key(token)
            except AuthStoreUnavailable as exc:
                logger.warning(
                    "MCP key store lookup failed: %s", type(exc.__cause__ or exc).__name__
                )
                return None
            except Exception as exc:  # noqa: BLE001 - the auth boundary never leaks a 500
                logger.error("MCP key verification failed: %s", type(exc).__name__)
                return None
            if key is None:
                return None
            return AccessToken(
                token="",  # never echo the presented key back into the request state
                client_id=f"mcp-key:{key.key_id}",
                subject=key.owner,
                scopes=["mcp:read"],
                expires_at=int(key.expires_at.timestamp()),
                claims={
                    "sub": key.owner,
                    "role": "reader",
                    "tenants": [key.tenant_id],
                    "key_id": key.key_id,
                    "auth_method": "api_key",
                },
            )

    return PostgresApiKeyVerifier(required_scopes=["mcp:read"])


def create_composite_verifier(verifiers: Sequence[TokenVerifier]) -> TokenVerifier:
    """Accept a bearer that any one of ``verifiers`` accepts; the first match wins.

    ``api_key`` mode pairs the hashed reader keys with the loopback owner token,
    so the status UI keeps its configuration editor while every other caller
    presents a key. Each verifier applies its own claims; nothing is merged.
    """

    from fastmcp.server.auth import AccessToken, TokenVerifier

    if not verifiers:
        raise ValueError("composite verifier needs at least one verifier")

    class CompositeVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            for verifier in verifiers:
                access = await verifier.verify_token(token)
                if access is not None:
                    return access
            return None

    return CompositeVerifier(required_scopes=["mcp:read"])
