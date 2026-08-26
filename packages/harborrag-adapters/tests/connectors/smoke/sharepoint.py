"""Smoke check a configured SharePoint connection."""

from __future__ import annotations

from bootstrap import (
    ConnectorConfigurationError,
    build_connector,
    load_env,
    print_document,
    print_failure,
)

from harborrag_adapters.connectors.schemas import ConnectorQuery


def run_sharepoint(*, connection_id: str | None = None, limit: int = 3) -> int:
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
    except Exception as exc:  # noqa: BLE001 - smoke runner returns a stable exit code
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
    except Exception as exc:  # noqa: BLE001 - smoke runner returns a stable exit code
        print_failure("sharepoint", exc)
        return 1
    print_document("sharepoint", document)
    return 0


def main() -> int:
    return run_sharepoint()


if __name__ == "__main__":
    raise SystemExit(main())
