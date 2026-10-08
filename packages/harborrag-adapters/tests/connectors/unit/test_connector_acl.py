"""Source ACL evidence and the restricted-content gate shared by connectors."""

from __future__ import annotations

import pytest

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

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

ADA = SourcePrincipal("user", "acc-ada", "Ada")
ENG = SourcePrincipal("group", "grp-eng")


def test_acl_shape_is_validated():
    with pytest.raises(ValueError, match="restricted"):
        SourceAcl("restricted")
    with pytest.raises(ValueError, match="inherited"):
        SourceAcl("inherited", levels=((ADA,),))
    with pytest.raises(ValueError, match="non-empty"):
        SourceAcl.restricted([()], reason="x")
    with pytest.raises(ValueError, match="status"):
        SourceAcl("public")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="principal"):
        SourcePrincipal("user", " ")


def test_principal_key_and_payload():
    assert ADA.key == "user:acc-ada"
    assert ADA.to_dict() == {"kind": "user", "id": "acc-ada", "display_name": "Ada"}
    assert ENG.to_dict() == {"kind": "group", "id": "grp-eng"}


def test_acl_round_trips_through_metadata():
    acl = SourceAcl.restricted([[ADA, ENG], [ENG]], reason="confluence_read_restriction")

    payload = acl_metadata(acl)[SOURCE_ACL_METADATA_KEY]

    assert payload["status"] == "restricted"
    assert payload["levels"][0][0] == {"kind": "user", "id": "acc-ada", "display_name": "Ada"}
    assert SourceAcl.from_dict(payload) == acl


def test_inherited_acl_adds_no_metadata():
    assert acl_metadata(SourceAcl.inherited()) == {}
    assert acl_metadata(SourceAcl.unknown("u"))[SOURCE_ACL_METADATA_KEY]["status"] == "unknown"


def test_skip_policy_withholds_restricted_and_unknown_items_with_reasons():
    gate = RestrictedContentGate("skip")

    assert gate.admit(SourceAcl.inherited(), path="a")
    assert not gate.admit(SourceAcl.restricted([[ADA]], reason="issue_security_level"), path="b")
    assert not gate.admit(SourceAcl.unknown("page_read_restrictions_unavailable"), path="c")

    assert [(skip.path, skip.reason) for skip in gate.skipped] == [
        ("b", SKIP_REASON_ACCESS_RESTRICTED),
        ("c", SKIP_REASON_ACCESS_UNRESOLVED),
    ]
    assert "issue_security_level" in gate.skipped[0].detail
    gate.reset()
    assert gate.skipped == ()


def test_include_policy_admits_restricted_but_never_unknown():
    gate = RestrictedContentGate("include")

    assert gate.admit(SourceAcl.restricted([[ADA]], reason="r"), path="b")
    assert not gate.admit(SourceAcl.unknown("u"), path="c")
    assert [skip.reason for skip in gate.skipped] == [SKIP_REASON_ACCESS_UNRESOLVED]


def test_require_raises_for_items_discovery_would_withhold():
    gate = RestrictedContentGate("skip")

    gate.require(SourceAcl.inherited(), path="a")
    with pytest.raises(SourceRestrictedError, match="b"):
        gate.require(SourceAcl.restricted([[ADA]], reason="r"), path="b")
    with pytest.raises(SourceRestrictedError, match="c"):
        RestrictedContentGate("include").require(SourceAcl.unknown("u"), path="c")
    assert issubclass(SourceRestrictedError, DocumentProcessingError)
    assert gate.skipped == ()


def test_policy_validation():
    assert validate_restricted_content_policy("include") == "include"
    with pytest.raises(ValueError, match="restricted_content"):
        validate_restricted_content_policy("open")
