"""Frozen derived checkpoints account for every input chunk before publication."""

from __future__ import annotations

from harborrag_core.indexing import VectorIndexRecord
from harborrag_core.topology import DocumentTopologyBuild


def validate_derived_coverage(
    records: list[VectorIndexRecord], build: DocumentTopologyBuild, kind: str
) -> None:
    expected = set(build.chunk_ids)
    if not records or not expected or len(expected) != len(build.chunk_ids):
        raise ValueError("derived coverage requires non-empty distinct input chunks")
    if kind == "contextual":
        chunks = [row.payload.get("chunk_id") for row in records]
        if (
            not all(isinstance(chunk, str) for chunk in chunks)
            or len(set(chunks)) != len(chunks)
            or set(chunks) != expected
        ):
            raise ValueError("contextual checkpoint coverage does not match all input chunks")
        return
    _validate_parent_coverage(records, build, expected)


def _validate_parent_coverage(
    records: list[VectorIndexRecord], build: DocumentTopologyBuild, expected: set[str]
) -> None:
    parent_keys: set[str] = set()
    union: set[str] = set()
    documents = 0
    for row in records:
        payload = row.payload
        key, level = payload.get("parent_key"), payload.get("level")
        if not isinstance(key, str) or not key or key in parent_keys:
            raise ValueError("parent checkpoint requires distinct parent keys")
        if level not in {"section", "document"}:
            raise ValueError("unsupported parent checkpoint level")
        if payload.get("input_coverage") != "complete":
            raise ValueError("parent checkpoint does not account for every supplied input")
        if payload.get("semantic_coverage") not in {"not_evaluated", "validated"}:
            raise ValueError("parent checkpoint has unknown semantic coverage")
        parent_keys.add(key)
        inputs, citations = (
            _chunk_set(payload.get("chunk_ids")),
            _chunk_set(payload.get("cited_chunk_ids")),
        )
        if not inputs <= expected or not citations <= inputs:
            raise ValueError("parent checkpoint citations exceed input coverage")
        union.update(inputs)
        if level == "document":
            documents += 1
            if key != build.document_id or inputs != expected:
                raise ValueError("document parent must cover every input chunk")
    if documents != 1 or union != expected:
        raise ValueError("parent checkpoint requires complete document-level coverage")


def _chunk_set(value: object) -> set[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("parent checkpoint requires non-empty chunk/citation IDs")
    return set(value)
