"""Smoke check a real GitHub connection using repo-root `.env`."""

from __future__ import annotations

from bootstrap import env, load_env, print_document

from harborrag_adapters.connectors import HarborConnector
from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_adapters.connectors.schemas import ConnectorQuery


def missing_vars() -> list[str]:
    missing = []
    if not env("GITHUB_REPOSITORY_URL") and not (env("GITHUB_OWNER") and env("GITHUB_REPO")):
        missing.append("GITHUB_OWNER+GITHUB_REPO (or GITHUB_REPOSITORY_URL)")
    if not env("GITHUB_TOKEN"):
        missing.append("GITHUB_TOKEN")
    return missing


def main() -> int:
    load_env()
    if missing := missing_vars():
        print(f"[github] missing env vars: {missing}")
        return 2

    config = GitHubRepositoryConfig(
        owner=env("GITHUB_OWNER"),
        repo=env("GITHUB_REPO"),
        repository_url=env("GITHUB_REPOSITORY_URL"),
        token=env("GITHUB_TOKEN"),
        ref=env("GITHUB_REF"),
    )

    connector = HarborConnector("github", config=config)
    records = list(connector.discover(ConnectorQuery(limit=3)))
    print(f"\n[github] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[github] no records discovered")
        return 1

    document = connector.load(records[0])
    print_document("github", document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
