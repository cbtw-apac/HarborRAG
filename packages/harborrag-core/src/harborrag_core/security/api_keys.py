import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Literal, cast

Environment = Literal["dev", "staging", "prod"]
_ENVIRONMENTS = {"dev", "staging", "prod"}
_MAX_KEY_LENGTH = 120
_KEY_RE = re.compile(r"^hrk_(dev|staging|prod)_v1_([0-9a-f]{24})\.([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class NewKey:
    key_id: str
    secret_hash: str = field(repr=False)
    raw_key: str = field(repr=False)


@dataclass
class ParsedKey:
    environment: Environment
    key_id: str


def hash_key(raw_key: str) -> str:
    """SHA-256 of the complete key. Only valid for machine-generated keys."""
    return hashlib.sha256(raw_key.encode("ascii")).hexdigest()


def generate_key(environment: Environment) -> NewKey:
    if environment not in _ENVIRONMENTS:
        raise ValueError("Unsupported environment")
    key_id = secrets.token_hex(12)
    raw_key = f"hrk_{environment}_v1_{key_id}.{secrets.token_urlsafe(32)}"
    return NewKey(key_id=key_id, secret_hash=hash_key(raw_key), raw_key=raw_key)


def parse_key(raw_key: str) -> ParsedKey | None:
    """Return the public parts, or None if the string isn't a valid key format."""
    if len(raw_key) > _MAX_KEY_LENGTH or not raw_key.isascii():
        return None
    match = _KEY_RE.fullmatch(raw_key)
    if match is None:
        return None
    # The regex alternation only admits dev|staging|prod.
    return ParsedKey(environment=cast("Environment", match.group(1)), key_id=match.group(2))


def hash_matches(raw_key: str, stored_hash: str) -> bool:
    """Constant-time comparison. Call only after parse_key() succeeded (key is ASCII)."""
    return secrets.compare_digest(hash_key(raw_key), stored_hash)
