from __future__ import annotations

from harborrag_core.storage import DEFAULT_STORAGE_NAMESPACE_PREFIX, tenant_namespace


def validate_object_key(bucket: str, key: str) -> None:
    if not bucket or "/" in bucket or "\\" in bucket or bucket in {".", ".."}:
        raise ValueError("invalid bucket name")
    normalized = key.replace("\\", "/")
    if not key or key.startswith(("/", "\\")) or ".." in normalized.split("/"):
        raise ValueError("invalid object key")


def tenant_object_prefix(
    tenant_id: object, namespace_prefix: str = DEFAULT_STORAGE_NAMESPACE_PREFIX
) -> str:
    """Return the tenant's physical prefix: its storage namespace, as in every store."""
    return tenant_namespace(tenant_id, namespace_prefix)


def physical_object_key(
    tenant_id: object, key: str, namespace_prefix: str = DEFAULT_STORAGE_NAMESPACE_PREFIX
) -> str:
    """Map a public object key into its tenant-isolated physical key."""
    return f"{tenant_object_prefix(tenant_id, namespace_prefix)}/{key}"


def logical_object_key(
    tenant_id: object,
    physical_key: str,
    namespace_prefix: str = DEFAULT_STORAGE_NAMESPACE_PREFIX,
) -> str | None:
    """Recover a logical key only when it belongs to the requested tenant."""
    prefix = f"{tenant_object_prefix(tenant_id, namespace_prefix)}/"
    return physical_key.removeprefix(prefix) if physical_key.startswith(prefix) else None
