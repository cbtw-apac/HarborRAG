"""The corpus access basis of a retrieval request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings


def corpus_mode_for(
    settings: RuntimeSettings, tenant_id: str
) -> Literal["source_acl", "tenant_shared"]:
    """Return the corpus mode a request for ``tenant_id`` reads on.

    Args:
        settings: Runtime settings.
        tenant_id: Tenant the request is scoped to.

    Returns:
        ``settings.corpus_access_mode`` for the configured shared tenant, else ``source_acl``.
    """
    if tenant_id == settings.corpus_shared_tenant_id:
        return settings.corpus_access_mode
    return "source_acl"
