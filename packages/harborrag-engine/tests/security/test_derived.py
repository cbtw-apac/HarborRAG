from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

import pytest

from harborrag_core.schemas.ids import TenantId
from harborrag_core.security.classes import QUARANTINE_CLASS, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_engine.security.derived import classes_of, may_serve

TENANT = "tenant-1"
OTHER_TENANT = "tenant-2"
C1 = ClassId("class-1")
C2 = ClassId("class-2")
C3 = ClassId("class-3")
SCOPE_A = "jira-project:CPM"
SCOPE_B = "confluence-space:ENG"


def access(
    *,
    classes: Iterable[str] = (),
    entitled: Iterable[str] | None = None,
    mode: Literal["source_acl", "tenant_shared"] = "source_acl",
    purpose: Literal["serve", "process"] = "serve",
) -> AccessContext:
    return AccessContext(
        principal_id="hr-alice",
        tenant_id=TenantId(TENANT),
        corpus_mode=mode,
        purpose=purpose,
        class_ids=frozenset(classes),
        entitled_source_scopes=None if entitled is None else frozenset(entitled),
    )


# --- classes_of ------------------------------------------------------------------------------


def test_classes_of_collects_the_distinct_classes() -> None:
    assert classes_of([C1, C2, C1]) == frozenset({C1, C2})


def test_classes_of_accepts_any_iterable_and_no_inputs() -> None:
    assert classes_of(class_id for class_id in (C3,)) == frozenset({C3})
    assert classes_of([]) == frozenset()


# --- may_serve -------------------------------------------------------------------------------


def test_derived_data_needs_every_input_class() -> None:
    context = access(classes=[C1, C2])

    assert may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1, C2}), source_scopes=frozenset({SCOPE_A})
    )
    assert may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_A})
    )
    assert not may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1, C3}), source_scopes=frozenset({SCOPE_A})
    )
    assert not may_serve(
        context, tenant_id=TENANT, classes=frozenset({C3}), source_scopes=frozenset({SCOPE_A})
    )
    assert not may_serve(
        access(classes=[C1]),
        tenant_id=TENANT,
        classes=frozenset({C1, C2}),
        source_scopes=frozenset({SCOPE_A}),
    )


def test_derived_data_needs_every_input_datasource() -> None:
    context = access(classes=[C1], entitled=[SCOPE_A])

    assert may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_A})
    )
    assert not may_serve(
        context,
        tenant_id=TENANT,
        classes=frozenset({C1}),
        source_scopes=frozenset({SCOPE_A, SCOPE_B}),
    )
    assert not may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_B})
    )
    both = access(classes=[C1], entitled=[SCOPE_A, SCOPE_B])
    assert may_serve(
        both, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_A, SCOPE_B})
    )


def test_a_tenant_without_rbac_skips_the_datasource_gate() -> None:
    context = access(classes=[C1], entitled=None)

    assert may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_A})
    )
    assert may_serve(context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset())


def test_an_empty_entitlement_denies_derived_data() -> None:
    for mode in ("source_acl", "tenant_shared"):
        context = access(classes=[C1], entitled=[], mode=mode)

        assert not may_serve(
            context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_A})
        )
        assert not may_serve(
            context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset()
        ), mode


def test_derived_data_without_inputs_is_never_served() -> None:
    contexts = [
        access(),
        access(classes=[C1]),
        access(classes=[C1], entitled=[SCOPE_A]),
        access(mode="tenant_shared"),
        access(mode="tenant_shared", entitled=[SCOPE_A]),
    ]

    for context in contexts:
        assert not may_serve(
            context, tenant_id=TENANT, classes=frozenset(), source_scopes=frozenset({SCOPE_A})
        )
        assert not may_serve(
            context, tenant_id=TENANT, classes=frozenset(), source_scopes=frozenset()
        )


def test_derived_data_from_a_quarantined_input_is_never_served() -> None:
    inputs = frozenset({C1, QUARANTINE_CLASS})
    contexts = [
        access(classes=[C1, QUARANTINE_CLASS]),
        access(classes=[C1, QUARANTINE_CLASS], entitled=[SCOPE_A]),
        access(mode="tenant_shared"),
    ]

    for context in contexts:
        assert not may_serve(
            context, tenant_id=TENANT, classes=inputs, source_scopes=frozenset({SCOPE_A})
        )
        assert not may_serve(
            context,
            tenant_id=TENANT,
            classes=frozenset({QUARANTINE_CLASS}),
            source_scopes=frozenset({SCOPE_A}),
        )


def test_shared_corpus_serves_derived_data() -> None:
    context = access(mode="tenant_shared")

    assert may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1, C2}), source_scopes=frozenset({SCOPE_A})
    )
    entitled = access(mode="tenant_shared", entitled=[SCOPE_A, SCOPE_B])
    assert may_serve(
        entitled,
        tenant_id=TENANT,
        classes=frozenset({C1}),
        source_scopes=frozenset({SCOPE_A, SCOPE_B}),
    )


def test_shared_corpus_still_applies_the_datasource_gate_to_derived_data() -> None:
    context = access(mode="tenant_shared", entitled=[SCOPE_A])

    assert not may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_B})
    )


def test_a_process_context_is_never_served_derived_data() -> None:
    # Even one that holds every class, is entitled to every datasource, or reads the
    # shared corpus: a serve check refuses a background context.
    inputs = frozenset({C1})
    scopes = frozenset({SCOPE_A})
    contexts = [
        access(purpose="process"),
        access(purpose="process", classes=[C1]),
        access(purpose="process", classes=[C1], entitled=[SCOPE_A]),
        access(purpose="process", mode="tenant_shared"),
        access(purpose="process", mode="tenant_shared", classes=[C1], entitled=[SCOPE_A]),
    ]

    for context in contexts:
        assert not may_serve(context, tenant_id=TENANT, classes=inputs, source_scopes=scopes)


@pytest.mark.parametrize("mode", ["source_acl", "tenant_shared"])
def test_another_tenants_context_is_never_served_derived_data(
    mode: Literal["source_acl", "tenant_shared"],
) -> None:
    # Class ids are fingerprints of an ACL, so two tenants can hold the same class id.
    # Everything else says yes (the context holds the class and the datasource, and a
    # shared corpus serves everyone); only the tenant is wrong.
    context = AccessContext(
        principal_id="hr-alice",
        tenant_id=TenantId(OTHER_TENANT),
        corpus_mode=mode,
        class_ids=frozenset({C1}),
        entitled_source_scopes=frozenset({SCOPE_A}),
    )

    assert not may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_A})
    )
    assert may_serve(
        context,
        tenant_id=OTHER_TENANT,
        classes=frozenset({C1}),
        source_scopes=frozenset({SCOPE_A}),
    )


@pytest.mark.parametrize("mode", ["source_acl", "tenant_shared"])
def test_the_derived_datas_own_tenant_is_served_as_before(
    mode: Literal["source_acl", "tenant_shared"],
) -> None:
    context = access(classes=[C1], entitled=[SCOPE_A], mode=mode)

    assert may_serve(
        context, tenant_id=TENANT, classes=frozenset({C1}), source_scopes=frozenset({SCOPE_A})
    )
