from __future__ import annotations

from harborrag_core.contracts.chunking import (
    ChunkContextualizer,
    ChunkHeaderContext,
    SplitBoundaryKind,
    TextRefinementRequest,
    TextSplit,
)
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion.chunking.records.contextualizer import (
    ContextHeaderBudget,
    DefaultChunkContextualizer,
    budgeted_profile,
)

from .chunking_helpers import (
    CharacterCounter,
    make_document,
    make_profile,
    make_request,
    make_service,
)


def contextualizer() -> DefaultChunkContextualizer:
    return DefaultChunkContextualizer(CharacterCounter())


def test_default_contextualizer_is_a_core_port() -> None:
    assert isinstance(contextualizer(), ChunkContextualizer)
    assert contextualizer().format_version == "1"


def test_header_renders_every_present_field_in_a_fixed_order() -> None:
    header = contextualizer().header(
        ChunkHeaderContext(
            document_title="Guide",
            structural_path=("Install", "Linux"),
            caption="Figure 2",
            code_location="src/app.py",
            language="python",
            scope=("class App:", "def run(self):"),
            part_index=2,
            part_count=3,
        )
    )

    assert header == (
        "Document: Guide\n"
        "Section: Install > Linux\n"
        "Caption: Figure 2\n"
        "File: src/app.py (python)\n"
        "Scope: class App: > def run(self):\n"
        "Part: 2/3"
    )


def test_header_omits_absent_fields_and_render_joins_with_a_blank_line() -> None:
    plain = contextualizer()

    assert plain.header(ChunkHeaderContext()) == ""
    assert plain.render("", "body") == "body"
    assert plain.header(ChunkHeaderContext(language="go")) == "Language: go"
    assert plain.render("Document: X", "body") == "Document: X\n\nbody"


def test_header_trims_outer_context_first_to_fit_a_budget() -> None:
    context = ChunkHeaderContext(
        document_title="Guide",
        structural_path=("Part One", "Chapter Two", "Section Three"),
        scope=("class A:", "def b(self):"),
        part_index=1,
        part_count=2,
    )
    plain = contextualizer()
    full = plain.header(context)

    trimmed = plain.header(context, maximum_tokens=len(full) - 10)

    assert "Section: Section Three" in trimmed or "Section: Chapter Two > Section Three" in trimmed
    assert "Part One" not in trimmed
    assert trimmed.endswith("Part: 1/2")
    assert plain.header(context, maximum_tokens=3) == ""


def test_evidence_embedding_text_carries_title_and_full_path() -> None:
    document = make_document(
        [
            DocumentElement("h1", "heading", "Install", {"level": 1}),
            DocumentElement("h2", "heading", "Linux", {"level": 2}),
            DocumentElement("p1", "paragraph", "run the installer"),
        ]
    )

    result = make_service(make_profile(maximum=200, target=150)).chunk(make_request(document))

    (chunk,) = [record for record in result.chunks if record.content == "run the installer"]
    assert chunk.embedding_text == (
        "Document: HarborRAG\nSection: Install > Linux\n\nrun the installer"
    )
    assert chunk.content == "run the installer"
    assert "context_truncated" not in chunk.metadata


def test_reserved_header_budget_keeps_embedding_text_within_the_hard_limit() -> None:
    sentences = " ".join(f"Sentence {index} ends here." for index in range(40))
    document = make_document(
        [
            DocumentElement("h1", "heading", "A rather long section heading", {"level": 1}),
            DocumentElement("p1", "paragraph", sentences),
        ]
    )
    # The header (~74 characters with the part allowance) stays under the
    # one-third cap of a 300-character limit, so nothing has to be trimmed.
    profile = make_profile(minimum=10, target=150, maximum=300)

    result = make_service(profile, reserve_context_tokens=True).chunk(make_request(document))

    evidence = [record for record in result.chunks if record.record_kind.value == "evidence"]
    assert len(evidence) > 1
    assert all(len(record.embedding_text) <= 300 for record in evidence)
    assert all(record.embedding_text.startswith("Document: HarborRAG") for record in evidence)
    assert all("context_truncated" not in record.metadata for record in evidence)
    parts = [record.embedding_text.rsplit("Part: ", 1)[-1].split("\n", 1)[0] for record in evidence]
    assert parts == [f"{index}/{len(evidence)}" for index in range(1, len(evidence) + 1)]


def test_without_the_reserve_an_oversized_header_is_dropped_and_flagged() -> None:
    document = make_document([DocumentElement("p1", "paragraph", "x" * 24)])
    profile = make_profile(minimum=1, target=20, maximum=25)

    result = make_service(profile).chunk(make_request(document))

    record = result.chunks[0]
    assert record.embedding_text == record.content
    assert record.metadata["context_truncated"] is True


def test_budget_reserve_is_the_largest_unit_header_capped_at_a_third() -> None:
    document = make_document([DocumentElement("p1", "paragraph", "body")])
    request = make_request(document)
    profile = make_profile(maximum=300, target=200)
    budget = ContextHeaderBudget(contextualizer(), CharacterCounter())
    units = (
        make_service(profile)
        ._candidates._strategies.get("canonical")
        .create_units(request, profile)
    )

    reserve = budget.reserve(request, units, profile)

    assert reserve == len("Document: HarborRAG") + 2
    assert budget.reserve(request, units, make_profile(maximum=30, target=20)) == 10


def test_budgeted_profile_keeps_limits_ordered() -> None:
    profile = make_profile(minimum=5, target=20, maximum=25, overlap=4)

    reduced = budgeted_profile(profile, 8)

    assert reduced.maximum_tokens == 17
    assert reduced.soft_maximum_tokens == 17
    assert reduced.target_tokens == 17
    assert reduced.minimum_tokens == 5
    assert budgeted_profile(profile, 0) is profile


class RecordingCodeRefiner:
    """Split code in halves and report scope the way the adapters refiner does."""

    def __init__(self) -> None:
        self.requests: list[TextRefinementRequest] = []

    def split(self, request: TextRefinementRequest) -> tuple[TextSplit, ...]:
        self.requests.append(request)
        middle = request.content.index("\n", len(request.content) // 2) + 1
        return tuple(
            TextSplit(
                content=part,
                token_count=len(part),
                boundary_kind=SplitBoundaryKind.FORCED,
                structural_path=request.structural_path,
                prefix="class Store:" if index else None,
                forced_split=True,
            )
            for index, part in enumerate((request.content[:middle], request.content[middle:]))
        )


def test_code_units_reach_the_refiner_with_language_and_keep_their_scope() -> None:
    code = "class Store:\n" + "".join(f"    def m{index}(self): pass\n" for index in range(6))
    document = make_document(
        [DocumentElement("c1", "code", code, {"language": "python"})],
        extra={"relative_path": "src/store.py"},
    )
    refiner = RecordingCodeRefiner()
    profile = make_profile(minimum=1, target=100, maximum=160)

    result = make_service(profile, refiner=refiner, reserve_context_tokens=True).chunk(
        make_request(document)
    )

    (request,) = refiner.requests
    assert request.language == "python"
    assert request.separators is None
    second = result.chunks[1]
    assert second.metadata["code_scope"] == "class Store:"
    assert "Scope: class Store:" in second.embedding_text
    assert "File: src/store.py (python)" in second.embedding_text
    assert "class Store:" in second.search_text
