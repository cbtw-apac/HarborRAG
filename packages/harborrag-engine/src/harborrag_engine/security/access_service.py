"""The class-based access service: a verified credential in, access contexts and decisions out.

:class:`ClassAccessService` composes the ports of ``AccessFactsPort`` with the pure
rules of :func:`~harborrag_engine.security.decide.decide`. It does no I/O of its own
and keeps no state besides its configuration.

``authenticate`` turns a :class:`~harborrag_core.ports.identity.VerifiedToken` into an
:class:`~harborrag_core.security.context.AccessContext`:

- the tenant is the one asked for, or the token's only named tenant, and the token must
  grant it (listed, or ``*``);
- ``tenant_shared`` applies to the one tenant :class:`CorpusPolicy` names, and ``source_acl``
  to every other;
- ``class_ids`` are the classes the principal may read, public ones included and the
  quarantine class never. A class that is not public and whose realm is stale is not
  among them, nor is one whose class or snapshot record is missing or is not its own.
  They are empty in a shared corpus, which has no per-class gate;
- ``entitled_source_scopes`` come from the entitlement store in both modes;
- the scope is ``stale`` and names the realms that are stale: those of the principal's
  readable classes, and any realm whose last sync is past its SLO.

A realm is stale when it has no sync record, or its last sync is older than its
revocation SLO. Public classes are read whatever the state of their realm, as
:func:`~harborrag_engine.security.decide.decide` reads them.

Every doubt resolves to less access. A reader of more classes than the limit raises
:class:`~harborrag_core.contracts.errors.HarborAccessLimitError` and is never given a
truncated list, and ``check_batch`` answers only once its decisions are recorded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import get_args

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborAccessLimitError, HarborAuthError
from harborrag_core.ports.access import AccessFactsPort
from harborrag_core.ports.identity import VerifiedToken
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security.classes import QUARANTINE_CLASS, AclClass, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import AccessScope, Decision
from harborrag_core.security.directory import DecisionLogEntry
from harborrag_core.security.resources import ResourceRef
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
from harborrag_engine.security.admit import CorpusMode
from harborrag_engine.security.decide import decide

WILDCARD_TENANT = "*"
"""The tenant a token lists when it may act in every tenant. It is never a tenant itself."""

_CORPUS_MODES: frozenset[str] = frozenset(get_args(CorpusMode))


def _is_tenant_id(value: object) -> bool:
    """Whether ``value`` can name one tenant: plain text, no padding, not the wildcard."""

    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value != WILDCARD_TENANT
    )


@dataclass(frozen=True, slots=True)
class CorpusPolicy:
    """How the corpus is read: per source ACL, or shared by every member of one tenant.

    ``mode`` applies to ``shared_tenant_id`` only; every other tenant is ``source_acl``.
    This is the only place that rule lives. A ``tenant_shared`` policy must name its
    tenant, since a shared corpus nobody can be named for would silently be source-ACL
    everywhere.
    """

    mode: CorpusMode
    shared_tenant_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in _CORPUS_MODES:
            raise ValueError(f"unsupported corpus mode: {self.mode!r}")
        if self.shared_tenant_id is None:
            if self.mode == "tenant_shared":
                raise ValueError("a tenant_shared corpus requires the shared tenant id")
        elif not _is_tenant_id(self.shared_tenant_id):
            raise ValueError(f"unusable shared tenant id: {self.shared_tenant_id!r}")

    def mode_for(self, tenant_id: str) -> CorpusMode:
        """The corpus mode of ``tenant_id``."""

        if self.mode == "tenant_shared" and tenant_id == self.shared_tenant_id:
            return "tenant_shared"
        return "source_acl"


@dataclass(frozen=True, slots=True)
class AccessLimits:
    """The bounds access is enforced within.

    ``max_classes_per_user`` caps how many classes one reader may belong to; above it
    access cannot be decided and raises. A realm whose directory was synced longer ago
    than its revocation SLO (``revocation_slo``, else ``default_revocation_slo``) is
    stale.
    """

    max_classes_per_user: int = 5000
    default_revocation_slo: timedelta = timedelta(hours=24)
    revocation_slo: Mapping[str, timedelta] = field(default_factory=dict)

    def __post_init__(self) -> None:
        limit = self.max_classes_per_user
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("max_classes_per_user must be a non-negative integer")
        _require_positive(self.default_revocation_slo, "default_revocation_slo")
        if not isinstance(self.revocation_slo, Mapping):
            raise ValueError("revocation_slo must map realms to durations")
        slos: dict[str, timedelta] = {}
        for realm, slo in self.revocation_slo.items():
            if not isinstance(realm, str) or not realm.strip():
                raise ValueError("revocation_slo realms must be non-empty strings")
            _require_positive(slo, f"revocation_slo of {realm!r}")
            slos[realm] = slo
        object.__setattr__(self, "revocation_slo", MappingProxyType(slos))


def _require_positive(value: object, name: str) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{name} must be a positive duration")


def _tenant_of(token: VerifiedToken, requested: str | None) -> str:
    """The tenant ``token`` acts in, or ``HarborAuthError(forbidden=True)``."""

    if requested is None:
        named = token.tenant_ids - {WILDCARD_TENANT}
        if len(named) != 1:
            raise HarborAuthError(
                "a tenant must be named: the token does not grant exactly one", forbidden=True
            )
        (tenant,) = named
    else:
        tenant = requested
    granted = requested is None or WILDCARD_TENANT in token.tenant_ids or tenant in token.tenant_ids
    if not granted or not _is_tenant_id(tenant):
        raise HarborAuthError("tenant access is not permitted", forbidden=True)
    return tenant


def _vouched_realm(
    tenant: str,
    class_id: ClassId,
    acl_class: AclClass | None,
    snapshot: ResolvedPermissionSnapshot | None,
) -> str | None:
    """The realm of a class whose records are its own, else ``None``.

    Both records must exist, and the class and its snapshot must be ``class_id``'s in
    ``tenant``; anything else is no evidence about the class (as in ``decide``).
    """

    if acl_class is None or snapshot is None:
        return None
    if acl_class.class_id != class_id or acl_class.tenant_id != tenant:
        return None
    if (
        snapshot.resource_kind != "class"
        or snapshot.resource_id != class_id
        or snapshot.tenant_id != tenant
    ):
        return None
    return acl_class.acl.realm


def _document_id(ref: ResourceRef) -> str | None:
    """The document a resource is read through (the same rule ``decide`` applies)."""

    return ref.id if ref.kind == "document" else ref.document_id


class ClassAccessService:
    """:class:`~harborrag_core.ports.access.AccessService` over ACL classes."""

    def __init__(
        self,
        facts: AccessFactsPort,
        *,
        corpus: CorpusPolicy,
        limits: AccessLimits = AccessLimits(),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._facts = facts
        self._corpus = corpus
        self._limits = limits
        self._clock = clock

    async def authenticate(self, token: VerifiedToken, *, tenant_id: str | None) -> AccessContext:
        """The access context ``token`` has in a tenant; ``HarborAuthError`` when it has none."""

        tenant = _tenant_of(token, tenant_id)
        mode = self._corpus.mode_for(tenant)
        now = self._now()
        access = AccessContext(
            principal_id=token.user_id,
            tenant_id=TenantId(tenant),
            corpus_mode=mode,
            actor_id=token.actor_id,
            entitled_source_scopes=await self._facts.entitled_source_scopes(tenant, token.user_id),
        )
        synced = await self._facts.realm_sync_times(tenant)
        if mode == "tenant_shared":
            class_ids: frozenset[ClassId] = frozenset()
            stale = self._stale_realms(synced, now)
        else:
            class_ids, stale = await self._servable_classes(access, synced, now)
        scope = AccessScope.stale(tuple(sorted(stale))) if stale else AccessScope.full()
        return access.model_copy(update={"class_ids": class_ids, "scope": scope})

    async def classes_for(self, principal_id: str, tenant_id: str) -> frozenset[ClassId]:
        """The classes ``principal_id`` belongs to in ``tenant_id``, whatever the corpus mode.

        This is membership as the snapshots record it: unlike ``authenticate`` it does
        not drop the classes of stale realms. Raises
        :class:`~harborrag_core.contracts.errors.HarborAccessLimitError` rather than
        truncating, and never includes the quarantine class.
        """

        if not _is_tenant_id(tenant_id):
            raise ValueError("tenant_id must be a plain tenant id")
        access = AccessContext(
            principal_id=principal_id,
            tenant_id=TenantId(tenant_id),
            corpus_mode=self._corpus.mode_for(tenant_id),
        )
        return await self._readable_classes(access)

    async def check_batch(
        self, access: AccessContext, refs: Sequence[ResourceRef]
    ) -> list[Decision]:
        """One decision per ref, in order, recorded before they are returned.

        Staleness is judged now, from the directories' sync times, and a realm the
        context already names as stale stays stale: the context is never trusted to
        be fresher than it says. A recording failure propagates, so no decision
        leaves unrecorded.
        """

        if not refs:
            return []
        now = self._now()
        tenant = str(access.tenant_id)
        document_ids = list(
            dict.fromkeys(id_ for ref in refs if (id_ := _document_id(ref)) is not None)
        )
        documents = await self._facts.document_access(tenant, document_ids) if document_ids else {}
        class_ids = list(dict.fromkeys(document.class_id for document in documents.values()))
        # ``classes(tenant, None)`` would mean every class, so an empty list is never passed.
        classes = await self._facts.classes(tenant, class_ids) if class_ids else {}
        snapshots = await self._facts.class_snapshots(tenant, class_ids) if class_ids else {}
        synced = await self._facts.realm_sync_times(tenant)
        stale_realms = self._stale_realms(
            synced, now, among=(acl_class.acl.realm for acl_class in classes.values())
        ) | frozenset(access.scope.stale_realms)
        decisions = decide(
            access,
            refs,
            documents=documents,
            classes=classes,
            snapshots=snapshots,
            stale_realms=stale_realms,
            now=now,
        )
        await self._facts.record(
            [
                DecisionLogEntry(
                    tenant_id=tenant,
                    principal_id=access.principal_id,
                    actor_id=access.actor_id,
                    resource=ref,
                    decision=decision,
                    at=now,
                )
                for ref, decision in zip(refs, decisions, strict=True)
            ]
        )
        return decisions

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("the clock must return a timezone-aware datetime")
        return now

    async def _readable_classes(self, access: AccessContext) -> frozenset[ClassId]:
        limit = self._limits.max_classes_per_user
        readable = await self._facts.readable_class_ids(access, limit=limit)
        if len(readable) > limit:
            # The store is meant to raise itself; a result over the limit is never used.
            raise HarborAccessLimitError(
                f"reader can read {len(readable)} ACL classes, over the limit of {limit}"
            )
        return frozenset(readable) - {QUARANTINE_CLASS}

    async def _servable_classes(
        self, access: AccessContext, synced: Mapping[str, datetime], now: datetime
    ) -> tuple[frozenset[ClassId], frozenset[str]]:
        """The readable classes that may be served now, and the stale realms found.

        A class is dropped when its class or snapshot record is missing or is not its
        own, or when it is not public and its realm is stale.
        """

        readable = sorted(await self._readable_classes(access))
        if not readable:
            return frozenset(), self._stale_realms(synced, now)
        tenant = str(access.tenant_id)
        classes = await self._facts.classes(tenant, readable)
        snapshots = await self._facts.class_snapshots(tenant, readable)
        realms: dict[ClassId, str] = {}
        for class_id in readable:
            realm = _vouched_realm(tenant, class_id, classes.get(class_id), snapshots.get(class_id))
            if realm is not None:
                realms[class_id] = realm
        stale = self._stale_realms(synced, now, among=realms.values())
        served = frozenset(
            class_id
            for class_id, realm in realms.items()
            if snapshots[class_id].public or realm not in stale
        )
        return served, stale

    def _stale_realms(
        self, synced: Mapping[str, datetime], now: datetime, *, among: Iterable[str] = ()
    ) -> frozenset[str]:
        """The realms whose last sync is older than their SLO, and those of ``among`` never synced."""

        slos = self._limits.revocation_slo
        default = self._limits.default_revocation_slo
        overdue = (
            realm
            for realm, synced_at in synced.items()
            if now - synced_at > slos.get(realm, default)
        )
        return frozenset(overdue) | frozenset(realm for realm in among if realm not in synced)
