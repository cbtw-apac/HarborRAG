"""Smoke check a real local-files connection using repo-root `.env`."""
from __future__ import annotations

from _bootstrap import env_path, load_env, print_document

from harborrag_adapters.connectors import HarborConnector
from harborrag_adapters.connectors.local.config import LocalFileConfig
from harborrag_adapters.connectors.schemas import ConnectorQuery


def missing_vars() -> list[str]:
    path = env_path("LOCAL_SOURCE_PATH")
    if not path:
        return ["LOCAL_SOURCE_PATH"]
    if not path.exists():
        return [f"LOCAL_SOURCE_PATH ({path} does not exist)"]
    return []


def main() -> int:
    load_env()
    if missing := missing_vars():
        print(f"[local] missing env vars: {missing}")
        return 2

    config = LocalFileConfig(source_path=env_path("LOCAL_SOURCE_PATH"))
    connector = HarborConnector("local", config=config)
    records = list(connector.discover(ConnectorQuery(limit=5)))
    print(f"\n[local] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[local] no records discovered")
        return 1

    document = connector.load(records[0])
    print_document("local", document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
