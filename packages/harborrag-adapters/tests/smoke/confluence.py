"""Smoke check a real Confluence connection using repo-root `.env`."""
from __future__ import annotations

from _bootstrap import env, load_env, print_document

from harborrag_adapters.connectors import HarborConnector
from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
from harborrag_adapters.connectors.schemas import ConnectorQuery


def missing_vars() -> list[str]:
    required = ("CONFLUENCE_BASE_URL", "CONFLUENCE_SPACE_KEY", "CONFLUENCE_TOKEN")
    return [name for name in required if not env(name)]


def _config(*, include_attachments: bool) -> ConfluenceSpaceConfig:
    return ConfluenceSpaceConfig(
        space_key=env("CONFLUENCE_SPACE_KEY"),
        base_url=env("CONFLUENCE_BASE_URL"),
        token=env("CONFLUENCE_TOKEN"),
        email=env("CONFLUENCE_EMAIL"),
        include_comments=False,
        include_attachments=include_attachments,
    )


def main() -> int:
    load_env()
    if missing := missing_vars():
        print(f"[confluence] missing env vars: {missing}")
        return 2

    connector = HarborConnector("confluence", config=_config(include_attachments=False))
    records = list(connector.discover(ConnectorQuery(limit=3)))
    print(f"\n[confluence] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[confluence] no records discovered")
        return 1

    print("\n[confluence] === load without attachments ===")
    document = connector.load(records[0])
    print_document("confluence", document)

    print("\n[confluence] === load with attachments ===")
    with_attachments = HarborConnector(
        "confluence", config=_config(include_attachments=True)
    )
    document_with_attachments = with_attachments.load(records[0])
    print_document("confluence", document_with_attachments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
