"""One tenant namespace, spelled the same way in every store.

The vector collections, the knowledge graph and the object-store prefix of a
tenant all start with ``{prefix}_{tenant_id}``, so finding -- or removing --
everything a tenant owns is one name per store rather than three schemes (one of
them a hash). Postgres keeps the plain ``tenant_id`` column.
"""

from __future__ import annotations

import re

DEFAULT_STORAGE_NAMESPACE_PREFIX = "harborrag"

STORAGE_NAMESPACE_PREFIX_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
_PREFIX = re.compile(STORAGE_NAMESPACE_PREFIX_PATTERN)
# The charset every store accepts in a name: Qdrant collections, FalkorDB graph
# keys and S3 key segments. Trusted tenant ids already follow it.
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def tenant_namespace(
    tenant_id: object,
    prefix: str = DEFAULT_STORAGE_NAMESPACE_PREFIX,
) -> str:
    """Return ``{prefix}_{tenant_id}`` after validating both parts."""

    tenant = str(tenant_id)
    if _TENANT.fullmatch(tenant) is None:
        raise ValueError(
            "storage namespaces require a trusted tenant of 1-128 ASCII letters, "
            "digits, '.', '_' or '-' beginning with a letter or digit"
        )
    if _PREFIX.fullmatch(prefix) is None:
        raise ValueError(
            "storage namespace prefix must be 1-64 ASCII letters, digits, '_' or '-' "
            "beginning with a letter"
        )
    return f"{prefix}_{tenant}"


__all__ = [
    "DEFAULT_STORAGE_NAMESPACE_PREFIX",
    "STORAGE_NAMESPACE_PREFIX_PATTERN",
    "tenant_namespace",
]
