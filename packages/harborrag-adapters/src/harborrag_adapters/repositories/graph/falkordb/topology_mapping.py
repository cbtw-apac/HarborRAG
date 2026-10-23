"""Deterministic, text-free mapping for isolated semantic build generations."""

from __future__ import annotations

from typing import Any

from harborrag_core.topology.extraction import digest
from harborrag_core.topology.records import (
    CanonicalAssertion,
    CanonicalMention,
    DocumentTopologyBuild,
)


def _validate_ownership(build: DocumentTopologyBuild, tenant_id: str) -> None:
    if not tenant_id.strip() or not build.build_id.strip():
        raise ValueError("topology projection requires tenant and build identities")
    records: tuple[CanonicalMention | CanonicalAssertion, ...] = (
        *build.mentions,
        *build.assertions,
    )
    owners = {(record.document_id, record.document_version_id) for record in records}
    if len(owners) > 1:
        raise ValueError("topology build contains observations from different document versions")
    if any(
        record.tenant_id != tenant_id or record.build_id != build.build_id for record in records
    ):
        raise ValueError("topology observation ownership mismatch")


def projection_rows(
    build: DocumentTopologyBuild, tenant_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    _validate_ownership(build, tenant_id)
    common = {"tenant_id": tenant_id, "build_id": build.build_id}
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, str]] = {}

    def node(key: str, kind: str, **values: Any) -> None:
        payload = {**common, "record_key": key, "kind": kind, **values}
        payload["payload_sha256"] = digest(payload)
        previous = nodes.setdefault(key, payload)
        if previous != payload:
            raise ValueError("conflicting topology record identity")

    def link(source: str, target: str, role: str) -> None:
        key = digest([source, target, role])
        edges[key] = {
            **common,
            "relation_key": key,
            "source": source,
            "target": target,
            "role": role,
        }

    node("build", "build", artifact_sha256=build.artifact.sha256)
    for chunk_id in build.chunk_ids:
        key = f"chunk:{chunk_id}"
        node(key, "evidence", chunk_id=chunk_id)
        link("build", key, "evidence")
    for mention in build.mentions:
        entity = f"entity:{mention.entity_id}"
        node(entity, "entity", entity_id=mention.entity_id)
        key = f"mention:{mention.mention_id}"
        node(
            key,
            "mention",
            entity_id=mention.entity_id,
            document_id=mention.document_id,
            document_version_id=mention.document_version_id,
            title=mention.observation.name,
            entity_type=mention.observation.entity_type,
            start=mention.observation.span.start,
            end=mention.observation.span.end,
        )
        link(key, entity, "refers_to")
        link(key, f"chunk:{mention.chunk_id}", "supported_by")
    for assertion in build.assertions:
        key = f"assertion:{assertion.assertion_id}"
        observation = assertion.observation
        node(
            key,
            "assertion",
            document_id=assertion.document_id,
            document_version_id=assertion.document_version_id,
            predicate=observation.predicate,
            polarity=observation.polarity,
            modality=observation.modality,
            time_qualifier=observation.time_qualifier or "",
            start=observation.span.start,
            end=observation.span.end,
        )
        link(key, f"entity:{assertion.subject_entity_id}", "subject")
        link(key, f"entity:{assertion.object_entity_id}", "object")
        link(key, f"chunk:{assertion.chunk_id}", "supported_by")
    if any(
        edge[endpoint] not in nodes for edge in edges.values() for endpoint in ("source", "target")
    ):
        raise ValueError("topology projection has missing support or entity endpoints")
    return [nodes[key] for key in sorted(nodes)], [edges[key] for key in sorted(edges)]
