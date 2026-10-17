from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Literal

import pytest

from harborrag_core.sources.acl import SourceAcl, SourcePrincipal
from harborrag_engine.security.admit import StageDecision, stage_decision

REALM = "atlassian:acme.atlassian.net"
SCOPE = "jira-project:CPM"

NONE_ACL = None
INHERITED = SourceAcl.inherited(realm=REALM, scope=SCOPE)
UNKNOWN = SourceAcl.unknown(reason="the source did not answer")
RESTRICTED = SourceAcl.restricted(
    realm=REALM,
    scope=SCOPE,
    levels=[[SourcePrincipal(realm=REALM, kind="group", id="g1")]],
    reason="page restriction",
)

Content = Literal["skip", "include"]
Mode = Literal["source_acl", "tenant_shared"]


@pytest.mark.parametrize(
    ("acl", "restricted_content", "corpus_mode", "action", "reason"),
    [
        # No ACL evidence at all: withheld whatever the policy.
        (NONE_ACL, "skip", "source_acl", "withhold", "acl_missing"),
        (NONE_ACL, "include", "source_acl", "withhold", "acl_missing"),
        (NONE_ACL, "skip", "tenant_shared", "withhold", "acl_missing"),
        (NONE_ACL, "include", "tenant_shared", "withhold", "acl_missing"),
        # An ACL the connector could not determine: withheld whatever the policy.
        (UNKNOWN, "skip", "source_acl", "withhold", "acl_unknown"),
        (UNKNOWN, "include", "source_acl", "withhold", "acl_unknown"),
        (UNKNOWN, "skip", "tenant_shared", "withhold", "acl_unknown"),
        (UNKNOWN, "include", "tenant_shared", "withhold", "acl_unknown"),
        # Inherited: exactly the readers of the scope, so always staged.
        (INHERITED, "skip", "source_acl", "stage", "inherited"),
        (INHERITED, "include", "source_acl", "stage", "inherited"),
        (INHERITED, "skip", "tenant_shared", "stage", "inherited"),
        (INHERITED, "include", "tenant_shared", "stage", "inherited"),
        # Restricted: staged only when the policy includes it in a source-ACL corpus.
        (RESTRICTED, "skip", "tenant_shared", "withhold", "restricted_in_shared_corpus"),
        (RESTRICTED, "include", "tenant_shared", "withhold", "restricted_in_shared_corpus"),
        (RESTRICTED, "skip", "source_acl", "withhold", "restricted_skip"),
        (RESTRICTED, "include", "source_acl", "stage", "restricted_include"),
    ],
)
def test_stage_decision_table(
    acl: SourceAcl | None,
    restricted_content: Content,
    corpus_mode: Mode,
    action: str,
    reason: str,
) -> None:
    decision = stage_decision(acl, restricted_content=restricted_content, corpus_mode=corpus_mode)

    assert decision == StageDecision(action=action, reason=reason)  # type: ignore[arg-type]


def test_a_stage_decision_is_immutable() -> None:
    decision = stage_decision(INHERITED, restricted_content="skip", corpus_mode="source_acl")

    with pytest.raises(FrozenInstanceError):
        decision.action = "withhold"  # type: ignore[misc]


def test_an_unrecognised_corpus_mode_withholds_a_restricted_item() -> None:
    decision = stage_decision(
        RESTRICTED,
        restricted_content="include",
        corpus_mode="somewhere_else",  # type: ignore[arg-type]
    )

    assert decision == StageDecision(action="withhold", reason="restricted_in_shared_corpus")


def test_an_unrecognised_restricted_content_policy_withholds_a_restricted_item() -> None:
    decision = stage_decision(
        RESTRICTED,
        restricted_content="everything",  # type: ignore[arg-type]
        corpus_mode="source_acl",
    )

    assert decision == StageDecision(action="withhold", reason="restricted_skip")


def test_an_acl_with_an_unrecognised_status_is_withheld() -> None:
    # ``SourceAcl`` refuses such a status at construction; skip that to prove that a value
    # which got through anyway is withheld and never staged.
    acl = SourceAcl.unknown(reason="forged")
    object.__setattr__(acl, "status", "everyone")

    decision = stage_decision(acl, restricted_content="include", corpus_mode="source_acl")

    assert decision == StageDecision(action="withhold", reason="acl_unknown")
