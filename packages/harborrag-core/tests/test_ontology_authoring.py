"""Operator-authored vocabularies must be validated as strictly as built-in ones."""

from __future__ import annotations

import pytest

from harborrag_core.topology.ontology import OntologyRegistry, RelationDefinition

pytestmark = pytest.mark.unit


def _relation(name: str = "owns") -> RelationDefinition:
    return RelationDefinition(name=name, endpoint_pairs=(("person", "service"),))


@pytest.mark.parametrize("entity_type", ["", "Person", "has space", "a" * 65, "1person"])
def test_entity_types_reject_names_the_wire_schema_cannot_carry(entity_type: str) -> None:
    # An unvalidated type still produces a valid-looking Literal enum, then rejects
    # every chunk after the full provider reservation has been spent.
    with pytest.raises(ValueError):
        OntologyRegistry(
            version="custom-v1",
            # "person"/"service" keep the endpoint check satisfied, so the only
            # thing left to reject is the malformed type name itself.
            entity_types=("person", "service", entity_type),
            relations=(_relation(),),
        )


def test_well_formed_entity_types_are_accepted() -> None:
    registry = OntologyRegistry(
        version="custom-v1",
        entity_types=("person", "service"),
        relations=(_relation(),),
    )
    assert registry.entity_types == ("person", "service")
