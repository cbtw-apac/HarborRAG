"""Filter evidence by a source item's own fields, and reach what is attached to it.

A Jira issue carries its typed custom fields (``fields.skill_set``); the CV
attached to it is a document of its own and carries none of them. Copying the
issue's fields onto every attachment would go stale the moment a recruiter edits
the issue, and re-ingesting -- re-OCRing -- attachments on every field edit is the
wrong price for keeping them current. So the fields stay where they belong, and
retrieval follows the attachment link instead: first the items whose fields
match, then the evidence of those items *and* of everything attached to them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition

if TYPE_CHECKING:
    from harborrag_core.ports.storage import VectorRepositoryPort
    from harborrag_core.storage import StorageOperationContext

FIELD_FILTER_PREFIX = "fields."

# The traced item ids become one set-membership condition. Past this many the
# filter is not narrowing anything useful, and a silently truncated set would
# drop matching items at random -- so the caller is asked to narrow it instead.
MAX_TRACED_ITEMS = 10_000


def split_field_filters(
    filters: VectorFilter | None,
) -> tuple[VectorFilter | None, VectorFilter | None]:
    """Separate ``fields.*`` conditions from the rest of an evidence filter.

    Returns ``(field_filter, remaining_filter)``, each ``None`` when empty.
    """

    if filters is None:
        return None, None
    fields: dict[str, list[VectorFilterCondition]] = {"must": [], "should": [], "must_not": []}
    rest: dict[str, list[VectorFilterCondition]] = {"must": [], "should": [], "must_not": []}
    for clause in fields:
        for condition in getattr(filters, clause):
            target = fields if condition.field.startswith(FIELD_FILTER_PREFIX) else rest
            target[clause].append(condition)
    return (
        VectorFilter(**fields) if any(fields.values()) else None,
        VectorFilter(**rest) if any(rest.values()) else None,
    )


@dataclass(frozen=True)
class SourceFieldTrace:
    """Turn a ``fields.*`` filter into "these items and their attachments"."""

    vectors: VectorRepositoryPort
    index_name: str
    limit: int = MAX_TRACED_ITEMS

    async def matching_items(
        self,
        field_filter: VectorFilter | None,
        *,
        context: StorageOperationContext,
    ) -> tuple[str, ...]:
        """The source items whose own fields satisfy the filter (already permission-scoped)."""

        items = await self.vectors.distinct_values(
            self.index_name,
            "source_item_id",
            filters=field_filter,
            limit=self.limit + 1,
            context=context,
        )
        if len(items) > self.limit:
            raise HarborValidationError(
                f"the fields filter matches more than {self.limit} items; narrow it",
                details={"limit": self.limit},
            )
        return items

    @staticmethod
    def scope(items: tuple[str, ...], rest: VectorFilter | None) -> VectorFilter:
        """Evidence of the matched items, or of anything attached to one of them.

        The two memberships go in ``should``: a point qualifies when either holds.
        Any ``should`` the caller already had would then be satisfied by the trace
        alone, so those are rejected rather than silently loosened.
        """

        remaining = rest or VectorFilter()
        if remaining.should:
            raise HarborValidationError(
                "a fields filter cannot be combined with other alternative conditions"
            )
        return VectorFilter(
            must=list(remaining.must),
            should=[
                VectorFilterCondition(
                    field="source_item_id", operator=FilterOperator.IN, value=list(items)
                ),
                VectorFilterCondition(
                    field="parent_source_item_id", operator=FilterOperator.IN, value=list(items)
                ),
            ],
            must_not=list(remaining.must_not),
        )


__all__ = [
    "FIELD_FILTER_PREFIX",
    "MAX_TRACED_ITEMS",
    "SourceFieldTrace",
    "split_field_filters",
]
