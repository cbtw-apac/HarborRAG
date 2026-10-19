"""Join descriptions only after graph visibility, with fresh authority/ACL checks."""

from harborrag_core.ingestion import GraphNodeRecord, KnowledgeNodeKind
from harborrag_core.ports.summary_projection import SummaryReaderPort
from harborrag_core.security.context import AccessContext


def _may_carry_card(node: GraphNodeRecord, entitled: frozenset[str] | None) -> bool:
    """Whether a reader with this datasource set may be shown ``node``'s card.

    The summary authority applies the same rule to what it is asked for; not asking keeps
    the card out of any reader that does not. The tenant card summarises every
    datasource, so a node with no datasource of its own gets none.
    """

    if entitled is None:
        return True
    return node.node_kind != KnowledgeNodeKind.TENANT and node.source_scope_id in entitled


async def apply_summary_views(
    nodes: tuple[GraphNodeRecord, ...],
    reader: SummaryReaderPort | None,
    access: AccessContext,
) -> tuple[GraphNodeRecord, ...]:
    if reader is None:
        return nodes
    eligible = tuple(
        node
        for node in nodes
        if node.node_kind
        in {
            KnowledgeNodeKind.STRUCTURE,
            KnowledgeNodeKind.DOCUMENT_VERSION,
            KnowledgeNodeKind.SOURCE_ENTITY,
            KnowledgeNodeKind.DATA_SOURCE,
            KnowledgeNodeKind.TENANT,
        }
        and _may_carry_card(node, access.entitled_source_scopes)
    )
    views = await reader.views(
        str(access.tenant_id),
        tuple(dict.fromkeys(node.node_key for node in eligible)),
        access=access,
        source_scopes={
            node.node_key: node.source_scope_id or "@tenant"
            for node in eligible
            if node.source_scope_id or node.node_kind == KnowledgeNodeKind.TENANT
        },
    )
    result = []
    for node in nodes:
        view = views.get(node.node_key)
        if view is None:
            result.append(node)
            continue
        description = (
            view.card.description
            if view.card
            else (
                f"{node.entity_type.value.replace('_', ' ').capitalize()} in the source topology."
            )
        )
        result.append(node.model_copy(update={"description": description, "summary": view}))
    return tuple(result)
