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
import logging
import sys
from pathlib import Path

# Standalone-script mode: make the tests/ root importable so the shared library
# (graph_eval.corpus, .golden, .health) resolves the same way it does under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph_eval.health.diffing import diff_reports, reports_by_tenant  # noqa: E402
from graph_eval.smoke import configure_logging  # noqa: E402

logger = logging.getLogger("harborrag.graph_eval.graph_diff")


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--min-node-jaccard", type=float, default=1.0)
    parser.add_argument("--min-relation-jaccard", type=float, default=1.0)
    parser.add_argument("--allow-new-signatures", action="store_true")
    arguments = parser.parse_args()
    try:
        baselines = reports_by_tenant(json.loads(arguments.baseline.read_text()))
        currents = reports_by_tenant(json.loads(arguments.current.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as error:
        logger.error("unusable report input: %s", error)
        return 2
    failures: list[str] = []
    diffs: list[dict[str, object]] = []
    tenant_ids = sorted(set(baselines) | set(currents))
    for tenant_id in tenant_ids:
        # A tenant on only one side has no counterpart to compare against: the diff
        # would be vacuously clean, so it is the failure itself.
        if tenant_id not in baselines or tenant_id not in currents:
            side = "baseline" if tenant_id in baselines else "current"
            failures.append(f"{tenant_id}: only in the {side} report")
            continue
        diff = diff_reports(baselines[tenant_id], currents[tenant_id])
        diffs.append(diff.as_dict())
        tenant_failures: list[str] = []
        # A report without --identities makes diff_reports' jaccard None, which its own
        # gate then skips: the determinism gate would pass by comparing nothing. Fail
        # unless the caller opted out with a 0 bound.
        for label, jaccard, minimum in (
            ("node", diff.node_jaccard, arguments.min_node_jaccard),
            ("relation", diff.relation_jaccard, arguments.min_relation_jaccard),
        ):
            if jaccard is None and minimum > 0:
                tenant_failures.append(
                    f"{label} identities missing (run graph_health.py "
                    f"--identities, or pass --min-{label}-jaccard 0 to skip)"
                )
        tenant_failures.extend(
            diff.gate_failures(
                arguments.min_node_jaccard,
                arguments.min_relation_jaccard,
                allow_new_signatures=arguments.allow_new_signatures,
            )
        )
        failures.extend(f"{tenant_id}: {failure}" for failure in tenant_failures)
        logger.info(
            "%s: node jaccard %s, relation jaccard %s, nodes %d -> %d, relations %d -> %d, "
            "placeholders %d -> %d, signatures +%d -%d, %s",
            tenant_id,
            "missing" if diff.node_jaccard is None else f"{diff.node_jaccard:.3f}",
            "missing" if diff.relation_jaccard is None else f"{diff.relation_jaccard:.3f}",
            *diff.node_count_delta,
            *diff.relation_count_delta,
            *diff.placeholder_count_delta,
            len(diff.added_signatures),
            len(diff.removed_signatures),
            f"{len(tenant_failures)} failed" if tenant_failures else "pass",
        )
    # JSON is for machines: emit it only when stdout is piped/redirected, so an
    # interactive run shows just the summary and verdict.
    if not sys.stdout.isatty():
        print(json.dumps(diffs, indent=2, sort_keys=True))
    for failure in failures:
        logger.error("GATE FAILURE %s", failure)
    verdict = logger.error if failures else logger.info
    verdict(
        "%s: %d gate failures across %d tenants",
        "FAIL" if failures else "PASS",
        len(failures),
        len(tenant_ids),
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
