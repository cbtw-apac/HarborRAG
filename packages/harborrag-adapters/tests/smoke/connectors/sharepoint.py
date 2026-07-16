"""Smoke check a real SharePoint connection using repo-root `.env`."""
from __future__ import annotations

from _bootstrap import env, load_env, print_document
from harborrag_adapters.connectors import HarborConnector
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig


def missing_vars() -> list[str]:
    missing = []
    if not env("SHAREPOINT_SITE_URL") and not env("SHAREPOINT_SITE_ID"):
        missing.append("SHAREPOINT_SITE_URL (or SHAREPOINT_SITE_ID)")

    has_token = bool(env("MICROSOFT_GRAPH_TOKEN"))
    has_client_creds = bool(
        env("MICROSOFT_TENANT_ID")
        and env("MICROSOFT_CLIENT_ID")
        and env("MICROSOFT_CLIENT_SECRET")
    )
    if not has_token and not has_client_creds:
        missing.append(
            "MICROSOFT_GRAPH_TOKEN (or MICROSOFT_TENANT_ID+MICROSOFT_CLIENT_ID+"
            "MICROSOFT_CLIENT_SECRET)"
        )
    return missing


def main() -> int:
    load_env()
    if missing := missing_vars():
        print(f"[sharepoint] missing env vars: {missing}")
        return 2

    config = SharePointSiteConfig(
        site_url=env("SHAREPOINT_SITE_URL"),
        site_id=env("SHAREPOINT_SITE_ID"),
        drive_name=env("SHAREPOINT_DRIVE_NAME"),
        access_token=env("MICROSOFT_GRAPH_TOKEN"),
        tenant_id=env("MICROSOFT_TENANT_ID"),
        client_id=env("MICROSOFT_CLIENT_ID"),
        client_secret=env("MICROSOFT_CLIENT_SECRET"),
    )

    connector = HarborConnector("sharepoint", config=config)
    records = list(connector.discover(ConnectorQuery(limit=3)))
    print(f"\n[sharepoint] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[sharepoint] no records discovered")
        return 1

    document = connector.load(records[0])
    print_document("sharepoint", document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
