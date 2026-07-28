"""Run every configured real connector smoke script."""

from __future__ import annotations

import confluence
import github
import jira
import local
import sharepoint

PROVIDERS = {
    "confluence": confluence.main,
    "github": github.main,
    "jira": jira.main,
    "local": local.main,
    "sharepoint": sharepoint.main,
}


def main() -> int:
    outcomes: dict[str, int] = {}
    for name, run in PROVIDERS.items():
        print(f"\n=== {name} ===")
        code = run()
        if code == 2:
            continue
        outcomes[name] = code

    if not outcomes:
        print("No providers were configured. Fill repo-root .env and run again.")
        return 2
    failed = {name: code for name, code in outcomes.items() if code != 0}
    if failed:
        print(f"Smoke failures: {failed}")
        return 1
    print(f"Smoke passed for: {', '.join(sorted(outcomes))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
