"""Connector settings that are only safe under a particular corpus access mode."""

from __future__ import annotations

from harborrag_runtime.config.errors import ConnectorConfigurationError


def require_corpus_access_mode(name: str, connector: object, *, corpus_access_mode: str) -> None:
    """Refuse ``restricted_content=include`` outside ``source_acl`` corpus mode.

    ``include`` ingests items the source restricts and relies on per-document
    snapshots to hide them. ``tenant_shared`` ignores snapshots and serves every
    active document to the whole tenant, so the combination would leak them.
    The policy is read from the built provider config, so environment overrides
    and normalisation are already applied.
    """

    provider = getattr(connector, "provider", connector)
    policy = getattr(getattr(provider, "config", None), "restricted_content", None)
    if policy == "include" and corpus_access_mode != "source_acl":
        raise ConnectorConfigurationError(
            f"Connector {name!r} sets restricted_content=include, which requires "
            f"HARBORRAG_CORPUS_ACCESS_MODE=source_acl (current: {corpus_access_mode}); "
            "use restricted_content=skip or switch the corpus to source_acl"
        )
