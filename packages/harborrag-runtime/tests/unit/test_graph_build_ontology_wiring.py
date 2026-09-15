"""graph_build.yaml selects which vocabulary the LLM extracts into, per source."""

from __future__ import annotations

import pytest

from harborrag_runtime.config.errors import GraphBuildConfigurationError
from harborrag_runtime.config.graph_build import GraphBuildConfig

pytestmark = pytest.mark.unit

_ONTOLOGY = """version: acme-v1
entity_types: [person, service]
relations:
  - name: owns
    subjects: [person]
    objects: [service]
"""


def _project(tmp_path, graph_build: str):
    (tmp_path / "ontologies").mkdir(exist_ok=True)
    (tmp_path / "ontologies" / "acme.yaml").write_text(_ONTOLOGY, encoding="utf-8")
    path = tmp_path / "graph_build.yaml"
    path.write_text(graph_build, encoding="utf-8")
    return path


def test_a_source_resolves_the_named_ontology_into_its_extraction_profile(tmp_path) -> None:
    path = _project(
        tmp_path,
        """ontologies:
  acme: ontologies/acme.yaml
tenants:
  - tenant_id: tenant-a
    mode: llm
    sources:
      - source_scope_id: source-a
        ontology: acme
""",
    )

    config = GraphBuildConfig.from_file(path)
    registry = config.resolved_ontologies[config.tenants[0].sources[0].ontology]

    assert registry.version == "acme-v1"
    assert registry.entity_types == ("person", "service")


def test_a_source_naming_an_undeclared_ontology_is_rejected_at_load(tmp_path) -> None:
    path = _project(
        tmp_path,
        """ontologies:
  acme: ontologies/acme.yaml
tenants:
  - tenant_id: tenant-a
    mode: llm
    sources:
      - source_scope_id: source-a
        ontology: missing
""",
    )

    with pytest.raises(GraphBuildConfigurationError, match="missing"):
        GraphBuildConfig.from_file(path)


def test_omitting_an_ontology_keeps_the_builtin_vocabulary(tmp_path) -> None:
    path = _project(
        tmp_path,
        """tenants:
  - tenant_id: tenant-a
    mode: llm
    sources:
      - source_scope_id: source-a
""",
    )

    config = GraphBuildConfig.from_file(path)
    assert config.tenants[0].sources[0].ontology is None
    assert config.resolved_ontologies == {}


def test_profile_factory_pins_the_configured_vocabulary_into_the_extraction_profile(
    tmp_path,
) -> None:
    from harborrag_runtime.topology.configuration import GraphBuildProfileFactory

    path = _project(
        tmp_path,
        """ontologies:
  acme: ontologies/acme.yaml
tenants:
  - tenant_id: tenant-a
    mode: llm
    sources:
      - source_scope_id: source-a
        ontology: acme
""",
    )
    models = tmp_path / "models.yaml"
    models.write_text(
        """default_model: primary
models:
  primary:
    provider: openai
    model: openai/extractor-v1
    api_key: not-a-real-key
    capabilities:
      structured_output: true
""",
        encoding="utf-8",
    )
    config = GraphBuildConfig.from_file(path)
    factory = GraphBuildProfileFactory(models, config.resolved_ontologies)

    profile = factory.build(config.tenants[0].sources[0])

    assert profile.ontology_version == "acme-v1"
    assert profile.resolved_ontology().entity_types == ("person", "service")
    # Changing the vocabulary must change reuse identity, or generations mix.
    assert (
        profile.fingerprint
        != factory.build(
            config.tenants[0].sources[0].model_copy(update={"ontology": None})
        ).fingerprint
    )
