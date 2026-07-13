"""Smoke check a real JIRA connection using repo-root `.env`."""
from __future__ import annotations

from _bootstrap import env, load_env, print_document
from harborrag_adapters.connectors import HarborConnector
from harborrag_adapters.connectors.jira.config import JiraProjectConfig
from harborrag_adapters.connectors.schemas import ConnectorQuery


def missing_vars() -> list[str]:
    missing = [] if env("JIRA_BASE_URL") else ["JIRA_BASE_URL"]
    if not (env("JIRA_TOKEN") or env("JIRA_API_TOKEN")):
        missing.append("JIRA_TOKEN (or JIRA_API_TOKEN)")
    return missing


def _config(*, include_attachments: bool) -> JiraProjectConfig:
    project_key = env("JIRA_PROJECT_KEY")
    return JiraProjectConfig(
        base_url=env("JIRA_BASE_URL"),
        token=env("JIRA_TOKEN") or env("JIRA_API_TOKEN"),
        email=env("JIRA_EMAIL"),
        project_keys=[project_key] if project_key else [],
        include_comments=False,
        include_attachments=include_attachments,
        include_changelog=False,
    )


def main() -> int:
    load_env()
    if missing := missing_vars():
        print(f"[jira] missing env vars: {missing}")
        return 2

    connector = HarborConnector("jira", config=_config(include_attachments=False))
    records = list(connector.discover(ConnectorQuery(limit=3)))
    print(f"\n[jira] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[jira] no records discovered")
        return 1

    print("\n[jira] === load without attachments ===")
    document = connector.load(records[0])
    print_document("jira", document)

    print("\n[jira] === load with attachments ===")
    with_attachments = HarborConnector("jira", config=_config(include_attachments=True))
    document_with_attachments = with_attachments.load(records[0])
    print_document("jira", document_with_attachments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
