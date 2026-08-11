"""Smoke check a configured GitHub connection."""

from __future__ import annotations

from bootstrap import ConnectorConfigurationError, build_connector, load_env, print_document

from harborrag_adapters.connectors.schemas import ConnectorQuery


def run_github(*, connection_id: str | None = None, limit: int = 3) -> int:
    load_env()
    identifier = connection_id or "github"
    try:
        connector = build_connector(
            identifier,
            include_attachments=False,
            expected_provider="github",
        )
    except ConnectorConfigurationError as exc:
        print(f"[github] not configured: {exc}")
        return 2

    records = list(connector.discover(ConnectorQuery(limit=limit)))
    print(f"\n[github] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[github] no records discovered")
        return 1

    document = connector.load(records[0])
    print_document("github", document)
    return 0


def main() -> int:
    return run_github()


if __name__ == "__main__":
    raise SystemExit(main())
