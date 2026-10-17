from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from harborrag_core.schemas.ids import TenantId
from harborrag_core.security.classes import QUARANTINE_CLASS, AclClass, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import Decision, DocumentAccess
from harborrag_core.security.resources import ResourceRef
from harborrag_core.sources.acl import SourceAcl
from harborrag_core.topology.permissions import (
    PermissionResourceKind,
    ResolvedPermissionSnapshot,
)
from harborrag_engine.security.decide import decide

REALM = "atlassian:acme.atlassian.net"
OTHER_REALM = "atlassian:other.atlassian.net"
SCOPE = "jira-project:CPM"
OTHER_SCOPE = "jira-project:OPS"
TENANT = "tenant-1"
OTHER_TENANT = "tenant-2"
PRINCIPAL = "hr-alice"
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)
EXPIRES = T0 + timedelta(hours=2)

C1 = ClassId("class-1")
C2 = ClassId("class-2")

Mode = Literal["source_acl", "tenant_shared"]
Purpose = Literal["serve", "process"]


def access(
    *,
    principal: str = PRINCIPAL,
    tenant: str = TENANT,
    mode: Mode = "source_acl",
    purpose: Purpose = "serve",
    entitled: Iterable[str] | None = None,
) -> AccessContext:
    return AccessContext(
        principal_id=principal,
        tenant_id=TenantId(tenant),
        corpus_mode=mode,
        purpose=purpose,
        entitled_source_scopes=None if entitled is None else frozenset(entitled),
    )


def acl_class(
    class_id: ClassId = C1,
    *,
    tenant: str = TENANT,
    realm: str = REALM,
    scope: str = SCOPE,
) -> AclClass:
    return AclClass(
        tenant_id=tenant, class_id=class_id, acl=SourceAcl.inherited(realm=realm, scope=scope)
    )


def snapshot(
    class_id: ClassId = C1,
    *,
    tenant: str = TENANT,
    known: bool = True,
    processing_allowed: bool = True,
    public: bool = False,
    allowed: Iterable[str] = (PRINCIPAL,),
    denied: Iterable[str] = (),
    resolved_at: datetime = T0,
    expires_at: datetime = EXPIRES,
    kind: PermissionResourceKind = "class",
) -> ResolvedPermissionSnapshot:
    return ResolvedPermissionSnapshot(
        tenant_id=tenant,
        resource_kind=kind,
        resource_id=class_id,
        revision="rev-1",
        resolved_at=resolved_at,
        expires_at=expires_at,
        known=known,
        processing_allowed=processing_allowed,
        public=public,
        allowed_principal_ids=tuple(allowed),
        denied_principal_ids=tuple(denied),
    )


def doc(ref_id: str = "doc-1") -> ResourceRef:
    return ResourceRef(kind="document", id=ref_id)


def run(
    context: AccessContext,
    refs: Sequence[ResourceRef],
    *,
    documents: Mapping[str, DocumentAccess] | None = None,
    classes: Mapping[ClassId, AclClass] | None = None,
    snapshots: Mapping[ClassId, ResolvedPermissionSnapshot] | None = None,
    stale_realms: frozenset[str] = frozenset(),
    now: datetime = NOW,
) -> list[Decision]:
    """Decide ``refs`` in a world where ``doc-1`` is in ``C1``, readable by ``PRINCIPAL``."""

    return decide(
        context,
        refs,
        documents={"doc-1": DocumentAccess(C1, SCOPE)} if documents is None else documents,
        classes={C1: acl_class()} if classes is None else classes,
        snapshots={C1: snapshot()} if snapshots is None else snapshots,
        stale_realms=stale_realms,
        now=now,
    )


def one(context: AccessContext, **world: object) -> Decision:
    (decision,) = run(context, [doc()], **world)  # type: ignore[arg-type]
    return decision


# --- rule 1: which document a ref belongs to -------------------------------------------------


def test_a_document_is_decided_by_its_own_id() -> None:
    assert one(access()) == Decision(True, "member", C1)


def test_a_chunk_is_decided_by_the_document_it_derives_from() -> None:
    chunk = ResourceRef(kind="chunk", id="chunk-9", document_id="doc-1")

    assert run(access(), [chunk]) == [Decision(True, "member", C1)]


def test_a_derived_resource_is_never_decided_by_its_own_id() -> None:
    # The chunk's own id happens to be a readable document; the chunk derives from an
    # unreadable one, and it is the latter that decides.
    documents = {"doc-1": DocumentAccess(C1, SCOPE), "doc-2": DocumentAccess(C2, SCOPE)}
    classes = {C1: acl_class(C1), C2: acl_class(C2)}
    snapshots = {C1: snapshot(C1), C2: snapshot(C2, allowed=("someone-else",))}
    chunk = ResourceRef(kind="chunk", id="doc-1", document_id="doc-2")

    decisions = run(access(), [chunk], documents=documents, classes=classes, snapshots=snapshots)

    assert decisions == [Decision(False, "not_member", C2)]


def test_a_document_ref_is_decided_by_its_own_class_whatever_document_id_it_carries() -> None:
    # ``doc-unreadable`` is not readable by the principal; the stray ``document_id`` names a
    # document that is. Borrowing that document's class would turn a denial into ``member``.
    documents = {
        "doc-readable": DocumentAccess(C1, SCOPE),
        "doc-unreadable": DocumentAccess(C2, SCOPE),
    }
    classes = {C1: acl_class(C1), C2: acl_class(C2)}
    snapshots = {C1: snapshot(C1), C2: snapshot(C2, allowed=("someone-else",))}
    ref = ResourceRef(kind="document", id="doc-unreadable", document_id="doc-readable")

    decisions = run(access(), [ref], documents=documents, classes=classes, snapshots=snapshots)

    assert decisions == [Decision(False, "not_member", C2)]


def test_a_document_ref_with_a_stray_document_id_is_not_rescued_by_it() -> None:
    ref = ResourceRef(kind="document", id="doc-404", document_id="doc-1")

    assert run(access(), [ref]) == [Decision(False, "no_class", None)]


@pytest.mark.parametrize("kind", ["chunk", "node", "edge", "summary", "blob", "memory"])
def test_a_derived_resource_without_a_document_has_no_class(
    kind: Literal["chunk", "node", "edge", "summary", "blob", "memory"],
) -> None:
    ref = ResourceRef(kind=kind, id="doc-1")

    assert run(access(), [ref]) == [Decision(False, "no_class", None)]


def test_a_document_that_is_not_in_the_document_map_has_no_class() -> None:
    assert run(access(), [doc("doc-404")]) == [Decision(False, "no_class", None)]


def test_a_chunk_of_an_unknown_document_has_no_class() -> None:
    chunk = ResourceRef(kind="chunk", id="chunk-9", document_id="doc-404")

    assert run(access(), [chunk]) == [Decision(False, "no_class", None)]


# --- rule 2: quarantine ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "purpose"),
    [("source_acl", "serve"), ("tenant_shared", "serve"), ("source_acl", "process")],
)
def test_a_quarantined_document_is_never_readable(mode: Mode, purpose: Purpose) -> None:
    # The quarantine class is not an ACL class, so it is in no map; even a public,
    # processing-allowed snapshot filed under it must not let anything through.
    documents = {"doc-1": DocumentAccess(QUARANTINE_CLASS, SCOPE)}
    snapshots = {QUARANTINE_CLASS: snapshot(QUARANTINE_CLASS, public=True)}

    decision = one(
        access(mode=mode, purpose=purpose, entitled=[SCOPE]),
        documents=documents,
        snapshots=snapshots,
    )

    assert decision == Decision(False, "quarantined", QUARANTINE_CLASS)


# --- rule 3: the class, and the tenant it belongs to -----------------------------------------


def test_a_class_missing_from_the_class_map_has_no_class() -> None:
    assert one(access(), classes={}) == Decision(False, "no_class", C1)


def test_a_class_of_another_tenant_is_a_tenant_mismatch() -> None:
    classes = {C1: acl_class(tenant=OTHER_TENANT)}

    assert one(access(), classes=classes) == Decision(False, "tenant_mismatch", C1)


@pytest.mark.parametrize(
    ("mode", "purpose"),
    [("source_acl", "serve"), ("tenant_shared", "serve"), ("source_acl", "process")],
)
def test_another_tenants_context_is_denied(mode: Mode, purpose: Purpose) -> None:
    # Everything else says yes: the principal is listed, the snapshot is public and
    # processing-allowed, the datasource is entitled and the corpus is shared. Only
    # the tenant is wrong.
    context = access(tenant=OTHER_TENANT, mode=mode, purpose=purpose, entitled=[SCOPE])
    snapshots = {C1: snapshot(public=True, allowed=(PRINCIPAL,))}

    assert one(context, snapshots=snapshots) == Decision(False, "tenant_mismatch", C1)


def test_a_tenant_mismatch_is_reported_before_an_unentitled_datasource() -> None:
    classes = {C1: acl_class(tenant=OTHER_TENANT)}

    assert one(access(entitled=[]), classes=classes) == Decision(False, "tenant_mismatch", C1)


def test_a_missing_class_is_reported_before_an_unentitled_datasource() -> None:
    assert one(access(entitled=[]), classes={}) == Decision(False, "no_class", C1)


def test_a_class_filed_under_another_class_id_is_treated_as_missing() -> None:
    documents = {"doc-1": DocumentAccess(C2, SCOPE)}
    classes = {C2: acl_class(C1)}
    snapshots = {C2: snapshot(C2)}

    decision = one(access(), documents=documents, classes=classes, snapshots=snapshots)

    assert decision == Decision(False, "no_class", C2)


def test_a_misfiled_class_cannot_hide_a_stale_realm() -> None:
    # ``C2`` really belongs to a stale realm. ``C1`` (a fresh realm) is filed under its key;
    # trusting it would skip the stale-realm denial and answer ``member``.
    documents = {"doc-1": DocumentAccess(C2, SCOPE)}
    classes = {C2: acl_class(C1, realm=REALM)}
    snapshots = {C2: snapshot(C2)}

    decision = one(
        access(),
        documents=documents,
        classes=classes,
        snapshots=snapshots,
        stale_realms=frozenset({OTHER_REALM}),
    )

    assert decision == Decision(False, "no_class", C2)


# --- rule 4: datasource entitlement ----------------------------------------------------------


def test_an_entitled_datasource_is_readable() -> None:
    assert one(access(entitled=[SCOPE, OTHER_SCOPE])) == Decision(True, "member", C1)


def test_an_unentitled_datasource_is_not_entitled() -> None:
    decision = one(access(entitled=[OTHER_SCOPE]))

    assert decision == Decision(False, "not_entitled", C1)


def test_a_tenant_without_rbac_skips_the_entitlement_gate() -> None:
    assert one(access(entitled=None)) == Decision(True, "member", C1)


def test_an_unentitled_datasource_is_denied_even_in_the_shared_corpus() -> None:
    context = access(mode="tenant_shared", entitled=[OTHER_SCOPE])

    assert one(context) == Decision(False, "not_entitled", C1)


def test_an_empty_entitlement_denies_everything() -> None:
    documents = {"doc-1": DocumentAccess(C1, SCOPE), "doc-2": DocumentAccess(C1, OTHER_SCOPE)}
    snapshots = {C1: snapshot(public=True)}
    refs = [doc("doc-1"), doc("doc-2")]

    for mode in ("source_acl", "tenant_shared"):
        decisions = run(
            access(mode=mode, entitled=[]), refs, documents=documents, snapshots=snapshots
        )

        assert decisions == [Decision(False, "not_entitled", C1)] * 2, mode


def test_a_process_context_is_not_subject_to_datasource_entitlement() -> None:
    context = access(purpose="process", entitled=[])

    assert one(context) == Decision(True, "processing_allowed", C1)


def test_entitlement_is_decided_by_the_documents_datasource() -> None:
    documents = {"doc-1": DocumentAccess(C1, SCOPE), "doc-2": DocumentAccess(C1, OTHER_SCOPE)}

    decisions = run(access(entitled=[SCOPE]), [doc("doc-1"), doc("doc-2")], documents=documents)

    assert decisions == [Decision(True, "member", C1), Decision(False, "not_entitled", C1)]


# --- rule 5: the shared corpus ---------------------------------------------------------------


def test_a_serve_context_reads_the_shared_corpus_without_a_snapshot() -> None:
    context = access(mode="tenant_shared")

    assert one(context, snapshots={}) == Decision(True, "tenant_shared", C1)


def test_the_shared_corpus_ignores_what_the_snapshot_says() -> None:
    snapshots = {C1: snapshot(known=False, allowed=(), denied=(PRINCIPAL,))}

    decision = one(access(mode="tenant_shared"), snapshots=snapshots)

    assert decision == Decision(True, "tenant_shared", C1)


def test_the_shared_corpus_is_open_to_a_member_of_the_entitled_datasource() -> None:
    assert one(access(mode="tenant_shared", entitled=[SCOPE])) == Decision(
        True, "tenant_shared", C1
    )


# --- rule 6: a missing snapshot --------------------------------------------------------------


def test_a_class_without_a_snapshot_is_an_unknown_snapshot() -> None:
    assert one(access(), snapshots={}) == Decision(False, "unknown_snapshot", C1)


def test_a_snapshot_of_another_class_does_not_stand_in_for_a_missing_one() -> None:
    assert one(access(), snapshots={C2: snapshot(C2)}) == Decision(False, "unknown_snapshot", C1)


@pytest.mark.parametrize(
    ("mode", "purpose"),
    [("source_acl", "serve"), ("source_acl", "process"), ("tenant_shared", "process")],
)
def test_a_snapshot_filed_under_another_class_id_is_treated_as_missing(
    mode: Mode, purpose: Purpose
) -> None:
    # ``C1``'s snapshot lists the principal and allows processing, but it is filed under
    # ``C2``, the class of the document.
    documents = {"doc-1": DocumentAccess(C2, SCOPE)}
    classes = {C2: acl_class(C2)}
    snapshots = {C2: snapshot(C1)}

    decision = one(
        access(mode=mode, purpose=purpose),
        documents=documents,
        classes=classes,
        snapshots=snapshots,
    )

    assert decision == Decision(False, "unknown_snapshot", C2)


@pytest.mark.parametrize("purpose", ["serve", "process"])
@pytest.mark.parametrize("kind", ["source", "document"])
def test_a_snapshot_of_something_other_than_a_class_is_treated_as_missing(
    kind: PermissionResourceKind, purpose: Purpose
) -> None:
    snapshots = {C1: snapshot(C1, kind=kind)}

    decision = one(access(purpose=purpose), snapshots=snapshots)

    assert decision == Decision(False, "unknown_snapshot", C1)


def test_a_process_context_still_needs_a_snapshot_in_the_shared_corpus() -> None:
    context = access(mode="tenant_shared", purpose="process")

    assert one(context, snapshots={}) == Decision(False, "unknown_snapshot", C1)


# --- rule 7: a process context ---------------------------------------------------------------


def test_a_process_context_reads_by_processing_permission() -> None:
    # The principal is not a reader of the class: a background build does not read as one.
    snapshots = {C1: snapshot(allowed=("someone-else",), processing_allowed=True)}

    decision = one(access(purpose="process"), snapshots=snapshots)

    assert decision == Decision(True, "processing_allowed", C1)


def test_a_process_context_is_refused_when_processing_is_not_allowed() -> None:
    # A public snapshot that lists the principal still does not let a background build in.
    snapshots = {C1: snapshot(public=True, processing_allowed=False)}

    decision = one(access(purpose="process"), snapshots=snapshots)

    assert decision == Decision(False, "processing_disallowed", C1)


def test_a_process_context_is_refused_an_unknown_snapshot() -> None:
    snapshots = {C1: snapshot(known=False, processing_allowed=True)}

    decision = one(access(purpose="process"), snapshots=snapshots)

    assert decision == Decision(False, "processing_disallowed", C1)


@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        (T0 - timedelta(seconds=1), False),
        (T0, True),
        (EXPIRES - timedelta(seconds=1), True),
        (EXPIRES, False),
        (EXPIRES + timedelta(days=1), False),
    ],
    ids=[
        "before-validity",
        "at-resolved_at",
        "just-before-expiry",
        "at-expires_at",
        "long-expired",
    ],
)
def test_a_process_context_reads_only_inside_the_validity_window(
    now: datetime, allowed: bool
) -> None:
    decision = one(access(purpose="process"), now=now)

    reason = "processing_allowed" if allowed else "processing_disallowed"
    assert decision == Decision(allowed, reason, C1)


def test_a_process_context_is_refused_a_snapshot_of_another_tenant() -> None:
    snapshots = {C1: snapshot(tenant=OTHER_TENANT)}

    decision = one(access(purpose="process"), snapshots=snapshots)

    assert decision == Decision(False, "processing_disallowed", C1)


def test_a_process_context_is_not_held_back_by_a_stale_realm() -> None:
    decision = one(access(purpose="process"), stale_realms=frozenset({REALM}))

    assert decision == Decision(True, "processing_allowed", C1)


def test_a_process_context_in_the_shared_corpus_reads_by_processing_permission() -> None:
    context = access(mode="tenant_shared", purpose="process")

    assert one(context) == Decision(True, "processing_allowed", C1)
    refused = {C1: snapshot(processing_allowed=False)}
    assert one(context, snapshots=refused) == Decision(False, "processing_disallowed", C1)


# --- rule 8: a stale realm -------------------------------------------------------------------


def test_a_stale_realm_denies_a_class_that_is_not_public() -> None:
    # The principal is listed in a snapshot that is still inside its window; the realm's
    # directory is out of date, so the listing is not trusted.
    decision = one(access(), stale_realms=frozenset({REALM}))

    assert decision == Decision(False, "stale_realm", C1)


def test_a_public_class_is_readable_although_its_realm_is_stale() -> None:
    snapshots = {C1: snapshot(public=True, allowed=())}

    decision = one(access(), snapshots=snapshots, stale_realms=frozenset({REALM}))

    assert decision == Decision(True, "public", C1)


def test_a_stale_realm_only_affects_classes_of_that_realm() -> None:
    decision = one(access(), stale_realms=frozenset({OTHER_REALM}))

    assert decision == Decision(True, "member", C1)


def test_a_stale_realm_is_reported_before_membership() -> None:
    snapshots = {C1: snapshot(allowed=("someone-else",))}

    decision = one(access(), snapshots=snapshots, stale_realms=frozenset({REALM}))

    assert decision == Decision(False, "stale_realm", C1)


# --- rule 9: membership ----------------------------------------------------------------------


def test_a_listed_principal_is_a_member() -> None:
    assert one(access()) == Decision(True, "member", C1)


def test_anyone_may_read_a_public_class() -> None:
    snapshots = {C1: snapshot(public=True, allowed=())}

    assert one(access(), snapshots=snapshots) == Decision(True, "public", C1)


def test_a_listed_principal_of_a_public_class_reads_it_as_public() -> None:
    snapshots = {C1: snapshot(public=True, allowed=(PRINCIPAL,))}

    assert one(access(), snapshots=snapshots) == Decision(True, "public", C1)


def test_an_unlisted_principal_is_not_a_member() -> None:
    snapshots = {C1: snapshot(allowed=("someone-else",))}

    assert one(access(), snapshots=snapshots) == Decision(False, "not_member", C1)


def test_a_denied_principal_is_not_a_member_even_of_a_public_class() -> None:
    snapshots = {C1: snapshot(public=True, allowed=(PRINCIPAL,), denied=(PRINCIPAL,))}

    assert one(access(), snapshots=snapshots) == Decision(False, "not_member", C1)


def test_an_unknown_snapshot_is_never_readable() -> None:
    snapshots = {C1: snapshot(known=False, public=True, allowed=(PRINCIPAL,))}

    assert one(access(), snapshots=snapshots) == Decision(False, "not_member", C1)


@pytest.mark.parametrize(
    "now",
    [T0 - timedelta(seconds=1), EXPIRES, EXPIRES + timedelta(days=1)],
    ids=["before-validity", "at-expires_at", "long-expired"],
)
def test_a_snapshot_outside_its_window_is_not_readable(now: datetime) -> None:
    assert one(access(), now=now) == Decision(False, "not_member", C1)


def test_a_snapshot_of_another_tenant_is_not_readable() -> None:
    snapshots = {C1: snapshot(tenant=OTHER_TENANT, public=True)}

    assert one(access(), snapshots=snapshots) == Decision(False, "not_member", C1)


# --- the batch as a whole --------------------------------------------------------------------


def test_decisions_come_back_in_input_order() -> None:
    documents = {
        "doc-1": DocumentAccess(C1, SCOPE),
        "doc-2": DocumentAccess(C2, SCOPE),
        "doc-q": DocumentAccess(QUARANTINE_CLASS, SCOPE),
    }
    classes = {C1: acl_class(C1), C2: acl_class(C2)}
    snapshots = {C1: snapshot(C1), C2: snapshot(C2, allowed=("someone-else",))}
    refs = [
        doc("doc-2"),
        doc("doc-q"),
        ResourceRef(kind="chunk", id="chunk-1", document_id="doc-1"),
        doc("doc-404"),
        doc("doc-1"),
        doc("doc-2"),
    ]

    decisions = run(access(), refs, documents=documents, classes=classes, snapshots=snapshots)

    assert decisions == [
        Decision(False, "not_member", C2),
        Decision(False, "quarantined", QUARANTINE_CLASS),
        Decision(True, "member", C1),
        Decision(False, "no_class", None),
        Decision(True, "member", C1),
        Decision(False, "not_member", C2),
    ]


def test_no_refs_gives_no_decisions() -> None:
    assert run(access(), []) == []


def test_a_naive_clock_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run(access(), [doc()], now=datetime(2026, 10, 17, 13, 0))
