"""Run every configured real connector smoke script."""

from __future__ import annotations

import confluence
import github
import jira
import local
import sharepoint
from bootstrap import ConnectorConfigurationError, connector_catalog, load_env

RUNNERS = {
    "confluence": confluence.run_confluence,
    "github": github.run_github,
    "jira": jira.run_jira,
    "local": local.run_local,
    "sharepoint": sharepoint.run_sharepoint,
}


def main() -> int:
    load_env()
    try:
        catalog = connector_catalog()
    except ConnectorConfigurationError as exc:
        print(f"[config] {exc}")
        return 2

    outcomes: dict[str, int] = {}
    for connection_id in catalog.names(enabled_only=True):
        definition = catalog.get(connection_id)
        run = RUNNERS.get(definition.provider)
        if run is None:
            print(
                f"\n=== {connection_id} ({definition.provider}) ===\n"
                "No smoke runner is available; skipping"
            )
            continue
        print(f"\n=== {connection_id} ({definition.provider}) ===")
        outcomes[connection_id] = run(connection_id=connection_id)

    if not outcomes:
        print("No enabled connections with smoke runners were configured.")
        return 2
    failed = {connection_id: code for connection_id, code in outcomes.items() if code != 0}
    if failed:
        print(f"Smoke failures: {failed}")
        return 1
    print(f"Smoke passed for connections: {', '.join(sorted(outcomes))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
