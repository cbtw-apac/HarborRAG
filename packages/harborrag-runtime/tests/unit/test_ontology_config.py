"""Operators author the LLM's extraction vocabulary; endpoint pairs are expanded for them."""

from __future__ import annotations

import pytest

from harborrag_runtime.config.errors import GraphBuildConfigurationError
from harborrag_runtime.config.ontology_loading import load_ontology

pytestmark = pytest.mark.unit

_VALID = """version: acme-v1
entity_types: [person, team, service]
relations:
  - name: owns
    subjects: [person, team]
    objects: [service]
    examples: ["Team A owns Service B."]
    exclusions: ["Shared names do not establish ownership."]
  - name: related_to
    subjects: any
    objects: any
"""


def _write(tmp_path, payload: str):
    path = tmp_path / "acme.yaml"
    path.write_text(payload, encoding="utf-8")
    return path


def test_subjects_and_objects_expand_to_the_cartesian_endpoint_pairs(tmp_path) -> None:
    registry = load_ontology(_write(tmp_path, _VALID))

    assert registry.version == "acme-v1"
    assert registry.entity_types == ("person", "team", "service")
    owns = next(r for r in registry.relations if r.name == "owns")
    assert owns.endpoint_pairs == (("person", "service"), ("team", "service"))
    assert owns.exclusions == ("Shared names do not establish ownership.",)


def test_any_expands_to_every_declared_entity_type(tmp_path) -> None:
    registry = load_ontology(_write(tmp_path, _VALID))

    related = next(r for r in registry.relations if r.name == "related_to")
    assert len(related.endpoint_pairs) == 9
    assert ("service", "person") in related.endpoint_pairs


def test_a_relation_endpoint_outside_the_declared_types_is_rejected(tmp_path) -> None:
    payload = _VALID.replace("objects: [service]", "objects: [database]")

    with pytest.raises(GraphBuildConfigurationError, match="database"):
        load_ontology(_write(tmp_path, payload))


def test_unknown_keys_are_rejected_rather_than_silently_ignored(tmp_path) -> None:
    payload = _VALID.replace("version: acme-v1", "version: acme-v1\ntypo_key: true")

    with pytest.raises(GraphBuildConfigurationError):
        load_ontology(_write(tmp_path, payload))
