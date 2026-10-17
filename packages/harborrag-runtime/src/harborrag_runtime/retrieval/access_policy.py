"""What a serving context is held to before any read: the corpus policy and the access file.

Every serving call reaches the corpus through a reader facade, and each facade first passes
the caller's :class:`~harborrag_core.security.context.AccessContext` through
:meth:`ReaderAccessPolicy.hold`. The held context is the one the service sees; the one the
caller built is never used for a read. A held context

- is for serving only: a ``process`` context is trusted background work and is refused;
- carries its tenant's corpus mode from the deployment's policy, whatever the caller set;
- is limited to the datasources the access file entitles its principal to, when there is a
  file, and never to more than the context itself already allowed; and
- entitles at least one datasource, or is refused.

Refusals are ``HarborAuthError(..., forbidden=True)`` and say nothing about who was refused.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.security.entitlements import narrower
from harborrag_engine.security.access_service import CorpusPolicy

if TYPE_CHECKING:
    from harborrag_core.ports.access import ConnectionScopeReader
    from harborrag_core.security.context import AccessContext
    from harborrag_runtime.config.access_file import AccessFileWatcher
    from harborrag_runtime.config.settings import RuntimeSettings


class FileEntitlements:
    """The source scopes the access file entitles a principal to, as of now.

    The file names connection ids; they are resolved to the scope ids ingested for them on
    every call, so a connection ingested after the file was written, or removed from the
    catalog, is followed without a restart.
    """

    def __init__(self, watcher: AccessFileWatcher, *, scopes: ConnectionScopeReader) -> None:
        self._watcher = watcher
        self._scopes = scopes

    async def entitled_source_scopes(self, tenant_id: str, principal_id: str) -> frozenset[str]:
        """The principal's scopes: never ``None``, and empty when anything is missing.

        A missing or invalid file, a tenant or principal the file does not list, and
        connections that have no scope yet all give the empty set, which entitles nothing.
        The watcher stats and may parse files, so it is read off the event loop.
        """

        access = await asyncio.to_thread(self._watcher.current)
        if access is None:
            return frozenset()
        connections = access.entitled_connections(tenant_id, principal_id)
        if not connections:
            return frozenset()
        by_connection = await self._scopes.source_scope_ids_by_connection(
            tenant_id, sorted(connections)
        )
        return frozenset().union(*by_connection.values())


class ReaderAccessPolicy:
    """Hold serving contexts to the corpus policy and, when there is one, the access file."""

    def __init__(self, *, corpus: CorpusPolicy, entitlements: FileEntitlements | None) -> None:
        self._corpus = corpus
        self._entitlements = entitlements

    async def hold(self, access: AccessContext) -> AccessContext:
        """The context a read may run as, or ``HarborAuthError`` (403) when there is none."""

        if access.purpose == "process":
            raise HarborAuthError("a process context cannot be served", forbidden=True)
        tenant_id = str(access.tenant_id)
        held = access.model_copy(update={"corpus_mode": self._corpus.mode_for(tenant_id)})
        if self._entitlements is not None:
            from_file = await self._entitlements.entitled_source_scopes(
                tenant_id, access.principal_id
            )
            held = held.model_copy(
                update={
                    "entitled_source_scopes": narrower(access.entitled_source_scopes, from_file)
                }
            )
        if held.entitled_source_scopes is not None and not held.entitled_source_scopes:
            raise HarborAuthError("not entitled to any datasource in this tenant", forbidden=True)
        return held


def corpus_policy_from_settings(settings: RuntimeSettings) -> CorpusPolicy:
    """The deployment's corpus policy; the shared tenant id counts only in shared mode."""

    shared = settings.corpus_access_mode == "tenant_shared"
    return CorpusPolicy(
        mode=settings.corpus_access_mode,
        shared_tenant_id=settings.corpus_shared_tenant_id if shared else None,
    )


def build_access_policy(
    settings: RuntimeSettings, *, scopes: ConnectionScopeReader | None
) -> ReaderAccessPolicy:
    """The policy a deployment serves under: its corpus policy, plus the file when one is set.

    ``scopes`` resolves the file's connection ids to scope ids; it is read only when
    ``settings.access_config_path`` is set.
    """

    corpus = corpus_policy_from_settings(settings)
    if settings.access_config_path is None:
        return ReaderAccessPolicy(corpus=corpus, entitlements=None)
    if scopes is None:
        raise ValueError("an access file needs a connection scope reader to resolve connections")
    from harborrag_runtime.config.access_file import AccessFileWatcher

    watcher = AccessFileWatcher(
        settings.access_config_path, catalog_path=settings.connector_config_path
    )
    return ReaderAccessPolicy(corpus=corpus, entitlements=FileEntitlements(watcher, scopes=scopes))


__all__ = [
    "FileEntitlements",
    "ReaderAccessPolicy",
    "build_access_policy",
    "corpus_policy_from_settings",
]
