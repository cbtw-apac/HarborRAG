from __future__ import annotations

import cloudpickle
import pytest

from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.normalized_document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.schemas.documents import ChunkRecord
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkingDiagnostics,
    ChunkingResult,
    ChunkManifest,
    ChunkReference,
    ChunkValidationResult,
)
from harborrag_runtime.temporal.process_codec import (
    ProcessResultKind,
    decode_process_response,
    encode_process_error,
    encode_process_result,
)


def _document() -> Document:
    return Document(
        id="document-1",
        title="Title",
        content=[DocumentElement(id="element-1", type="paragraph", content="text")],
        content_type="page",
        provenance=DocumentProvenance(source="jira", record_id="record-1"),
    )


def _chunking_result() -> ChunkingResult:
    record = ChunkRecord.from_legacy(
        logical_chunk_id="logical-1",
        chunk_revision_id="revision-1",
        tenant_id="tenant-1",
        document_id="document-1",
        document_version_id="document-version-1",
        artifact_id="artifact-1",
        artifact_revision_id="artifact-revision-1",
        ordinal=0,
        role="body",
        content="text",
        content_hash="content-hash",
        token_count=1,
    )
    reference = ChunkReference(
        logical_chunk_id="logical-1",
        chunk_revision_id="revision-1",
        ordinal=0,
        content_hash="content-hash",
        token_count=1,
    )
    manifest = ChunkManifest(
        tenant_id="tenant-1",
        artifact_id="artifact-1",
        artifact_revision_id="artifact-revision-1",
        chunker_name="default",
        chunker_version="1",
        configuration_hash="configuration-hash",
        chunks=(reference,),
        total_token_count=1,
        total_chunk_count=1,
        validation=ChunkValidationResult(valid=True),
        fingerprint="fingerprint",
    )
    return ChunkingResult(
        artifact_id="artifact-1",
        artifact_revision_id="artifact-revision-1",
        strategy="default",
        profile="default",
        profile_hash="configuration-hash",
        chunks=(record,),
        diagnostics=ChunkingDiagnostics("default", "default", 1, 0, 0, 0, 1, 1),
        manifest=manifest,
    )


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (ProcessResultKind.JSON, ("value", 1)),
        (
            ProcessResultKind.PARSED_DOCUMENT,
            ParsedDocument(content="text", parser_name="test"),
        ),
        (ProcessResultKind.NORMALIZED_DOCUMENT, _document()),
        (ProcessResultKind.CHUNKING_RESULT, _chunking_result()),
    ],
)
def test_process_result_round_trip_uses_explicit_typed_codecs(kind, value) -> None:
    status, decoded = decode_process_response(encode_process_result(kind, value), kind)

    assert status == "result"
    assert decoded == value


def test_process_error_carries_only_bounded_type_information() -> None:
    status, value = decode_process_response(
        encode_process_error(RuntimeError("token=must-not-cross")),
        ProcessResultKind.JSON,
    )

    assert status == "error"
    assert value == {"module": "builtins", "type": "RuntimeError"}
    assert "must-not-cross" not in repr(value)


def test_parent_rejects_pickle_without_deserializing_it() -> None:
    payload = cloudpickle.dumps(("result", {"attacker": "controlled"}))

    with pytest.raises(RuntimeError, match="invalid JSON"):
        decode_process_response(payload, ProcessResultKind.JSON)
