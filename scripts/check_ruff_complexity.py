"""Enforce a per-file ratchet for HarborRAG's existing Ruff complexity debt.

Usage:
    python scripts/check_ruff_complexity.py
    python scripts/check_ruff_complexity.py --write-baseline
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".ruff-complexity-baseline.json"
SELECTED_RULES = ("C901", "PLR0913")
BASELINE_VERSION = 1

type RuleCounts = dict[str, int]
type ComplexityCounts = dict[str, RuleCounts]


def aggregate_diagnostics(
    diagnostics: Sequence[Mapping[str, Any]],
    *,
    root: Path = REPO_ROOT,
) -> ComplexityCounts:
    """Group Ruff diagnostics by repository-relative file and rule."""

    grouped: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for diagnostic in diagnostics:
        code = diagnostic.get("code")
        if code not in SELECTED_RULES:
            raise ValueError(f"unsupported Ruff complexity rule: {code!r}")
        filename = Path(str(diagnostic["filename"]))
        relative = filename.relative_to(root) if filename.is_absolute() else filename
        grouped[relative.as_posix()][code] += 1
    return {
        filename: dict(sorted(rule_counts.items()))
        for filename, rule_counts in sorted(grouped.items())
    }


def compare_counts(
    current: Mapping[str, Mapping[str, int]],
    baseline: Mapping[str, Mapping[str, int]],
) -> list[str]:
    """Return regressions and stale downward-baseline entries."""

    failures: list[str] = []
    for filename in sorted(set(current) | set(baseline)):
        current_rules = current.get(filename, {})
        baseline_rules = baseline.get(filename, {})
        for rule in SELECTED_RULES:
            current_count = current_rules.get(rule, 0)
            baseline_count = baseline_rules.get(rule, 0)
            if current_count > baseline_count:
                failures.append(
                    f"{filename}: {rule} increased from {baseline_count} to {current_count}"
                )
            elif current_count < baseline_count:
                failures.append(
                    f"{filename}: {rule} decreased "
                    f"from {baseline_count} to {current_count}; "
                    "update the baseline downward"
                )
    return failures


def load_baseline(path: Path = BASELINE_PATH) -> ComplexityCounts:
    """Load and validate the committed complexity baseline."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load complexity baseline: {path}") from exc
    if payload.get("version") != BASELINE_VERSION:
        raise RuntimeError(f"unsupported complexity baseline version: {payload.get('version')!r}")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise RuntimeError("complexity baseline must contain a files mapping")
    return {
        str(filename): {str(rule): int(count) for rule, count in rule_counts.items()}
        for filename, rule_counts in files.items()
    }


def write_baseline(
    counts: Mapping[str, Mapping[str, int]],
    path: Path = BASELINE_PATH,
) -> None:
    """Write deterministic baseline data after an intentional reduction."""

    payload = {
        "version": BASELINE_VERSION,
        "files": counts,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_ruff(root: Path = REPO_ROOT) -> ComplexityCounts:
    """Run only the ratcheted Ruff rules and return grouped diagnostics."""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            ",".join(SELECTED_RULES),
            "--output-format",
            "json",
            "--no-cache",
            ".",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "Ruff complexity check failed")
    return aggregate_diagnostics(json.loads(result.stdout), root=root)


def _totals(counts: Mapping[str, Mapping[str, int]]) -> RuleCounts:
    totals = Counter(
        {rule: sum(values.get(rule, 0) for values in counts.values()) for rule in SELECTED_RULES}
    )
    return dict(totals)


def _summary(counts: Mapping[str, Mapping[str, int]]) -> str:
    totals = _totals(counts)
    return ", ".join(f"{rule}={totals[rule]}" for rule in SELECTED_RULES)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute ruff check run"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="replace the baseline with current tracked-file counts",
    )
    args = parser.parse_args(argv)

    current = run_ruff()
    if args.write_baseline:
        write_baseline(current)
        print(f"Updated Ruff complexity baseline: {_summary(current)}")
        return 0

    failures = compare_counts(current, load_baseline())
    if failures:
        print("Ruff complexity ratchet failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Ruff complexity baseline kept: {_summary(current)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
