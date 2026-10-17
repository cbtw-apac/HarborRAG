from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from harborrag_core.contracts.errors import (
    HarborAccessLimitError,
    HarborAuthorizationUnavailableError,
    HarborUnavailableError,
)
from harborrag_core.security import (
    QUARANTINE_CLASS,
    AccessContext,
    AccessScope,
    AclClass,
    ClassId,
    Decision,
    DocumentAccess,
    ResourceRef,
)
from harborrag_core.sources import SourceAcl, SourcePrincipal
from harborrag_core.topology.permissions import (
    BuildInputLineage,
    DerivedArtifactLineage,
    PermissionDependency,
    ResolvedPermissionSnapshot,
)

REALM = "atlassian:acme.atlassian.net"
SCOPE = "jira-project:CPM"


def _inherited() -> SourceAcl:
    return SourceAcl.inherited(realm=REALM, scope=SCOPE)


def _restricted() -> SourceAcl:
    return SourceAcl.restricted(
        realm=REALM,
        scope=SCOPE,
        levels=[[SourcePrincipal(realm=REALM, kind="group", id="g1")]],
        reason="restricted",
    )


def test_access_context_defaults_serve_with_no_classes():
    access = AccessContext(principal_id="u1", tenant_id="t1")
    assert (access.purpose, access.class_ids, access.scope.status) == (
        "serve",
        frozenset(),
        "full",
    )
    assert access.entitled_source_scopes is None
    assert access.actor_id is None


def test_access_context_without_the_new_fields_is_unchanged_on_the_wire():
    access = AccessContext(principal_id="u1", tenant_id="t1")
    assert AccessContext.model_validate(access.model_dump()) == access
    assert AccessContext.model_validate_json(access.model_dump_json()) == access
    assert access.model_copy(update={"corpus_mode": "tenant_shared"}).purpose == "serve"


def test_access_context_carries_classes_entitlements_and_scope():
    access = AccessContext(
        principal_id="u1",
        tenant_id="t1",
        actor_id="actor-1",
        class_ids=frozenset({"c1", "c2"}),
        entitled_source_scopes=frozenset({"ds-1"}),
        scope=AccessScope.partial("directory degraded"),
    )
    assert access.class_ids == frozenset({"c1", "c2"})
    assert access.entitled_source_scopes == frozenset({"ds-1"})
    assert access.scope.status == "partial"
    assert AccessContext.model_validate_json(access.model_dump_json()) == access


def test_access_context_accepts_collections_for_the_set_fields():
    access = AccessContext(
        principal_id="u1",
        tenant_id="t1",
        class_ids=["c1", "c1"],  # type: ignore[arg-type]
        entitled_source_scopes={"ds-1"},  # type: ignore[arg-type]
    )
    assert access.class_ids == frozenset({"c1"})
    assert access.entitled_source_scopes == frozenset({"ds-1"})


def test_an_empty_entitlement_set_is_distinct_from_no_rbac():
    nothing = AccessContext(principal_id="u1", tenant_id="t1", entitled_source_scopes=frozenset())
    unconfigured = AccessContext(principal_id="u1", tenant_id="t1")
    assert nothing.entitled_source_scopes == frozenset()
    assert unconfigured.entitled_source_scopes is None
    assert nothing != unconfigured


@pytest.mark.parametrize("actor", ["", "x" * 256])
def test_actor_id_is_bounded_when_set(actor):
    with pytest.raises(ValidationError):
        AccessContext(principal_id="u1", tenant_id="t1", actor_id=actor)


def test_access_context_rejects_an_unknown_purpose():
    with pytest.raises(ValidationError):
        AccessContext(principal_id="u1", tenant_id="t1", purpose="admin")  # type: ignore[arg-type]


def test_system_context_is_for_processing():
    access = AccessContext.system("t1")
    assert access.purpose == "process"
    assert (access.principal_id, str(access.tenant_id)) == ("harborrag-runtime", "t1")
    assert access.class_ids == frozenset()
    assert access.entitled_source_scopes is None


def test_document_access_needs_a_class_and_a_datasource():
    with pytest.raises(ValueError):
        DocumentAccess(class_id=ClassId("c"), source_scope_id="")
    with pytest.raises(ValueError):
        DocumentAccess(class_id=ClassId(" "), source_scope_id="ds-1")
    ok = DocumentAccess(class_id=ClassId("c"), source_scope_id="ds-1")
    assert (ok.class_id, ok.source_scope_id) == ("c", "ds-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ok.class_id = ClassId("d")  # type: ignore[misc]


def test_stale_scope_names_its_realms():
    with pytest.raises(ValueError):
        AccessScope(status="stale")
    assert AccessScope.stale(["atlassian:acme.atlassian.net"]).stale_realms == (
        "atlassian:acme.atlassian.net",
    )


def test_partial_scope_needs_a_reason():
    with pytest.raises(ValueError):
        AccessScope(status="partial")
    with pytest.raises(ValueError):
        AccessScope.partial("  ")
    scope = AccessScope.partial("directory unreachable")
    assert (scope.status, scope.reason) == ("partial", "directory unreachable")


def test_stale_scope_rejects_blank_realms():
    with pytest.raises(ValueError):
        AccessScope.stale([""])
    with pytest.raises(ValueError):
        AccessScope.stale([])


@pytest.mark.parametrize("realms", ["atlassian:acme.atlassian.net", b"atlassian:acme"])
def test_stale_scope_refuses_a_bare_string_for_its_realms(realms: object):
    # A bare string would otherwise be read as one realm per character.
    with pytest.raises(ValueError):
        AccessScope.stale(realms)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["full", "partial"])
def test_only_a_stale_scope_names_stale_realms(status: str):
    with pytest.raises(ValueError, match="stale"):
        AccessScope(
            status=status,  # type: ignore[arg-type]
            reason="directory unreachable",
            stale_realms=("atlassian:acme.atlassian.net",),
        )
    assert AccessScope(status=status, reason="directory unreachable").stale_realms == ()  # type: ignore[arg-type]


def test_full_scope_is_the_default_and_is_immutable():
    scope = AccessScope.full()
    assert (scope.status, scope.reason, scope.stale_realms) == ("full", None, ())
    assert scope == AccessScope()
    with pytest.raises(ValidationError):
        scope.status = "partial"  # type: ignore[misc]


def test_an_unknown_acl_cannot_be_a_class():
    with pytest.raises(ValueError):
        AclClass(tenant_id="t1", class_id=ClassId("c"), acl=SourceAcl.unknown(reason="x"))


def test_acl_class_accepts_known_acls():
    for acl in (_inherited(), _restricted()):
        member = AclClass(tenant_id="t1", class_id=ClassId("c"), acl=acl)
        assert (member.tenant_id, member.class_id, member.acl) == ("t1", "c", acl)


@pytest.mark.parametrize(
    ("tenant_id", "class_id"),
    [("", "c"), ("  ", "c"), ("t1", ""), ("t1", "  ")],
)
def test_acl_class_needs_a_tenant_and_a_class_id(tenant_id, class_id):
    with pytest.raises(ValueError):
        AclClass(tenant_id=tenant_id, class_id=ClassId(class_id), acl=_inherited())


def test_acl_class_needs_a_source_acl():
    with pytest.raises(ValueError):
        AclClass(tenant_id="t1", class_id=ClassId("c"), acl={"status": "inherited"})  # type: ignore[arg-type]


def test_quarantine_is_a_class_id():
    assert QUARANTINE_CLASS == "quarantine"


def test_decision_defaults_to_no_class():
    assert Decision(allowed=False, reason="not_member") == Decision(
        allowed=False, reason="not_member", class_id=None
    )
    granted = Decision(allowed=True, reason="member", class_id=ClassId("c"))
    assert (granted.allowed, granted.reason, granted.class_id) == (True, "member", "c")
    with pytest.raises(dataclasses.FrozenInstanceError):
        granted.allowed = False  # type: ignore[misc]


@pytest.mark.parametrize("kind", ["document", "chunk", "node", "edge", "summary", "blob", "memory"])
def test_resource_ref_kinds(kind):
    ref = ResourceRef(kind=kind, id="r1")
    assert (ref.kind, ref.id, ref.document_id) == (kind, "r1", None)


def test_resource_ref_rejects_a_blank_id_an_unknown_kind_or_a_blank_document():
    with pytest.raises(ValueError):
        ResourceRef(kind="chunk", id="")
    with pytest.raises(ValueError):
        ResourceRef(kind="tenant", id="r1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ResourceRef(kind="chunk", id="r1", document_id=" ")
    assert ResourceRef(kind="chunk", id="r1", document_id="d1").document_id == "d1"


def test_access_limit_error_fails_closed_like_authorization_unavailable():
    assert issubclass(HarborAccessLimitError, HarborAuthorizationUnavailableError)
    assert issubclass(HarborAccessLimitError, HarborUnavailableError)
    with pytest.raises(HarborAuthorizationUnavailableError):
        raise HarborAccessLimitError("too many classes")


def _dependencies(*kinds: str) -> tuple[PermissionDependency, ...]:
    return tuple(
        PermissionDependency(resource_kind=kind, resource_id=f"{kind}-1", revision="r")
        for kind in kinds  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("kinds", "ok"),
    [
        (("class",), True),
        (("class", "source"), True),
        (("source", "document"), True),
        (("source", "document", "class"), True),
        (("document",), False),
        (("source",), False),
        (("document", "document"), False),
    ],
)
def test_lineage_depends_on_a_class_or_on_source_and_document(kinds, ok):
    deps = _dependencies(*kinds)

    def build() -> BuildInputLineage:
        return BuildInputLineage(input_document_versions={"d1": "v1"}, permission_dependencies=deps)

    if ok:
        assert build().permission_dependencies == deps
    else:
        with pytest.raises(ValueError):
            build()


def test_lineage_still_needs_at_least_one_dependency():
    with pytest.raises(ValueError):
        BuildInputLineage(input_document_versions={"d1": "v1"}, permission_dependencies=())


def test_derived_lineage_inherits_the_class_rule():
    def derived(*kinds: str) -> DerivedArtifactLineage:
        return DerivedArtifactLineage(
            input_document_versions={"d1": "v1"},
            permission_dependencies=_dependencies(*kinds),
            artifact_id="a1",
            artifact_kind="parent_summary",
            build_id="b1",
            input_digest="digest",
        )

    assert derived("class").permission_dependencies[0].resource_kind == "class"
    with pytest.raises(ValueError):
        derived("source")


def test_permission_snapshot_can_describe_a_class():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = ResolvedPermissionSnapshot(
        tenant_id="t1",
        resource_kind="class",
        resource_id="c1",
        revision="r1",
        resolved_at=now,
        expires_at=now + timedelta(hours=1),
        known=True,
    )
    assert snapshot.dependency == PermissionDependency(
        resource_kind="class", resource_id="c1", revision="r1"
    )
    with pytest.raises(ValidationError):
        PermissionDependency(resource_kind="tenant", resource_id="x", revision="r")  # type: ignore[arg-type]
