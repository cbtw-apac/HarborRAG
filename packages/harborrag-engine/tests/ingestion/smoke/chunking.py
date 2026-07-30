"""Chunk real connector documents end to end and emit an observable report.

    python packages/harborrag-engine/tests/ingestion/smoke/chunking.py
    python .../chunking.py --connector confluence --limit 3 --output json
    python .../chunking.py --connector jira --limit 5 --profile jira

Each discovered record runs the real ingestion path — connector fetch, parser,
document normalization, chunking — with no pytest, mocks, or fake clients. The
JSON report on stdout describes what the chunker actually did: the routed
strategy and profile, per-stage counters, chunk identities, token statistics,
and the invariants each document passed or failed. Progress lines go to stderr
so stdout stays parseable. `--output json` additionally saves one
self-contained report per record under `output/`.

See README.md in this directory before running it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

from bootstrap import safe_error
from chunking_checks import checks_for
from chunking_report import document_report, failure_report, vector_policy
from chunking_stage import (
    ChunkingStage,
    StageOutcome,
    build_stage,
    load_smoke_environment,
    run_record,
)

from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord
from harborrag_engine.ingestion.chunking import ChunkingConfig
from harborrag_runtime.config import ConfigurationError

DEFAULT_CONNECTOR = "local"
DEFAULT_LIMIT = 3
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"
SUPPORTED_OUTPUT_FORMATS = ("json",)
MAX_NAME_LENGTH = 180


class DiscoveryFailed(RuntimeError):
    """Report a real discovery failure with a bounded, redacted detail."""


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real connector-to-chunk ingestion path and report its output.",
    )
    parser.add_argument(
        "--connector",
        default=DEFAULT_CONNECTOR,
        help=f"Configured connector from config/connectors.yaml (default: {DEFAULT_CONNECTOR}).",
    )
    parser.add_argument(
        "--limit",
        type=_positive,
        default=DEFAULT_LIMIT,
        help=f"Max records to discover and chunk (default: {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        choices=sorted(ChunkingConfig().profiles),
        help="Force one chunking profile instead of letting the router select it.",
    )
    parser.add_argument(
        "--output",
        choices=SUPPORTED_OUTPUT_FORMATS,
        default=None,
        help="Save one self-contained report file per record. Default: save nothing.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Directory for saved reports (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="Embed real chunk text in the report. Off by default; the text is "
        "source content, so only enable it for non-sensitive documents.",
    )
    return parser.parse_args()


def _progress(message: str) -> None:
    print(f"[chunking] {message}", file=sys.stderr)


def _report_skips(stage: ChunkingStage) -> None:
    """Name every path discovery deliberately dropped, with its reason.

    A skip is informational, not a failure: a file the configured size limit
    excludes is correct behavior and must still be reported rather than vanish.
    It produces no vector points, so it belongs in progress output only.
    """

    for skip in stage.connector.skipped:
        _progress(f"skipped {skip.path}: {skip.detail} [{skip.reason}]")


def _discover(stage: ChunkingStage, limit: int) -> list[SourceRecord]:
    """Discover records, letting the caller report a real discovery failure.

    Provider errors are re-raised as `DiscoveryFailed` so the report keeps a
    bounded, redacted detail instead of a traceback carrying a raw payload.
    """

    try:
        records = list(stage.connector.discover(ConnectorQuery(limit=limit)))
    except Exception as error:  # noqa: BLE001 - smoke reports a stable exit code
        raise DiscoveryFailed(safe_error(error)) from error
    _progress(f"discovered {len(records)} record(s) from {stage.connector_name!r}")
    return records


def _verdict(stage: ChunkingStage, outcome: StageOutcome) -> bool:
    """Run the chunking invariants and report them as progress, not as data.

    The checks decide the exit code; they describe the run rather than the
    vector store, so they never enter the JSON document.
    """

    checks = checks_for(
        outcome,
        stage.profile_for(outcome.result.profile),
        stage.indexing_config,
    )
    failed = [check for check in checks if not check.passed]
    for check in failed:
        _progress(f"check failed [{check.name}]: {check.detail}")
    return not failed


def _run_documents(
    stage: ChunkingStage,
    args: argparse.Namespace,
    records: list[SourceRecord],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Chunk every record, returning its vector document and the failed ids."""

    documents: list[dict[str, Any]] = []
    failed: list[str] = []
    for record in records:
        try:
            outcome = run_record(stage, record, profile_name=args.profile)
        except Exception as error:  # noqa: BLE001 - one failure must not hide the rest
            detail = safe_error(error)
            _progress(f"failed {record.id}: {detail}")
            documents.append(failure_report(record, error, detail))
            failed.append(record.id)
            continue
        passed = _verdict(stage, outcome)
        document = document_report(outcome, include_content=args.include_content)
        _progress(
            f"{'passed' if passed else 'failed'} {record.id}: "
            f"points={len(document['points'])} "
            f"strategy={outcome.result.strategy} "
            f"embedding_tokens={sum(p.token_count for p in outcome.prepared)}"
        )
        if not passed:
            failed.append(record.id)
        documents.append(document)
    return documents, failed


def _totals(documents: list[dict[str, Any]], failed: list[str]) -> dict[str, int]:
    points = [point for document in documents for point in document["points"]]
    return {
        "documents": len(documents),
        "failed_documents": len(failed),
        "points": len(points),
        "embedding_tokens": sum(point["embedding_input"]["token_count"] for point in points),
    }


def _sanitize(record_id: str) -> str:
    """Turn a record id into one safe path segment, as the connector smoke does.

    A local record id is an absolute `file://` URI, so a deeply nested corpus
    can exceed the 255-byte limit most filesystems place on a single name. Long
    ids keep a readable head plus a digest of the full id to stay unique.
    """

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", record_id).strip("_") or "document"
    if len(safe) <= MAX_NAME_LENGTH:
        return safe
    digest = sha256(record_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe[: MAX_NAME_LENGTH - len(digest) - 1]}-{digest}"


def _output_path(stage: ChunkingStage, record_id: str, args: argparse.Namespace) -> Path:
    target_dir = args.output_dir or DEFAULT_OUTPUT_DIR
    return target_dir / f"{stage.provider}-{_sanitize(record_id)}.{args.output}"


def _save_documents(
    stage: ChunkingStage,
    documents: list[dict[str, Any]],
    header: dict[str, Any],
    args: argparse.Namespace,
) -> list[str]:
    """Write one self-contained report per record so each page stands alone."""

    if not args.output:
        return []
    written: list[str] = []
    for document in documents:
        path = _output_path(stage, str(document["record_id"]), args)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{json.dumps({**header, **document}, indent=2)}\n",
            encoding="utf-8",
        )
        _progress(f"saved {document['record_id']} to {path}")
        written.append(str(path))
    return written


def _emit(report: dict[str, Any]) -> None:
    print(json.dumps(report, indent=2, sort_keys=False))


def main() -> int:
    args = _arguments()
    loaded = load_smoke_environment()
    _progress(f"environment files loaded: {', '.join(loaded) or 'none'}")
    try:
        stage = build_stage(args.connector)
    except ConfigurationError as error:
        _progress(f"not configured: {safe_error(error)}")
        return 2
    except ImportError as error:
        _progress(f"not configured: missing dependency: {safe_error(error)}")
        return 2

    discovery_error: str | None = None
    documents: list[dict[str, Any]] = []
    failed: list[str] = []
    try:
        try:
            documents, failed = _run_documents(stage, args, _discover(stage, args.limit))
        except DiscoveryFailed as error:
            discovery_error = str(error)
            _progress(f"discovery failed: {discovery_error}")
        _report_skips(stage)
    finally:
        stage.close()

    totals = _totals(documents, failed)
    # The document carries only what the vector store would hold. Connector,
    # parser, and chunking diagnostics are progress output on stderr.
    header = {"smoke": "ingestion-chunking-vector", "vector": vector_policy(stage)}
    saved = _save_documents(stage, documents, header, args)
    _emit(
        {
            **header,
            "documents": documents,
            "totals": totals,
            "status": "passed" if documents and not failed else "failed",
        }
    )
    for path in saved:
        _progress(f"wrote {path}")
    if discovery_error is not None:
        return 1
    if not documents:
        _progress("no records discovered")
        return 1
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
