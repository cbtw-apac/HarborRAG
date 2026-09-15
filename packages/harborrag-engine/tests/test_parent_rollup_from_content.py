"""A parent rolls up its 1-hop children's content, with no extraction in the loop."""

from __future__ import annotations

import pytest

from harborrag_core.topology.derived import DescriptionOutput, RollupSource
from harborrag_engine.topology.parent_builder import ParentDescriptionBuilder

pytestmark = pytest.mark.unit


class _Generator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def generate(self, packets):
        self.calls.append(tuple(packet.text for packet in packets))
        return DescriptionOutput(
            description="rolled up",
            cited_packet_ids=(packets[0].packet_id,),
            complete=True,
        )


def _sources(count: int) -> tuple[RollupSource, ...]:
    return tuple(
        RollupSource(
            chunk_id=f"chunk-{index}",
            text=f"Raw body of chunk {index}.",
            section_path=("Ingestion and Parsing",),
        )
        for index in range(count)
    )


@pytest.mark.asyncio
async def test_parent_summarises_raw_child_content() -> None:
    generator = _Generator()

    parents = await ParentDescriptionBuilder(generator).build("doc", _sources(3))

    # The model saw the children's content, not a description of it.
    assert generator.calls[0] == (
        "Raw body of chunk 0.",
        "Raw body of chunk 1.",
        "Raw body of chunk 2.",
    )
    assert parents[-1].input_chunk_ids == ("chunk-0", "chunk-1", "chunk-2")
    assert parents[-1].description == "rolled up"


@pytest.mark.asyncio
async def test_a_child_with_no_extracted_description_is_not_an_obstacle() -> None:
    # The previous contract raised when any child description was empty, which made the
    # product unreachable without a prior extraction pass.
    parents = await ParentDescriptionBuilder(_Generator()).build("doc", _sources(1))

    assert parents
