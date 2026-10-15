"""Smoke check a configured SharePoint connection."""

from __future__ import annotations

from bootstrap import (
    ConnectorConfigurationError,
    build_connector,
    load_env,
    print_document,
    print_failure,
)

from harborrag_adapters.connectors.exceptions import ConnectorError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord


def run_sharepoint(*, connection_id: str | None = None, limit: int = 10) -> int:
    """Discover up to ``limit`` SharePoint records and load one page and one file.

    Uses the ``sharepoint`` connection unless ``connection_id`` is given.
    Pages are discovered before files, so with ``include_pages`` on the first
    page and the first file in the discovered set are both loaded. Returns a
    process exit code: 0 on success, 1 when discovery or loading fails or finds
    no records, 2 when the connection is not configured.
    """
    load_env()
    identifier = connection_id or "sharepoint"
    try:
        connector = build_connector(
            identifier,
            include_attachments=False,
            expected_provider="sharepoint",
        )
    except ConnectorConfigurationError as exc:
        print(f"[sharepoint] not configured: {exc}")
        return 2

    try:
        records = list(connector.discover(ConnectorQuery(limit=limit)))
    except ConnectorError as exc:  # smoke runner maps connector failure to exit 1
        print_failure("sharepoint", exc)
        return 1
    print(f"\n[sharepoint] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - [{_record_kind(record)}] {record.id} ({record.source_type})")
    if not records:
        print("[sharepoint] no records discovered")
        return 1

    samples: dict[str, SourceRecord] = {}
    for record in records:
        samples.setdefault(_record_kind(record), record)
    for kind in ("page", "file"):
        if kind not in samples:
            print(f"[sharepoint] no {kind} discovered in the first {limit} record(s)")
    for kind, record in samples.items():
        try:
            document = connector.load(record)
        except ConnectorError as exc:
            print_failure("sharepoint", exc)
            return 1
        print(f"\n[sharepoint] sample {kind}")
        print_document("sharepoint", document)
    return 0


def _record_kind(record: SourceRecord) -> str:
    return "page" if record.metadata.get("record_kind") == "page" else "file"


def main() -> int:
    """Run the SharePoint smoke check end-to-end."""
    return run_sharepoint()


if __name__ == "__main__":
    raise SystemExit(main())
