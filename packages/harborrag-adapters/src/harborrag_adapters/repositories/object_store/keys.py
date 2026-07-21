from __future__ import annotations

import hashlib

_TENANT_ROOT = ".harborrag/tenants"


def validate_object_key(bucket: str, key: str) -> None:
    if not bucket or "/" in bucket or "\\" in bucket or bucket in {".", ".."}:
        raise ValueError("invalid bucket name")
    normalized = key.replace("\\", "/")
    if not key or key.startswith(("/", "\\")) or ".." in normalized.split("/"):
        raise ValueError("invalid object key")


def tenant_object_prefix(tenant_id: object) -> str:
    """Return an opaque, path-safe physical namespace for one tenant."""
    digest = hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()
    return f"{_TENANT_ROOT}/{digest}"


def physical_object_key(tenant_id: object, key: str) -> str:
    """Map a public object key into its tenant-isolated physical key."""
    return f"{tenant_object_prefix(tenant_id)}/{key}"


def logical_object_key(tenant_id: object, physical_key: str) -> str | None:
    """Recover a logical key only when it belongs to the requested tenant."""
    prefix = f"{tenant_object_prefix(tenant_id)}/"
    return physical_key.removeprefix(prefix) if physical_key.startswith(prefix) else None
