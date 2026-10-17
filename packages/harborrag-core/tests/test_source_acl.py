from __future__ import annotations

import json
from typing import Any

import pytest

from harborrag_core.base import _deep_freeze
from harborrag_core.sources import (
    SOURCE_ACL_METADATA_KEY,
    SourceAcl,
    SourcePrincipal,
    acl_from_metadata,
    acl_metadata,
)

REALM = "atlassian:acme.atlassian.net"
SCOPE = "jira-project:CPM"


def _group(gid: str, name: str | None = None) -> SourcePrincipal:
    return SourcePrincipal(realm=REALM, kind="group", id=gid, display_name=name)


def _user(uid: str) -> SourcePrincipal:
    return SourcePrincipal(realm=REALM, kind="user", id=uid)


def _restricted() -> SourceAcl:
    return SourceAcl.restricted(
        realm=REALM,
        scope=SCOPE,
        levels=[[_group("g1", "Recruiting"), _user("u1")], [_group("g2")]],
        reason="issue_security_level",
    )


# --- SourcePrincipal ---------------------------------------------------------


def test_principal_key_names_its_realm():
    assert _group("g1").key == "atlassian:acme.atlassian.net|group:g1"


def test_display_name_is_not_identity():
    assert _group("g1", "Recruiting") == _group("g1", "HR")
    assert hash(_group("g1", "Recruiting")) == hash(_group("g1", "HR"))
    assert len({_group("g1", "Recruiting"), _group("g1", "HR")}) == 1


def test_principals_in_other_realms_are_different_identities():
    other = SourcePrincipal(realm="entra:t1", kind="group", id="g1")
    assert other != _group("g1")
    assert other.key == "entra:t1|group:g1"
    assert other.key != _group("g1").key


def test_principals_order_by_realm_kind_id_ignoring_display_name():
    ordered = sorted([_group("b", "A"), _user("a"), _group("a", "Z")])
    assert [principal.key for principal in ordered] == [
        f"{REALM}|group:a",
        f"{REALM}|group:b",
        f"{REALM}|user:a",
    ]


def test_principals_are_keyword_only():
    with pytest.raises(TypeError):
        SourcePrincipal(REALM, "group", "g1")  # type: ignore[misc]


@pytest.mark.parametrize("field", ["realm", "kind", "id"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_principal_rejects_blank_identity_fields(field: str, blank: str):
    values = {"realm": REALM, "kind": "group", "id": "g1", field: blank}
    with pytest.raises(ValueError):
        SourcePrincipal(**values)


def test_principal_rejects_non_string_identity_fields():
    with pytest.raises(ValueError):
        SourcePrincipal(realm=REALM, kind="group", id=7)  # type: ignore[arg-type]


def test_principal_to_dict_carries_realm_and_omits_missing_display_name():
    assert _group("g1").to_dict() == {"realm": REALM, "kind": "group", "id": "g1"}
    assert _group("g1", "Recruiting").to_dict() == {
        "realm": REALM,
        "kind": "group",
        "id": "g1",
        "display_name": "Recruiting",
    }


def test_principal_dict_round_trip_keeps_display_name():
    principal = _group("g1", "Recruiting")
    restored = SourcePrincipal.from_dict(principal.to_dict())
    assert restored == principal
    assert restored.display_name == "Recruiting"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "group", "id": "g1"},
        {"realm": REALM, "id": "g1"},
        {"realm": REALM, "kind": "group"},
        {"realm": "", "kind": "group", "id": "g1"},
        {"realm": REALM, "kind": " ", "id": "g1"},
        {"realm": REALM, "kind": "group", "id": ""},
        {"realm": None, "kind": "group", "id": "g1"},
        {"realm": REALM, "kind": "group", "id": 7},
        {"realm": REALM, "kind": "group", "id": "g1", "display_name": 5},
        {},
    ],
)
def test_principal_from_dict_raises_value_error_for_missing_or_bad_fields(
    payload: dict[str, Any],
):
    with pytest.raises(ValueError):
        SourcePrincipal.from_dict(payload)


@pytest.mark.parametrize("payload", [None, "group:g1", ["group", "g1"], 3])
def test_principal_from_dict_rejects_non_mappings(payload: Any):
    with pytest.raises(ValueError):
        SourcePrincipal.from_dict(payload)


# --- SourceAcl construction --------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: SourceAcl.inherited(realm=REALM, scope=""),
        lambda: SourceAcl.inherited(realm="", scope="jira-project:CPM"),
        lambda: SourceAcl.restricted(realm=REALM, scope="jira-project:CPM", levels=[], reason="x"),
        lambda: SourceAcl.restricted(
            realm=REALM, scope="jira-project:CPM", levels=[[]], reason="x"
        ),
        lambda: SourceAcl.restricted(
            realm=REALM,
            scope="jira-project:CPM",
            levels=[[SourcePrincipal(realm="entra:t1", kind="group", id="g")]],
            reason="x",
        ),
    ],
)
def test_invalid_acls_are_rejected(build: Any):
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: SourceAcl.inherited(realm="  ", scope=SCOPE),
        lambda: SourceAcl.inherited(realm=REALM, scope="   "),
        lambda: SourceAcl.restricted(realm="", scope=SCOPE, levels=[[_group("g1")]], reason="x"),
        lambda: SourceAcl.restricted(realm=REALM, scope="", levels=[[_group("g1")]], reason="x"),
        lambda: SourceAcl.restricted(
            realm=REALM, scope=SCOPE, levels=[[_group("g1")], []], reason="x"
        ),
        lambda: SourceAcl(realm=REALM, scope=SCOPE, status="open"),  # type: ignore[arg-type]
        lambda: SourceAcl(realm=REALM, scope=SCOPE, status="inherited", levels=((_group("g1"),),)),
        lambda: SourceAcl(realm=REALM, scope=SCOPE, status="unknown", levels=((_group("g1"),),)),
        lambda: SourceAcl(realm=REALM, scope=SCOPE, status="restricted"),
        lambda: SourceAcl(
            realm=REALM,
            scope=SCOPE,
            status="restricted",
            levels=(("g1",),),  # type: ignore[arg-type]
        ),
    ],
)
def test_more_invalid_acls_are_rejected(build: Any):
    with pytest.raises(ValueError):
        build()


def test_every_principal_in_every_level_must_share_the_acl_realm():
    stray = SourcePrincipal(realm="entra:t1", kind="group", id="g2")
    with pytest.raises(ValueError, match="realm"):
        SourceAcl.restricted(
            realm=REALM, scope=SCOPE, levels=[[_group("g1")], [_group("g3"), stray]], reason="x"
        )


def test_acls_are_keyword_only():
    with pytest.raises(TypeError):
        SourceAcl(REALM, SCOPE, "inherited")  # type: ignore[misc]


def test_inherited_acl_has_no_levels_or_reason():
    acl = SourceAcl.inherited(realm=REALM, scope=SCOPE)
    assert (acl.realm, acl.scope, acl.status) == (REALM, SCOPE, "inherited")
    assert acl.levels == ()
    assert acl.reason == ""


def test_restricted_acl_freezes_levels_into_tuples():
    acl = _restricted()
    assert acl.status == "restricted"
    assert acl.levels == ((_group("g1"), _user("u1")), (_group("g2"),))
    assert all(isinstance(level, tuple) for level in acl.levels)
    assert isinstance(acl.levels, tuple)
    assert acl.reason == "issue_security_level"
    assert hash(acl) == hash(_restricted())


def test_restricted_accepts_generators_of_principals():
    acl = SourceAcl.restricted(
        realm=REALM,
        scope=SCOPE,
        levels=((principal for principal in (_group("g1"), _group("g2"))),),
        reason="x",
    )
    assert acl.levels == ((_group("g1"), _group("g2")),)


def test_direct_construction_normalises_list_levels_to_tuples():
    acl = SourceAcl(
        realm=REALM,
        scope=SCOPE,
        status="restricted",
        levels=[[_group("g1")]],  # type: ignore[arg-type]
        reason="x",
    )
    assert acl.levels == ((_group("g1"),),)
    assert isinstance(acl.levels[0], tuple)


def test_unknown_may_omit_realm_and_scope():
    assert SourceAcl.unknown(reason="issue_project_unreadable").status == "unknown"


def test_unknown_defaults_are_blank_and_levelless():
    acl = SourceAcl.unknown(reason="issue_project_unreadable")
    assert (acl.realm, acl.scope) == ("", "")
    assert acl.levels == ()
    assert acl.reason == "issue_project_unreadable"


def test_unknown_may_carry_realm_and_scope():
    acl = SourceAcl.unknown(reason="r", realm=REALM, scope=SCOPE)
    assert (acl.realm, acl.scope, acl.status) == (REALM, SCOPE, "unknown")


# --- SourceAcl serialisation -------------------------------------------------


def test_dict_round_trip():
    acl = SourceAcl.restricted(
        realm=REALM,
        scope="jira-project:CPM",
        levels=[[_group("g1", "Recruiting")]],
        reason="issue_security_level",
    )
    assert SourceAcl.from_dict(acl.to_dict()) == acl


def test_dict_round_trip_preserves_multiple_levels_and_order():
    acl = _restricted()
    restored = SourceAcl.from_dict(acl.to_dict())
    assert restored == acl
    assert [[p.key for p in level] for level in restored.levels] == [
        [f"{REALM}|group:g1", f"{REALM}|user:u1"],
        [f"{REALM}|group:g2"],
    ]
    assert restored.levels[0][0].display_name == "Recruiting"


@pytest.mark.parametrize(
    "acl",
    [
        SourceAcl.inherited(realm=REALM, scope=SCOPE),
        SourceAcl.unknown(reason="issue_project_unreadable"),
        SourceAcl.unknown(reason="r", realm=REALM, scope=SCOPE),
    ],
)
def test_dict_round_trip_for_inherited_and_unknown(acl: SourceAcl):
    assert SourceAcl.from_dict(acl.to_dict()) == acl


def test_to_dict_shape_is_json_compatible():
    payload = _restricted().to_dict()
    assert payload == {
        "realm": REALM,
        "scope": SCOPE,
        "status": "restricted",
        "reason": "issue_security_level",
        "levels": [
            [
                {"realm": REALM, "kind": "group", "id": "g1", "display_name": "Recruiting"},
                {"realm": REALM, "kind": "user", "id": "u1"},
            ],
            [{"realm": REALM, "kind": "group", "id": "g2"}],
        ],
    }
    assert json.loads(json.dumps(payload)) == payload


def test_from_dict_reads_the_frozen_containers_metadata_is_stored_in():
    acl = _restricted()
    frozen = _deep_freeze(acl.to_dict())
    assert SourceAcl.from_dict(frozen) == acl


@pytest.mark.parametrize(
    "legacy",
    [
        {"status": "inherited", "reason": "", "levels": []},
        {"status": "restricted", "reason": "r", "levels": [[{"kind": "group", "id": "g1"}]]},
        {"status": "unknown", "reason": "r"},
    ],
)
def test_legacy_payloads_without_realm_or_scope_are_rejected(legacy: dict[str, Any]):
    with pytest.raises(ValueError):
        SourceAcl.from_dict(legacy)


@pytest.mark.parametrize(
    "legacy",
    [
        {"status": "unknown", "reason": "r", "levels": []},
        {"status": "unknown", "reason": "r"},
        {"status": "unknown", "reason": "r", "realm": ""},
        {"status": "unknown", "reason": "r", "scope": ""},
    ],
)
def test_legacy_unknown_payloads_missing_realm_or_scope_keys_are_rejected(
    legacy: dict[str, Any],
):
    with pytest.raises(ValueError):
        SourceAcl.from_dict(legacy)


def test_unknown_payload_with_blank_realm_and_scope_decodes_as_unknown():
    acl = SourceAcl.from_dict({"status": "unknown", "reason": "r", "realm": "", "scope": ""})
    assert acl == SourceAcl.unknown(reason="r")
    assert acl.status == "unknown"
    assert (acl.realm, acl.scope, acl.levels) == ("", "", ())


@pytest.mark.parametrize("status", [[], {}, ["inherited"], 3, None])
def test_unhashable_or_non_string_status_is_a_value_error(status: Any):
    with pytest.raises(ValueError):
        SourceAcl(realm=REALM, scope=SCOPE, status=status)


def test_legacy_restricted_payload_never_decodes_to_an_inherited_acl():
    legacy = {"status": "restricted", "reason": "r", "levels": [[{"kind": "group", "id": "g1"}]]}
    with pytest.raises(ValueError):
        SourceAcl.from_dict(legacy)
    with pytest.raises(ValueError):
        acl_from_metadata({SOURCE_ACL_METADATA_KEY: legacy})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"realm": REALM, "scope": SCOPE},
        {"realm": REALM, "scope": SCOPE, "status": ""},
        {"realm": REALM, "scope": SCOPE, "status": None},
        {"realm": REALM, "scope": SCOPE, "status": "open"},
        {"realm": REALM, "scope": SCOPE, "status": 3},
        {"scope": SCOPE, "status": "inherited"},
        {"realm": REALM, "status": "inherited"},
        {"realm": "", "scope": SCOPE, "status": "inherited"},
        {"realm": REALM, "scope": "  ", "status": "inherited"},
        {"realm": None, "scope": SCOPE, "status": "inherited"},
        {"realm": REALM, "scope": 5, "status": "inherited"},
        {"realm": REALM, "scope": SCOPE, "status": "restricted"},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": []},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": [[]]},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": "g1"},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": {"g1": 1}},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": ["g1"]},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": [["g1"]]},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": [None]},
        {"realm": REALM, "scope": SCOPE, "status": "restricted", "levels": [[{"kind": "group"}]]},
        {
            "realm": REALM,
            "scope": SCOPE,
            "status": "restricted",
            "levels": [[{"kind": "group", "id": "g1"}]],
        },
        {
            "realm": REALM,
            "scope": SCOPE,
            "status": "restricted",
            "levels": [[{"realm": "entra:t1", "kind": "group", "id": "g1"}]],
        },
        {
            "realm": REALM,
            "scope": SCOPE,
            "status": "inherited",
            "levels": [[{"realm": REALM, "kind": "group", "id": "g1"}]],
        },
        {"realm": REALM, "scope": SCOPE, "status": "inherited", "reason": ["x"]},
    ],
)
def test_from_dict_raises_value_error_for_missing_or_malformed_fields(payload: dict[str, Any]):
    with pytest.raises(ValueError):
        SourceAcl.from_dict(payload)


@pytest.mark.parametrize("payload", [None, "inherited", ["inherited"], 3])
def test_from_dict_rejects_non_mappings(payload: Any):
    with pytest.raises(ValueError):
        SourceAcl.from_dict(payload)


# --- metadata helpers --------------------------------------------------------


def test_acl_metadata_records_inherited_acls():
    acl = SourceAcl.inherited(realm=REALM, scope="jira-project:CPM")
    assert acl_metadata(acl) == {"source_acl": acl.to_dict()}
    assert acl_from_metadata(acl_metadata(acl)) == acl
    assert acl_from_metadata({}) is None


def test_metadata_key_is_source_acl():
    assert SOURCE_ACL_METADATA_KEY == "source_acl"
    assert list(acl_metadata(_restricted())) == ["source_acl"]


@pytest.mark.parametrize(
    "acl",
    [
        _restricted(),
        SourceAcl.unknown(reason="issue_project_unreadable"),
        SourceAcl.inherited(realm=REALM, scope=SCOPE),
    ],
)
def test_acl_metadata_round_trips_every_status(acl: SourceAcl):
    metadata = acl_metadata(acl)
    assert metadata == {SOURCE_ACL_METADATA_KEY: acl.to_dict()}
    assert acl_from_metadata(metadata) == acl
    assert acl_from_metadata(_deep_freeze(metadata)) == acl


def test_acl_from_metadata_ignores_unrelated_keys():
    assert acl_from_metadata({"title": "x", "labels": ["a"]}) is None
    acl = SourceAcl.inherited(realm=REALM, scope=SCOPE)
    assert acl_from_metadata({"title": "x", **acl_metadata(acl)}) == acl


@pytest.mark.parametrize(
    "stored",
    [None, "", "inherited", [], 7, {}, {"status": "inherited"}, {"status": "restricted"}],
)
def test_acl_from_metadata_raises_when_present_but_malformed(stored: Any):
    with pytest.raises(ValueError):
        acl_from_metadata({SOURCE_ACL_METADATA_KEY: stored})


# --- package exports ---------------------------------------------------------


def test_names_are_exported_from_the_sources_package():
    import harborrag_core.sources as sources

    for name in (
        "SOURCE_ACL_METADATA_KEY",
        "AclStatus",
        "SourceAcl",
        "SourcePrincipal",
        "acl_from_metadata",
        "acl_metadata",
    ):
        assert name in sources.__all__
        assert hasattr(sources, name)
