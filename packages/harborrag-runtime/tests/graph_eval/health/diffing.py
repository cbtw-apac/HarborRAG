"""Diff two graph health reports: identity Jaccard, signature/placeholder drift, censuses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Any, not object: reports are decoded from JSON baselines, so their values arrive
# as untyped scalars, lists and mappings.
Report = Mapping[str, Any]


def _jaccard(left: set[str], right: set[str]) -> float:
    # Callers must reject empty identity lists first: two empty sets would score a
    # perfect 1.0, which is the determinism gate passing by comparing nothing.
    return len(left & right) / len(left | right)


@dataclass(frozen=True, slots=True)
class GraphDiff:
    tenant_id: str
    node_jaccard: float | None
    relation_jaccard: float | None
    added_signatures: tuple[str, ...]
    removed_signatures: tuple[str, ...]
    relation_count_deltas: dict[str, tuple[int, int]]
    node_count_delta: tuple[int, int]
    relation_count_delta: tuple[int, int]
    placeholder_count_delta: tuple[int, int]

    def gate_failures(
        self,
        min_node_jaccard: float,
        min_relation_jaccard: float,
        *,
        allow_new_signatures: bool,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        # A node flipped to placeholder changes only node properties, so every identity,
        # census and count key stays byte-identical -- observed live, 13 -> 14. The
        # min_node_jaccard 0 opt-out (census-only diffing) has to silence this too.
        baseline_placeholders, current_placeholders = self.placeholder_count_delta
        if min_node_jaccard > 0 and baseline_placeholders != current_placeholders:
            failures.append(f"placeholder count {baseline_placeholders} -> {current_placeholders}")
        if self.node_jaccard is not None and self.node_jaccard < min_node_jaccard:
            failures.append(f"node identity jaccard {self.node_jaccard:.3f} < {min_node_jaccard}")
        if self.relation_jaccard is not None and self.relation_jaccard < min_relation_jaccard:
            failures.append(
                f"relation identity jaccard {self.relation_jaccard:.3f} < {min_relation_jaccard}"
            )
        if self.added_signatures and not allow_new_signatures:
            failures.append(f"new edge signatures: {', '.join(self.added_signatures)}")
        return tuple(failures)

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "node_jaccard": self.node_jaccard,
            "relation_jaccard": self.relation_jaccard,
            "added_signatures": list(self.added_signatures),
            "removed_signatures": list(self.removed_signatures),
            "relation_count_deltas": {
                key: list(value) for key, value in self.relation_count_deltas.items()
            },
            "node_count_delta": list(self.node_count_delta),
            "relation_count_delta": list(self.relation_count_delta),
            "placeholder_count_delta": list(self.placeholder_count_delta),
        }


def reports_by_tenant(payload: Any) -> dict[str, Report]:
    """Index report entries by tenant, rejecting duplicates rather than keeping the last."""

    if not isinstance(payload, list):
        raise ValueError("report payload must be a JSON array of tenant reports")
    reports: dict[str, Report] = {}
    for entry in payload:
        tenant_id = str(entry["tenant_id"])
        if tenant_id in reports:
            raise ValueError(f"duplicate tenant_id {tenant_id!r}")
        reports[tenant_id] = entry
    return reports


def _identity_jaccard(baseline: Report, current: Report, key: str) -> float | None:
    left, right = baseline.get(key), current.get(key)
    # A null (hand-edited report) or empty (wiped tenant) identity list is as unusable as
    # an absent one -- graph_diff turns None into a failure unless the caller opted out
    # with a 0 bound.
    if not left or not right:
        return None
    return _jaccard(set(left), set(right))


def diff_reports(baseline: Report, current: Report) -> GraphDiff:
    baseline_signatures = set(baseline.get("signature_census", {}))
    current_signatures = set(current.get("signature_census", {}))
    baseline_relations: dict[str, int] = dict(baseline.get("relations_by_type", {}))
    current_relations: dict[str, int] = dict(current.get("relations_by_type", {}))
    deltas = {
        relation_type: (
            baseline_relations.get(relation_type, 0),
            current_relations.get(relation_type, 0),
        )
        for relation_type in sorted(set(baseline_relations) | set(current_relations))
        if baseline_relations.get(relation_type, 0) != current_relations.get(relation_type, 0)
    }
    return GraphDiff(
        tenant_id=str(current.get("tenant_id", "")),
        node_jaccard=_identity_jaccard(baseline, current, "node_keys"),
        relation_jaccard=_identity_jaccard(baseline, current, "relation_ids"),
        added_signatures=tuple(sorted(current_signatures - baseline_signatures)),
        removed_signatures=tuple(sorted(baseline_signatures - current_signatures)),
        relation_count_deltas=deltas,
        node_count_delta=(int(baseline.get("node_count", 0)), int(current.get("node_count", 0))),
        relation_count_delta=(
            int(baseline.get("relation_count", 0)),
            int(current.get("relation_count", 0)),
        ),
        placeholder_count_delta=(
            int(baseline.get("placeholder_count", 0)),
            int(current.get("placeholder_count", 0)),
        ),
    )
