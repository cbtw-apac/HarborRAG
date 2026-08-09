from dataclasses import replace

import pytest

from harborrag_core.chunking import ChunkKind
from harborrag_core.contracts.chunking import TokenCounter
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkStrategyRegistry,
    build_chunking_service,
)
from harborrag_engine.ingestion.chunking.config import ChunkingProfile
from harborrag_engine.ingestion.chunking.schemas import ChunkingRequest, ChunkUnit
from harborrag_engine.ingestion.chunking.sources.canonical import (
    CanonicalDocumentChunkingStrategy,
)

from ..chunking_helpers import (
    CharacterCounter,
    CharacterRefiner,
    make_document,
    make_profile,
    make_request,
    make_service,
)


class CommunitySourceStrategy:
    name = "community"
    version = "1"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._canonical = CanonicalDocumentChunkingStrategy(token_counter)

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        return tuple(
            replace(unit, metadata={**unit.metadata, "community_source": True})
            for unit in self._canonical.create_units(request, profile)
        )


def test_open_source_strategy_is_added_without_changing_the_service() -> None:
    profile = make_profile(name="community", strategy="community")
    strategy = CommunitySourceStrategy(CharacterCounter())
    document = make_document([DocumentElement("body", "paragraph", "Community content")])

    result = make_service(
        profile,
        additional_strategies=(strategy,),
    ).chunk(make_request(document))

    assert result.strategy == "community"
    assert result.chunks[0].metadata["community_source"] is True


def test_registry_rejects_duplicate_strategy_names() -> None:
    counter = CharacterCounter()
    strategy = CommunitySourceStrategy(counter)

    with pytest.raises(ValueError, match="already registered"):
        ChunkStrategyRegistry((strategy, CommunitySourceStrategy(counter)))


def test_default_config_selects_maintained_source_strategies() -> None:
    confluence = make_document(
        [DocumentElement("body", "paragraph", "Page content")],
        source="confluence",
        record_id="page-1",
    )
    jira = make_document(
        [DocumentElement("body", "paragraph", "Issue content", {"field": "description"})],
        source="jira",
        record_id="HARBOR-1",
    )
    counter = CharacterCounter()
    service = build_chunking_service(
        config=ChunkingConfig(),
        token_counter=counter,
        refiner=CharacterRefiner(),
    )

    assert service.chunk(make_request(confluence)).strategy == "confluence"
    assert service.chunk(make_request(jira)).strategy == "jira"


def test_default_config_uses_canonical_strategy_for_jira_table_attachment() -> None:
    table_id = "table:evidence"
    table_version_id = "table-version:evidence-v1"
    attachment = make_document(
        [
            DocumentElement(
                "sheet-1",
                "table",
                "Status\tOwner\nPassed\tAda",
                {
                    "table_id": table_id,
                    "table_version_id": table_version_id,
                    "tab_path": ("Evidence",),
                },
            )
        ],
        source="jira",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        record_id="jira://HARBOR/HARBOR-1/attachments/10001",
        extra={
            "binding_kind": "ATTACHMENT",
            "connector_type": "jira",
            "issue_key": "HARBOR-1",
        },
    )
    counter = CharacterCounter()
    service = build_chunking_service(
        config=ChunkingConfig(),
        token_counter=counter,
        refiner=CharacterRefiner(),
    )

    result = service.chunk(make_request(attachment))

    assert result.strategy == "canonical"
    table_chunks = [chunk for chunk in result.chunks if chunk.chunk_kind == ChunkKind.TABLE]
    assert len(table_chunks) == 1
    assert table_chunks[0].table_locator is not None
    assert table_chunks[0].table_locator.table_id == table_id
    assert table_chunks[0].table_locator.table_version_id == table_version_id
    assert service.chunk(make_request(attachment, profile_name="jira")).strategy == "jira"
