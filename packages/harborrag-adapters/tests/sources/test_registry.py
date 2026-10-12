from __future__ import annotations

import sys

import pytest

from harborrag_adapters.sources.registry import (
    SourceConnectorRegistry,
    source_registry,
    validate_connector,
)
from harborrag_core.sources import ConfigError
from harborrag_core.sources.testing import FakeCheckpoint, FakeConfig

from . import registry_fixtures as fixtures

MODULE = fixtures.__name__


def test_a_declared_connector_validates_and_resolves_by_alias() -> None:
    registry = SourceConnectorRegistry()
    registry.register_lazy("fake", f"{MODULE}:DeclaredFake", aliases=("fake_alias",))
    spec = registry.get("fake_alias")
    assert spec.cls is fixtures.DeclaredFake
    assert spec.config is FakeConfig and spec.checkpoint is FakeCheckpoint
    assert spec.capabilities.blobs
    assert registry.validate_all() == []
    assert validate_connector(fixtures.DeclaredFake) == []


def test_validation_reports_every_problem_of_a_broken_connector() -> None:
    problems = validate_connector(fixtures.BrokenFake)
    joined = "\n".join(problems)
    assert "fetch_keys must be an async generator" in joined
    assert "_Untagged.page_size must be annotated" in joined
    assert "version: Literal" in joined
    assert validate_connector(fixtures.Undeclared) == ["Undeclared is not declared with @connector"]


def test_registry_refuses_broken_misnamed_and_unknown_connectors() -> None:
    registry = SourceConnectorRegistry()
    registry.register_lazy("broken", f"{MODULE}:BrokenFake")
    registry.register_lazy("other", f"{MODULE}:Misnamed")
    registry.register_lazy("missing", f"{MODULE}:DoesNotExist")
    with pytest.raises(ConfigError, match="async generator"):
        registry.get("broken")
    with pytest.raises(ConfigError, match="declares itself as 'misnamed'"):
        registry.get("other")
    with pytest.raises(ConfigError, match="unknown source connector"):
        registry.get("nope")
    assert len(registry.validate_all()) == 3


def test_registration_is_lazy_and_names_are_unique() -> None:
    registry = SourceConnectorRegistry()
    registry.register_lazy("later", "harborrag_adapters_never_imported.module:Thing")
    assert "harborrag_adapters_never_imported" not in sys.modules
    with pytest.raises(ValueError, match="already registered"):
        registry.register_lazy("later", f"{MODULE}:DeclaredFake")
    with pytest.raises(ValueError, match="already registered"):
        registry.register_lazy("x", f"{MODULE}:DeclaredFake", aliases=("later",))
    with pytest.raises(ValueError, match="module:Class"):
        registry.register_lazy("y", "no_colon")


def test_the_shared_registry_validates_cleanly() -> None:
    assert source_registry.validate_all() == []
