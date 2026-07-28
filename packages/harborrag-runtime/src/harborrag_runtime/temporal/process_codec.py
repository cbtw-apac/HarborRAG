"""Non-executable result channel for isolated document workers."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import TypeAdapter, ValidationError

from harborrag_core.domain.normalized_document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_engine.ingestion.chunking.schemas import ChunkingResult
from harborrag_runtime.temporal.ingestioncodec import (
    CODEC_VERSION,
    dump_payload,
    load_chunking_result,
)


class ProcessResultKind(StrEnum):
    JSON = "json"
    PARSED_DOCUMENT = "parsed-document"
    NORMALIZED_DOCUMENT = "normalized-document"
    CHUNKING_RESULT = "chunking-result"


_PARSED_DOCUMENT = TypeAdapter(ParsedDocument)
_NORMALIZED_DOCUMENT = TypeAdapter(Document)


def encode_process_result(kind: ProcessResultKind, value: object) -> bytes:
    """Serialize a trusted result shape to JSON in the isolated child."""

    if kind is ProcessResultKind.JSON:
        container = "tuple" if isinstance(value, tuple) else "value"
        return dump_payload(
            "process-json",
            {"container": container, "value": value},
        )
    if kind is ProcessResultKind.PARSED_DOCUMENT:
        _require_type(value, ParsedDocument, kind)
        return dump_payload("process-parsed-document", value)
    if kind is ProcessResultKind.NORMALIZED_DOCUMENT:
        _require_type(value, Document, kind)
        return dump_payload("process-normalized-document", value)
    if kind is ProcessResultKind.CHUNKING_RESULT:
        _require_type(value, ChunkingResult, kind)
        return dump_payload("chunking-result", value)
    raise ValueError("unsupported isolated process result kind")


def encode_process_error(error: BaseException) -> bytes:
    return dump_payload(
        "process-error",
        {"module": type(error).__module__, "type": type(error).__name__},
    )


def decode_process_response(
    payload: bytes,
    expected_kind: ProcessResultKind,
) -> tuple[str, object]:
    """Validate an untrusted child response without importing executable state."""

    try:
        envelope = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("isolated document worker returned invalid JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("version") != CODEC_VERSION:
        raise RuntimeError("isolated document worker returned an invalid envelope")
    kind = envelope.get("kind")
    value = envelope.get("value")
    if kind == "process-error":
        return "error", _validate_error(value)
    if kind != _wire_kind(expected_kind):
        raise RuntimeError("isolated document worker returned an unexpected result kind")
    try:
        return "result", _decode_value(expected_kind, value, payload)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise RuntimeError("isolated document worker returned invalid result data") from exc


def _decode_value(kind: ProcessResultKind, value: object, payload: bytes) -> object:
    if kind is ProcessResultKind.JSON:
        if not isinstance(value, dict) or set(value) != {"container", "value"}:
            raise ValueError("invalid JSON result envelope")
        container = value["container"]
        if container == "tuple":
            items = value["value"]
            if not isinstance(items, list):
                raise ValueError("invalid tuple result")
            return tuple(items)
        if container != "value":
            raise ValueError("invalid JSON result container")
        return value["value"]
    if kind is ProcessResultKind.PARSED_DOCUMENT:
        return _PARSED_DOCUMENT.validate_python(value)
    if kind is ProcessResultKind.NORMALIZED_DOCUMENT:
        return _NORMALIZED_DOCUMENT.validate_python(value)
    if kind is ProcessResultKind.CHUNKING_RESULT:
        return load_chunking_result(payload)
    raise ValueError("unsupported isolated process result kind")


def _validate_error(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"module", "type"}:
        raise RuntimeError("isolated document worker returned an invalid error envelope")
    module = value["module"]
    error_type = value["type"]
    if not isinstance(module, str) or not isinstance(error_type, str):
        raise RuntimeError("isolated document worker returned an invalid error envelope")
    if not module or not error_type or len(module) > 256 or len(error_type) > 256:
        raise RuntimeError("isolated document worker returned an invalid error envelope")
    return {"module": module, "type": error_type}


def _wire_kind(kind: ProcessResultKind) -> str:
    if kind is ProcessResultKind.CHUNKING_RESULT:
        return "chunking-result"
    return f"process-{kind.value}"


def _require_type(value: object, expected: type[object], kind: ProcessResultKind) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"isolated {kind.value} result has an invalid type")
