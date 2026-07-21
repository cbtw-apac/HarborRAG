from __future__ import annotations

import chat
import embed
import rerank

TARGETS = {
    "chat": chat.main,
    "embed": embed.main,
    "rerank": rerank.main,
}


def main() -> int:
    outcomes: dict[str, int] = {}
    for name, run in TARGETS.items():
        print(f"\n=== models/{name} ===")
        code = run()
        if code != 2:
            outcomes[name] = code

    if not outcomes:
        print("No model smoke target is configured. Fill .env and run again.")
        return 2
    failed = {name: code for name, code in outcomes.items() if code != 0}
    if failed:
        print(f"Model smoke failures: {failed}")
        return 1
    print(f"Model smoke passed for: {', '.join(sorted(outcomes))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
