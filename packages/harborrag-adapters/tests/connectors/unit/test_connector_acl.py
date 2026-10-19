"""Source ACL evidence and the restricted-content gate shared by connectors."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors import acl as connector_acl
from harborrag_adapters.connectors.acl import (
    SOURCE_ACL_METADATA_KEY,
    RestrictedContentGate,
    SourceAcl,
    SourcePrincipal,
    acl_metadata,
    validate_restricted_content_policy,
)
from harborrag_adapters.connectors.exceptions import (
    DocumentProcessingError,
    SourceRestrictedError,
)
from harborrag_adapters.connectors.schemas import (
    SKIP_REASON_ACCESS_RESTRICTED,
    SKIP_REASON_ACCESS_UNRESOLVED,
)
from harborrag_core.sources import acl as core_acl

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

R = "atlassian:acme.atlassian.net"
S = "jira-project:ENG"
ADA = SourcePrincipal(realm=R, kind="user", id="acc-ada", display_name="Ada")
ENG = SourcePrincipal(realm=R, kind="group", id="grp-eng")


def inherited() -> SourceAcl:
    return SourceAcl.inherited(realm=R, scope=S)


def restricted(*levels: tuple[SourcePrincipal, ...], reason: str = "r") -> SourceAcl:
    return SourceAcl.restricted(realm=R, scope=S, levels=levels, reason=reason)


def unknown(reason: str = "u") -> SourceAcl:
    return SourceAcl.unknown(reason=reason, realm=R, scope=S)


def test_connector_acl_module_reexports_the_core_evidence_types():
    assert connector_acl.SourceAcl is core_acl.SourceAcl
    assert connector_acl.SourcePrincipal is core_acl.SourcePrincipal
    assert connector_acl.acl_metadata is core_acl.acl_metadata
    assert connector_acl.AclStatus is core_acl.AclStatus
    assert connector_acl.SOURCE_ACL_METADATA_KEY == core_acl.SOURCE_ACL_METADATA_KEY


def test_acl_shape_is_validated():
    with pytest.raises(ValueError, match="restricted"):
        SourceAcl(realm=R, scope=S, status="restricted")
    with pytest.raises(ValueError, match="inherited"):
        SourceAcl(realm=R, scope=S, status="inherited", levels=((ADA,),))
    with pytest.raises(ValueError, match="non-empty"):
        SourceAcl.restricted(realm=R, scope=S, levels=[()], reason="x")
    with pytest.raises(ValueError, match="status"):
        SourceAcl(realm=R, scope=S, status="public")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="principal"):
        SourcePrincipal(realm=R, kind="user", id=" ")


def test_principals_outside_the_acl_realm_are_rejected():
    stranger = SourcePrincipal(realm="atlassian:other.atlassian.net", kind="user", id="acc-ada")

    with pytest.raises(ValueError, match="realm"):
        restricted((ADA, stranger))


def test_principal_key_and_payload():
    assert ADA.key == f"{R}|user:acc-ada"
    assert ADA.to_dict() == {
        "realm": R,
        "kind": "user",
        "id": "acc-ada",
        "display_name": "Ada",
    }
    assert ENG.to_dict() == {"realm": R, "kind": "group", "id": "grp-eng"}


def test_acl_round_trips_through_metadata():
    acl = restricted((ADA, ENG), (ENG,), reason="confluence_read_restriction")

    payload = acl_metadata(acl)[SOURCE_ACL_METADATA_KEY]

    assert payload["status"] == "restricted"
    assert payload["realm"] == R
    assert payload["scope"] == S
    assert payload["levels"][0][0] == {
        "realm": R,
        "kind": "user",
        "id": "acc-ada",
        "display_name": "Ada",
    }
    assert SourceAcl.from_dict(payload) == acl


def test_inherited_acl_is_recorded_in_metadata_with_its_realm_and_scope():
    assert acl_metadata(inherited()) == {
        SOURCE_ACL_METADATA_KEY: {
            "realm": R,
            "scope": S,
            "status": "inherited",
            "reason": "",
            "levels": [],
        }
    }
    assert acl_metadata(unknown())[SOURCE_ACL_METADATA_KEY]["status"] == "unknown"


def test_skip_policy_withholds_restricted_and_unknown_items_with_reasons():
    gate = RestrictedContentGate("skip")

    assert gate.admit(inherited(), path="a")
    assert not gate.admit(restricted((ADA,), reason="issue_security_level"), path="b")
    assert not gate.admit(unknown("page_read_restrictions_unavailable"), path="c")

    assert [(skip.path, skip.reason) for skip in gate.skipped] == [
        ("b", SKIP_REASON_ACCESS_RESTRICTED),
        ("c", SKIP_REASON_ACCESS_UNRESOLVED),
    ]
    assert "issue_security_level" in gate.skipped[0].detail
    assert "graph" in gate.skipped[0].detail
    assert "title and attributes" in gate.skipped[0].detail
    gate.reset()
    assert gate.skipped == ()


def test_include_policy_admits_restricted_but_never_unknown():
    gate = RestrictedContentGate("include")

    assert gate.admit(restricted((ADA,)), path="b")
    assert not gate.admit(unknown(), path="c")
    assert [skip.reason for skip in gate.skipped] == [SKIP_REASON_ACCESS_UNRESOLVED]


def test_require_raises_for_items_discovery_would_withhold():
    gate = RestrictedContentGate("skip")

    gate.require(inherited(), path="a")
    with pytest.raises(SourceRestrictedError, match="b"):
        gate.require(restricted((ADA,)), path="b")
    with pytest.raises(SourceRestrictedError, match="c"):
        RestrictedContentGate("include").require(unknown(), path="c")
    assert issubclass(SourceRestrictedError, DocumentProcessingError)
    assert gate.skipped == ()


def test_policy_validation():
    assert validate_restricted_content_policy("include") == "include"
    with pytest.raises(ValueError, match="restricted_content"):
        validate_restricted_content_policy("open")
