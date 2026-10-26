"""Source-native links between source entities, with one owner per logical fact.

Most source links are declared by *both* ends: Jira returns an issue link on each
issue (``blocks`` on one, ``is_blocked_by`` on the other), an attachment says
``attached_to`` while its container lists it with ``has_attachment``, a subtask names
its parent while the parent lists its subtasks. Every declaration used to become an
edge owned by the declaring document's version, and ``relation_id`` hashes that
version, so each pair carried two parallel edges -- measured live, every one of 446
issue -> attachment pairs.

``_PREDICATES`` names, per declared predicate, which end owns the edge. When the far
end is published (resolved), only the owner asserts the edge -- but the non-owner
steps aside only when it can see the owner will: an attachment's and a subtask's own
projections always draw their edge, and for an issue link the owner must declare the
reciprocal predicate back at this document (read from its descriptor). A link only
one end declares therefore keeps its edge. When the far end is not published,
whichever end declared the link asserts it against an external stub, because the
owner may never be ingested -- an issue linked from a project this source does not
read is the common case.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import DocumentRelation
from harborrag_core.ingestion import GraphNodeRecord

from .graph_models import GraphDocumentTarget, UnresolvedGraphRelation
from .graph_state import GraphProjectionState, GraphRelationSpec
from .source_projector_support import (
    relation_entity_type,
    source_provider_id,
    target_connector_type,
)


class _Owner(Enum):
    """Which end of a resolved link asserts its edge."""

    DECLARER = "declarer"
    # The far end's own projection draws the edge from its own metadata, whatever it
    # declares: an attachment from its parent, a subtask from its `parent` field.
    FAR_END_PROJECTION = "far_end_projection"
    # The far end owns the edge when it declares ``reciprocal`` back at this document.
    FAR_END = "far_end"
    # Symmetric links: the end with the lexicographically smaller source item id, when
    # that end declares ``reciprocal`` back.
    SMALLER_ID = "smaller_id"


@dataclass(frozen=True, slots=True)
class _Predicate:
    relation_type: RelationType
    # The edge runs far end -> declarer, e.g. `attached_to` is container -> attachment.
    reverse: bool
    owner: _Owner
    reciprocal: str | None = None


_PREDICATES: Mapping[str, _Predicate] = {
    "has_attachment": _Predicate(RelationType.HAS_ATTACHMENT, False, _Owner.FAR_END_PROJECTION),
    "attached_to": _Predicate(RelationType.HAS_ATTACHMENT, True, _Owner.DECLARER),
    "child_of": _Predicate(RelationType.PARENT_OF, True, _Owner.DECLARER),
    "parent_of": _Predicate(RelationType.PARENT_OF, False, _Owner.FAR_END_PROJECTION),
    # The outward side of a directed issue link owns it.
    "blocks": _Predicate(RelationType.BLOCKS, False, _Owner.DECLARER),
    "is_blocked_by": _Predicate(RelationType.BLOCKS, True, _Owner.FAR_END, "blocks"),
    "duplicates": _Predicate(RelationType.DUPLICATES, False, _Owner.DECLARER),
    "is_duplicated_by": _Predicate(RelationType.DUPLICATES, True, _Owner.FAR_END, "duplicates"),
    "relates_to": _Predicate(RelationType.RELATES_TO, False, _Owner.SMALLER_ID, "relates_to"),
    # Declared by one end only.
    "links_to": _Predicate(RelationType.LINKS_TO, False, _Owner.DECLARER),
    # Was absent, so a transclusion produced no edge, no placeholder and no unresolved
    # record -- dropped without trace. Measured on a live space: 9 include macros, 0 edges.
    "includes": _Predicate(RelationType.INCLUDES, False, _Owner.DECLARER),
}


def normalized_predicate(predicate: str) -> str:
    return predicate.strip().lower().replace("-", "_").replace(" ", "_")


class SourceRelationProjector:
    """Project source-native links between stable source entities."""

    def __init__(
        self,
        *,
        state: GraphProjectionState,
        current_source_item: GraphNodeRecord,
        resolved_targets: Mapping[str, GraphDocumentTarget],
        external_stubs: bool = False,
    ) -> None:
        self._state = state
        self._current = current_source_item
        self._targets = resolved_targets
        self._external_stubs = external_stubs

    def project(self, relations: list[DocumentRelation]) -> tuple[UnresolvedGraphRelation, ...]:
        unresolved: dict[tuple[str, str], UnresolvedGraphRelation] = {}
        # A provider projector may already have asserted the same pair in this batch
        # (an attachment's parent, a subtask's parent). Those differ only by
        # relation_id, and MERGE keys on relation_id, so both would survive as
        # parallel edges.
        seen: set[tuple[RelationType, str, str]] = {
            (record.relation_type, record.source_node_key, record.target_node_key)
            for record in self._state.relations.values()
        }
        for relation in relations:
            predicate = normalized_predicate(relation.predicate)
            rule = _PREDICATES.get(predicate)
            if rule is None:
                continue
            resolved = self._targets.get(relation.target_id)
            if resolved is None:
                target_connector = target_connector_type(
                    self._state.context.connector_type.value, relation.target_id
                )
                unresolved.setdefault(
                    (relation.target_id, predicate),
                    UnresolvedGraphRelation(
                        relation_type=rule.relation_type.value,
                        target_source_item_id=relation.target_id,
                        predicate=predicate,
                        target_connector_type=target_connector,
                    ),
                )
                if not self._external_stubs:
                    continue
                target = self._stub(relation, rule, target_connector, seen)
                if target is None:
                    continue
            else:
                if not self._declarer_owns(rule, resolved):
                    continue
                target = self._resolved(relation, rule, resolved)
            source, destination = (
                (target, self._current) if rule.reverse else (self._current, target)
            )
            key = (rule.relation_type, source.node_key, destination.node_key)
            if key in seen:
                continue
            seen.add(key)
            supplied_version = relation.metadata.get("source_relation_version")
            self._state.relation(
                GraphRelationSpec(
                    relation_type=rule.relation_type,
                    source=source,
                    target=destination,
                    source_explicit=True,
                    attributes={"source_relation": True},
                    source_relation_version=(
                        str(supplied_version)
                        if supplied_version is not None and str(supplied_version).strip()
                        else None
                    ),
                )
            )
        return tuple(unresolved.values())

    def _declarer_owns(self, rule: _Predicate, resolved: GraphDocumentTarget) -> bool:
        if rule.owner is _Owner.DECLARER:
            return True
        if rule.owner is _Owner.FAR_END_PROJECTION:
            return False
        here = self._state.context.source_item_id
        if rule.owner is _Owner.SMALLER_ID and here < resolved.source_item_id:
            return True
        # Step aside only for an owner that states the link back at this document;
        # a link one end declares keeps its only edge.
        reciprocal = (rule.reciprocal or "", here)
        return reciprocal not in {
            (normalized_predicate(predicate), target_id)
            for predicate, target_id in resolved.declared_relations
        }

    def _resolved(
        self,
        relation: DocumentRelation,
        rule: _Predicate,
        resolved: GraphDocumentTarget,
    ) -> GraphNodeRecord:
        raw_target_id = resolved.source_item_id
        # The far end's own connector, not the declaring document's: both the
        # entity type and the provider-id reduction below feed the target's
        # node key, and keying a Confluence page as a Jira issue puts it
        # somewhere its own projection will never look.
        target_connector = target_connector_type(
            self._state.context.connector_type.value,
            raw_target_id,
        )
        target_id = source_provider_id(target_connector, raw_target_id)
        # Every node this projector creates stands in for something another document
        # owns: it carries no provider attributes of its own, so it must never
        # overwrite the concrete projection (adapter writes placeholders ON CREATE
        # SET only). The resolved target may also supply a better stub title.
        return self._state.source_node(
            relation_entity_type(
                target_connector,
                rule.relation_type,
                relation.target_type,
                reverse=rule.reverse,
            ),
            target_id,
            title=self._target_title(relation, resolved, target_id),
            source_scope_id=resolved.source_scope_id,
            attributes={"placeholder": True},
        )

    def _stub(
        self,
        relation: DocumentRelation,
        rule: _Predicate,
        target_connector: str,
        seen: set[tuple[RelationType, str, str]],
    ) -> GraphNodeRecord | None:
        """The external stand-in for an unresolved target, unless one already exists.

        A connector projector can already have placed the far end in this scope (a
        subtask's parent, an attachment's parent): that placeholder *is* the stand-in,
        and a second, scope-free node for the same item would split it in two.
        """

        entity_type = relation_entity_type(
            target_connector,
            rule.relation_type,
            relation.target_type,
            reverse=rule.reverse,
        )
        provider_id = source_provider_id(target_connector, relation.target_id)
        scoped = self._state.scoped_source_node_key(entity_type, provider_id)
        current = self._current.node_key
        existing = (scoped, current) if rule.reverse else (current, scoped)
        if (rule.relation_type, *existing) in seen:
            return None
        declaring_connector = self._state.context.connector_type.value
        return self._state.external_source_node(
            entity_type,
            connector_type=target_connector,
            # A cross-connector target is resolved without a connection, so its stub
            # cannot be keyed by one either.
            connection_id=(
                self._state.context.connection_id if target_connector == declaring_connector else ""
            ),
            source_item_id=relation.target_id,
            provider_id=provider_id,
        )

    @staticmethod
    def _target_title(
        relation: DocumentRelation,
        resolved: GraphDocumentTarget,
        target_id: str,
    ) -> str:
        """Name a resolved relation's far end as well as the connector allows."""

        if resolved.title:
            return resolved.title
        supplied = relation.metadata.get("target_title")
        if supplied is not None and str(supplied).strip():
            return str(supplied).strip()
        return target_id


__all__ = ["SourceRelationProjector", "normalized_predicate"]
