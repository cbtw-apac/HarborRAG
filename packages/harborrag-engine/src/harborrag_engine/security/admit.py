"""Whether a source item enters the corpus at all, decided from its ACL evidence.

This is the staging gate: it runs before an item is stored, on the ACL a connector
captured for it. Only an ACL the corpus can enforce lets an item through.

- No ACL, or an ``unknown`` one, is withheld: access cannot be evaluated, so the
  item is never stored under a guess.
- ``inherited`` is staged: exactly the readers of the item's scope may read it.
- ``restricted`` is staged only when the policy includes restricted content and the
  corpus enforces source ACLs. A shared corpus has no per-reader enforcement, so it
  never holds restricted content, whatever the policy says.

Only a positive match stages. The two policy arguments are checked first, before
the ACL is looked at: a value the types do not allow (``unrecognised_policy``)
withholds every item, whatever its ACL, because a gate that cannot tell what it was
asked to enforce must not stage anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from harborrag_core.sources.acl import SourceAcl

RestrictedContent = Literal["skip", "include"]
CorpusMode = Literal["source_acl", "tenant_shared"]

_RESTRICTED_CONTENT: frozenset[str] = frozenset(get_args(RestrictedContent))
_CORPUS_MODES: frozenset[str] = frozenset(get_args(CorpusMode))


@dataclass(frozen=True, slots=True)
class StageDecision:
    """Whether to ``stage`` an item or ``withhold`` it, and the reason why."""

    action: Literal["stage", "withhold"]
    reason: str


def stage_decision(
    acl: SourceAcl | None,
    *,
    restricted_content: Literal["skip", "include"],
    corpus_mode: Literal["source_acl", "tenant_shared"],
) -> StageDecision:
    """Decide whether an item with ``acl`` may be staged into the corpus.

    ``acl`` is ``None`` when the connector reported none. ``restricted_content`` is
    the policy for restricted items, and ``corpus_mode`` says how the target corpus
    enforces access. Either one outside its allowed values withholds the item with
    the reason ``unrecognised_policy``.
    """

    if (
        not isinstance(restricted_content, str)
        or restricted_content not in _RESTRICTED_CONTENT
        or not isinstance(corpus_mode, str)
        or corpus_mode not in _CORPUS_MODES
    ):
        return StageDecision("withhold", "unrecognised_policy")
    if acl is None:
        return StageDecision("withhold", "acl_missing")
    if acl.status == "inherited":
        return StageDecision("stage", "inherited")
    if acl.status != "restricted":
        # ``unknown``, and anything the status type does not name.
        return StageDecision("withhold", "acl_unknown")
    if corpus_mode == "tenant_shared":
        return StageDecision("withhold", "restricted_in_shared_corpus")
    if restricted_content == "skip":
        return StageDecision("withhold", "restricted_skip")
    return StageDecision("stage", "restricted_include")
