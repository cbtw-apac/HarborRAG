from __future__ import annotations

import pytest
from harborrag_adapters.chunking import (
    HarborBaseChunk,
    HarborChunk,
    HarborChunkRegistry,
)
from harborrag_adapters.chunking.markdownsplitter import MarkdownStructureSplitter
from harborrag_core.contracts.chunking import (
    JsonStructureSplitRequest,
    StructureSplitRequest,
    TextSplit,
)


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


class StubChunk(HarborBaseChunk[StructureSplitRequest]):
    chunk_name = "stub"
    request_type = StructureSplitRequest

    def __init__(
        self,
        token_counter: CharacterCounter,
        *,
        prefix: str = "",
    ) -> None:
        super().__init__(token_counter)
        self.prefix = prefix

    def split(self, request: StructureSplitRequest) -> tuple[TextSplit, ...]:
        content = f"{self.prefix}{request.content}"
        return (
            TextSplit(
                content=content,
                token_count=self._token_counter.count(content),
            ),
        )


class OtherStubChunk(StubChunk):
    chunk_name = "other"


class MissingDependencyChunk(StubChunk):
    chunk_name = "missing"
    required_dependency = "harborrag_dependency_that_does_not_exist"


def test_default_registry_contains_every_builtin_adapter() -> None:
    registry = HarborChunkRegistry.default()

    assert registry.names() == ("html", "json", "markdown", "recursive")
    assert registry.get_class("markdown") is MarkdownStructureSplitter


def test_registry_creates_adapters_by_name_and_alias() -> None:
    registry = HarborChunkRegistry().register(StubChunk, aliases=("test",))

    adapter = registry.create(" TEST ", CharacterCounter(), prefix="result:")

    assert isinstance(adapter, StubChunk)
    assert adapter.split(StructureSplitRequest(content="value"))[0].content == ("result:value")


def test_registry_reports_optional_dependency_availability_without_importing() -> None:
    registry = HarborChunkRegistry().register(StubChunk).register(MissingDependencyChunk)

    assert registry.available("stub")
    assert not registry.available("missing")
    assert not HarborChunk.available("missing", registry=registry)


def test_registry_preflights_alias_collisions_before_mutating() -> None:
    registry = HarborChunkRegistry().register(StubChunk)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(OtherStubChunk, aliases=("free", "stub"))

    assert registry.names() == ("stub",)


def test_harbor_chunk_factory_delegates_to_selected_adapter() -> None:
    registry = HarborChunkRegistry().register(StubChunk)
    chunker = HarborChunk(
        "stub",
        CharacterCounter(),
        registry=registry,
        prefix="factory:",
    )

    result = chunker.split(StructureSplitRequest(content="value"))

    assert chunker.adapter_name == "stub"
    assert isinstance(chunker.adapter, StubChunk)
    assert result[0].content == "factory:value"


def test_harbor_chunk_factory_rejects_the_wrong_request_contract() -> None:
    registry = HarborChunkRegistry().register(StubChunk)
    chunker = HarborChunk("stub", CharacterCounter(), registry=registry)

    with pytest.raises(TypeError, match="expects StructureSplitRequest"):
        chunker.split(JsonStructureSplitRequest(value={"key": "value"}))


def test_registry_rejects_classes_outside_the_chunk_contract() -> None:
    class NotAChunk:
        pass

    registry = HarborChunkRegistry()

    with pytest.raises(TypeError, match="must inherit HarborBaseChunk"):
        registry.register(NotAChunk)  # type: ignore[arg-type]
