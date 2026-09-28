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


def run_sharepoint(*, connection_id: str | None = None, limit: int = 3) -> int:
    """Discover up to ``limit`` SharePoint records and load the first one.

    Uses the ``sharepoint`` connection unless ``connection_id`` is given.
    Returns a process exit code: 0 on success, 1 when discovery or loading
    fails or finds no records, 2 when the connection is not configured.
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
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[sharepoint] no records discovered")
        return 1

    try:
        document = connector.load(records[0])
    except ConnectorError as exc:
        print_failure("sharepoint", exc)
        return 1
    print_document("sharepoint", document)
    return 0


def main() -> int:
    """Run the SharePoint smoke check end-to-end."""
    return run_sharepoint()


if __name__ == "__main__":
    raise SystemExit(main())
