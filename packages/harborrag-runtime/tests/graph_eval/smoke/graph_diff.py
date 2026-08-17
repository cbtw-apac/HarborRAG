"""Compare two graph health reports produced by graph_health.py --identities.

Usage:
    .venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/graph_diff.py \
        baseline.json current.json [--min-node-jaccard 1.0] \
        [--min-relation-jaccard 1.0] [--allow-new-signatures]

Exit codes: 0 within bounds, 1 gate failure, 2 unusable inputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Standalone-script mode: make the tests/ root importable so the shared library
# (graph_eval.corpus, .golden, .health) resolves the same way it does under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph_eval.health.diffing import diff_reports  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--min-node-jaccard", type=float, default=1.0)
    parser.add_argument("--min-relation-jaccard", type=float, default=1.0)
    parser.add_argument("--allow-new-signatures", action="store_true")
    arguments = parser.parse_args()
    try:
        baselines = {r["tenant_id"]: r for r in json.loads(arguments.baseline.read_text())}
        currents = {r["tenant_id"]: r for r in json.loads(arguments.current.read_text())}
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"unusable report input: {error}", file=sys.stderr)
        return 2
    failures: list[str] = []
    diffs: list[dict[str, object]] = []
    for tenant_id in sorted(set(baselines) | set(currents)):
        # A tenant on only one side has no counterpart to compare against: the diff
        # would be vacuously clean, so it is the failure itself.
        if tenant_id not in baselines or tenant_id not in currents:
            failures.append(f"{tenant_id}: present in only one report")
            continue
        diff = diff_reports(baselines[tenant_id], currents[tenant_id])
        diffs.append(diff.as_dict())
        # A report without --identities makes diff_reports' jaccard None, which its own
        # gate then skips: the determinism gate would pass by comparing nothing. Fail
        # unless the caller opted out with a 0 bound.
        for label, jaccard, minimum in (
            ("node", diff.node_jaccard, arguments.min_node_jaccard),
            ("relation", diff.relation_jaccard, arguments.min_relation_jaccard),
        ):
            if jaccard is None and minimum > 0:
                failures.append(
                    f"{tenant_id}: {label} identities missing (run graph_health.py "
                    f"--identities, or pass --min-{label}-jaccard 0 to skip)"
                )
        failures.extend(
            f"{tenant_id}: {failure}"
            for failure in diff.gate_failures(
                arguments.min_node_jaccard,
                arguments.min_relation_jaccard,
                allow_new_signatures=arguments.allow_new_signatures,
            )
        )
    print(json.dumps(diffs, indent=2, sort_keys=True))
    for failure in failures:
        print(f"GATE FAILURE {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
