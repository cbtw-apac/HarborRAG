"""MCP reader keys: generate, parse, hash and compare.

Format::

    hrk_<env>_v1_<key_id>.<secret>
     |    |    |     |        `-- secrets.token_urlsafe(32): 43 URL-safe characters
     |    |    |     `-- secrets.token_hex(12): 24 hex characters, public
     |    |    `-- format version
     |    `-- dev | prod (RuntimeSettings.env)
     `-- "HarborRAG key": a fixed prefix secret scanners can match

Only the SHA-256 of the whole key is ever stored. That is sufficient because
the secret carries 256 bits of randomness; it is not a password scheme.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Literal, get_args

KeyEnvironment = Literal["dev", "prod"]
KEY_ENVIRONMENTS: tuple[str, ...] = get_args(KeyEnvironment)
KEY_ID_LENGTH = 24
_MAX_KEY_LENGTH = 120
_KEY_RE = re.compile(r"^hrk_(dev|prod)_v1_([0-9a-f]{24})\.([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class NewKey:
    key_id: str
    secret_hash: str = field(repr=False)
    raw_key: str = field(repr=False)  # shown once at creation, never stored


@dataclass(frozen=True, slots=True)
class ParsedKey:
    environment: KeyEnvironment
    key_id: str


def hash_key(raw_key: str) -> str:
    """SHA-256 hex digest of the complete key string."""

    return hashlib.sha256(raw_key.encode("ascii")).hexdigest()


def generate_key(environment: str) -> NewKey:
    if environment not in KEY_ENVIRONMENTS:
        raise ValueError(f"unsupported key environment: {environment!r}")
    key_id = secrets.token_hex(12)
    raw_key = f"hrk_{environment}_v1_{key_id}.{secrets.token_urlsafe(32)}"
    return NewKey(key_id=key_id, secret_hash=hash_key(raw_key), raw_key=raw_key)


def parse_key(raw_key: str) -> ParsedKey | None:
    """The public parts of a well-formed key, or None for anything else."""

    if not isinstance(raw_key, str) or len(raw_key) > _MAX_KEY_LENGTH or not raw_key.isascii():
        return None
    match = _KEY_RE.fullmatch(raw_key)
    if match is None:
        return None
    environment = match.group(1)
    if environment not in KEY_ENVIRONMENTS:  # pragma: no cover - the regex already limits this
        return None
    return ParsedKey(environment=environment, key_id=match.group(2))  # type: ignore[arg-type]


def hash_matches(raw_key: str, stored_hash: str) -> bool:
    """Constant-time comparison; call only after ``parse_key`` accepted the key."""

    return secrets.compare_digest(hash_key(raw_key), stored_hash.lower())


__all__ = [
    "KEY_ENVIRONMENTS",
    "KEY_ID_LENGTH",
    "KeyEnvironment",
    "NewKey",
    "ParsedKey",
    "generate_key",
    "hash_key",
    "hash_matches",
    "parse_key",
]
