"""Workers pick the parser and chunker per run; no override keeps the boot objects."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.exceptions import ApplicationError

from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_engine.ingestion.chunking import ChunkingRequest
from harborrag_runtime.config.chunking import default_chunking_settings
from harborrag_runtime.config.parsers import load_parser_catalog
from harborrag_runtime.ingestion.document.materialization import DocumentMaterializationStages
from harborrag_runtime.ingestion.document.run_tools import ParserUnavailableError, RunTools
from harborrag_runtime.ingestion.runtime_builder import build_default_chunker
from harborrag_runtime.ingestion_contracts import RunSettings
from harborrag_runtime.temporal import ingestion_activities as activity_module
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.tokenization import ApproximateTokenCounter

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
CAP = 256


def _run_tools(*, parsers: Any = None, build_chunker: Any = None) -> RunTools:
    chunking = default_chunking_settings()
    return RunTools(
        parser=cast(Any, object()),
        chunker=build_default_chunker(config=chunking.config),
        chunking=chunking,
        parsers=parsers or load_parser_catalog(REPO_ROOT / "config" / "ingestion" / "parsers.yaml"),
        build_chunker=build_chunker
        or (lambda config: build_default_chunker(config=config, refiner=chunking.refiner)),
    )


def _canonical_document(paragraphs: int = 40) -> Document:
    sentence = "Rollback the deployment before draining the queue and notify the owners. "
    return Document(
        id="doc-1",
        title="Runbook",
        content=[
            DocumentElement(f"p{index}", "paragraph", sentence * 6) for index in range(paragraphs)
        ],
        content_type="page",
        provenance=DocumentProvenance(source="local_file"),
    )


@pytest.mark.parametrize("run", [None, RunSettings("rev_0")])
def test_default_run_uses_base_parser_and_chunker(run: RunSettings | None) -> None:
    tools = _run_tools()

    assert tools.parser_for(run) is tools._parser
    assert tools.chunker_for(run) is tools._chunker


def test_enabled_backend_and_uncapped_limits_keep_base_objects() -> None:
    tools = _run_tools()
    enabled = tools._parsers.enabled_pdf_backend()
    run = RunSettings("rev_1", pdf_parser=enabled, chunk_max_tokens=4096)

    assert tools.parser_for(run) is tools._parser
    assert tools.chunker_for(run) is tools._chunker


def test_cap_builds_one_cached_chunker() -> None:
    tools = _run_tools()
    first = tools.chunker_for(RunSettings("rev_1", chunk_max_tokens=CAP))
    second = tools.chunker_for(RunSettings("rev_2", chunk_max_tokens=CAP))

    assert first is second
    assert first is not tools._chunker
    document = _canonical_document()
    assert ApproximateTokenCounter().count(" ".join(e.content for e in document.content)) > 2000

    result = first.chunk(
        ChunkingRequest(
            tenant_id="tenant-1",
            document_version_id="version-1",
            document=document,
            connector_type="local",
            content_type="page",
        )
    )

    counter = ApproximateTokenCounter()
    evidence = [chunk for chunk in result.chunks if chunk.record_kind == "evidence"]
    assert len(evidence) > 1
    assert all(counter.count(chunk.content) <= CAP for chunk in evidence)


def test_docling_without_definition_is_unavailable() -> None:
    tools = _run_tools()

    with pytest.raises(ParserUnavailableError, match="parsing.pdf_parser.*docling"):
        tools.parser_for(RunSettings("rev_1", pdf_parser="docling"))


def test_backend_import_error_is_unavailable() -> None:
    catalog = SimpleNamespace(
        enabled_pdf_backend=lambda: "liteparse",
        pdf_definition_for=lambda backend: "pdf-docling",
        build_registry=Mock(side_effect=ImportError("docling missing")),
    )
    tools = _run_tools(parsers=catalog)

    with pytest.raises(ParserUnavailableError, match="docling"):
        tools.parser_for(RunSettings("rev_1", pdf_parser="docling"))


def test_selected_backend_registry_is_built_once() -> None:
    registry = object()
    catalog = SimpleNamespace(
        enabled_pdf_backend=lambda: "liteparse",
        pdf_definition_for=lambda backend: "pdf-docling",
        build_registry=Mock(return_value=registry),
    )
    tools = _run_tools(parsers=catalog)
    run = RunSettings("rev_1", pdf_parser="docling")

    assert tools.parser_for(run) is registry
    assert tools.parser_for(run) is registry
    catalog.build_registry.assert_called_once_with(pdf_definition="pdf-docling")


@pytest.mark.asyncio
async def test_chunk_stage_uses_run_cap() -> None:
    run = RunSettings("rev_1", chunk_max_tokens=CAP)
    chunker = Mock()
    chunker.chunk.return_value = SimpleNamespace(chunks=(), statistics="stats")
    run_tools = Mock()
    run_tools.chunker_for.return_value = chunker
    chunk_writer = SimpleNamespace(put=AsyncMock())
    dependencies = SimpleNamespace(
        run_tools=run_tools,
        chunker=object(),
        control=SimpleNamespace(),
        canonical_artifacts=None,
        chunk_reader=None,
        projection_artifacts=None,
        chunk_writer=chunk_writer,
    )
    stages = DocumentMaterializationStages(cast(Any, dependencies))
    stages._lifecycle = SimpleNamespace(record_chunks=AsyncMock())  # type: ignore[assignment]
    stages._required_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(chunk_artifact=None, chunk_index_artifact=None)
    )
    stages._canonical = AsyncMock(  # type: ignore[method-assign]
        return_value=_canonical_document(1)
    )
    request = SimpleNamespace(
        tenant_id="tenant-1",
        run_settings=run,
        chunking_max_tokens=None,
        source_identity=SimpleNamespace(connector_type=SimpleNamespace(value="local")),
    )
    prepared = SimpleNamespace(
        requires_processing=True, document_id="doc-1", document_version_id="version-1"
    )

    statistics = await stages.chunk_and_validate(cast(Any, request), cast(Any, prepared))

    run_tools.chunker_for.assert_called_once_with(run)
    chunker.chunk.assert_called_once()
    assert statistics == "stats"


@pytest.mark.asyncio
async def test_parse_activity_maps_parser_unavailable_to_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation = SimpleNamespace(
        parse_and_normalize=AsyncMock(
            side_effect=ParserUnavailableError("parsing.pdf_parser 'docling' has no definition")
        )
    )
    runtime = SimpleNamespace(
        stages=SimpleNamespace(preparation=preparation),
        source_plans=None,
    )
    activities = IngestionActivities(cast(Any, runtime))
    activities._documents = cast(
        Any, SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(request="release")))
    )
    activities._observability = cast(Any, SimpleNamespace(boundary=_passthrough_boundary))
    monkeypatch.setattr(activity_module, "last_heartbeat_detail", lambda: None)
    document = SimpleNamespace(task_id="task-1", document_index=0)
    request = SimpleNamespace(document=document, document_id="doc-1")
    monkeypatch.setattr(activity_module, "to_capture_stage", lambda value: "capture")

    with pytest.raises(ApplicationError) as raised:
        await activities.parse_and_normalize(cast(Any, request))

    assert raised.value.type == "parser_unavailable"
    assert raised.value.non_retryable is True


def _passthrough_boundary(name: str) -> AbstractContextManager[None]:
    return nullcontext()


@pytest.mark.asyncio
async def test_missing_pdf_parser_failure_is_recorded_retryable() -> None:
    from harborrag_runtime.ingestion.source.documents import SourceDocumentService
    from harborrag_runtime.temporal.failure_handling import durable_failure
    from runtime_ingestion.unit.source.test_plan import _planned

    recorded = AsyncMock()
    control = SimpleNamespace(tasks=SimpleNamespace(record_document_result=recorded))
    service = SourceDocumentService(control=cast(Any, control), documents=cast(Any, object()))
    error = ApplicationError("unavailable", type="parser_unavailable", non_retryable=True)
    error_type, _ = durable_failure(error)

    await service.record_failed_document(
        "task-1", _planned(), error_type=error_type, failed_stage="ParseAndNormalize"
    )

    result = recorded.await_args.args[0].result
    assert result["safe_error_code"] == "document_release_parser_unavailable"
    assert result["retryable"] is True


@pytest.mark.asyncio
async def test_direct_mode_records_an_unavailable_parser_like_temporal_mode() -> None:
    from harborrag_runtime.ingestion.failure_classification import ingestion_failure_classifier
    from harborrag_runtime.ingestion.source.documents import SourceDocumentService
    from runtime_ingestion.unit.source.test_plan import _planned

    error = ParserUnavailableError("parsing.pdf_parser 'docling' has no definition")
    error.harborrag_stage = "ParseAndNormalize"  # type: ignore[attr-defined]
    recorded = AsyncMock()
    control = SimpleNamespace(tasks=SimpleNamespace(record_document_result=recorded))
    documents = SimpleNamespace(release=AsyncMock(side_effect=error))
    service = SourceDocumentService(control=cast(Any, control), documents=cast(Any, documents))

    await service.release_one("task-1", _planned(), cast(Any, object()))

    result = recorded.await_args.args[0].result
    failure = ingestion_failure_classifier().classify("ParseAndNormalize", error)
    assert (failure.code, failure.retryable) == ("parser_unavailable", True)
    assert result["safe_error_code"] == "document_release_parser_unavailable"
    assert result["retryable"] is True
