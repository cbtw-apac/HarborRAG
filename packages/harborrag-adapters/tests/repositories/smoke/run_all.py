from __future__ import annotations

import qdrant

TARGETS = {
    "qdrant": qdrant.main,
}


def main() -> int:
    outcomes: dict[str, int] = {}
    for name, run in TARGETS.items():
        print(f"\n=== repositories/{name} ===")
        outcomes[name] = run()

    failed = {name: code for name, code in outcomes.items() if code == 1}
    unavailable = sorted(name for name, code in outcomes.items() if code == 2)
    passed = sorted(name for name, code in outcomes.items() if code == 0)
    if failed:
        print(f"Repository smoke failures: {failed}")
        return 1
    if unavailable:
        print(f"Repository smoke incomplete; unavailable: {', '.join(unavailable)}")
        if passed:
            print(f"Passed: {', '.join(passed)}")
        return 2
    print(f"Repository smoke passed for: {', '.join(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
