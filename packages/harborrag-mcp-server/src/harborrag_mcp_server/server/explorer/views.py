"""Shape reader-tool results into the rows and messages the explorer UI shows.

Pure functions over the reader tools' validated output. They never widen what a
tool returned: every value here came back through the audited handler, and
anything the UI shows or sends to the chat is a subset of it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SNIPPET_CHARACTERS = 280
SEARCH_LABELS = {"evidence": "evidence", "entities": "entity"}
_CHAT_TEXT_CHARACTERS = 1_200
_CHAT_HITS = 5
_CHART_LABEL_CHARACTERS = 40
_UNSAFE_LABEL = re.compile(r"[^\w\s.,:/#()\-]")


def snippet(text: object, limit: int = SNIPPET_CHARACTERS) -> str:
    """Collapse whitespace and cut ``text`` to ``limit`` characters."""

    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def section_label(section: object) -> str:
    if not isinstance(section, Sequence) or isinstance(section, str):
        return ""
    return " / ".join(str(part) for part in section)


def search_hits(result: Mapping[str, object]) -> list[dict[str, Any]]:
    """Rows for a ``vector_search`` result."""

    hits: list[dict[str, Any]] = []
    for rank, hit in enumerate(_mappings(result.get("results")), start=1):
        meta = _mapping(hit.get("metadata"))
        relevance = hit.get("relevance")
        hits.append(
            {
                "rank": rank,
                "title": meta.get("document_title") or meta.get("document_id") or "",
                "section": section_label(meta.get("section_path")),
                "snippet": snippet(hit.get("text")),
                "relevance": round(relevance, 3) if isinstance(relevance, int | float) else None,
                "evidence_id": hit.get("id", ""),
                "document_id": meta.get("document_id") or "",
            }
        )
    return hits


def entity_rows(result: Mapping[str, object]) -> list[dict[str, Any]]:
    """Rows for a ``find_entities`` result."""

    rows: list[dict[str, Any]] = []
    for match in _mappings(result.get("matches")):
        evidence = [item for item in _sequence(match.get("evidence_chunk_ids")) if item]
        attributes = "; ".join(
            f"{attribute.get('name')}: {', '.join(str(v) for v in _sequence(attribute.get('values')))}"
            for attribute in _mappings(match.get("attributes"))
        )
        score = match.get("score")
        rows.append(
            {
                "rank": match.get("rank"),
                "description": snippet(match.get("description")),
                "attributes": snippet(attributes, 160),
                "score": round(score, 3) if isinstance(score, int | float) else None,
                "coverage": match.get("coverage_mode") or "",
                "evidence_count": len(evidence),
                "first_evidence_id": evidence[0] if evidence else "",
                "node_key": match.get("node_key", ""),
            }
        )
    return rows


def search_chat_message(query: str, hits: Sequence[Mapping[str, Any]], mode: str) -> str:
    """A chat message asking the assistant to answer from the top hits."""

    if not hits:
        return ""
    lines = [f'Answer "{query}" using these HarborRAG {mode} results:']
    for hit in hits[:_CHAT_HITS]:
        where = f" / {hit['section']}" if hit.get("section") else ""
        lines.append(f"- {hit['title']}{where} (evidence {hit['evidence_id']})")
    lines.append("Read them with fetch_evidence before citing.")
    return "\n".join(lines)


def entity_chat_message(query: str, rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    lines = [f'Answer "{query}" using these HarborRAG entities:']
    for row in rows[:_CHAT_HITS]:
        lines.append(f"- {row['description']} (node {row['node_key']})")
    lines.append("Fetch their evidence with fetch_evidence before citing.")
    return "\n".join(lines)


def evidence_reader(
    item: Mapping[str, object],
    context: Mapping[str, object] | None,
) -> dict[str, Any]:
    """The reader view for one fetched evidence chunk and its reading window."""

    chunk_id = str(item.get("chunk_id") or "")
    title = str(item.get("document_title") or item.get("document_id") or chunk_id)
    section = section_label(item.get("section_path"))
    text = item.get("text") if isinstance(item.get("text"), str) else ""
    window = _context_chunks(context, anchor=chunk_id)
    quoted = snippet(text, _CHAT_TEXT_CHARACTERS)
    where = f" / {section}" if section else ""
    return {
        "kind": "evidence",
        "title": title,
        "section": section,
        "text": text,
        "evidence_id": chunk_id,
        "document_id": item.get("document_id") or "",
        "document_version_id": item.get("document_version_id") or "",
        "connector_type": item.get("connector_type") or "",
        "chunks": window,
        "outline": [],
        "next_cursor": _next_cursor(context),
        "summary": f"Evidence {chunk_id}" + (f", {len(window)} chunk window" if window else ""),
        "chat_message": (
            f"Use this HarborRAG evidence from {title}{where} (evidence {chunk_id}):\n\n"
            f"> {quoted}\n\nCite evidence {chunk_id}."
        ),
        "context_message": f"HarborRAG evidence {chunk_id} from {title}{where}: {quoted}",
    }


def document_reader(context: Mapping[str, object]) -> dict[str, Any]:
    """The reader view for one window of a document."""

    document_id = str(context.get("document_id") or "")
    title = str(context.get("document_title") or document_id)
    chunks = _context_chunks(context, anchor="")
    outcome = context.get("outcome")
    outline = [section_label(path) for path in _sequence(context.get("outline"))]
    body = "\n\n".join(f"{chunk['section']}: {chunk['text']}" for chunk in chunks[:3])
    ids = ", ".join(chunk["chunk_id"] for chunk in chunks)
    return {
        "kind": "document",
        "title": title,
        "section": f"{len(chunks)} chunk(s)" + ("" if outcome == "ok" else f" ({outcome})"),
        "text": "",
        "evidence_id": "",
        "document_id": document_id,
        "document_version_id": context.get("document_version_id") or "",
        # The context window does not carry the connector; the badge stays hidden.
        "connector_type": "",
        "chunks": chunks,
        "outline": [line for line in outline if line],
        "next_cursor": _next_cursor(context),
        "summary": f"Document {document_id}",
        "chat_message": (
            f"Use this HarborRAG document window from {title} (document {document_id}, "
            f"evidence {ids}):\n\n{snippet(body, _CHAT_TEXT_CHARACTERS)}"
        ),
        "context_message": f"HarborRAG document {title} ({document_id}): "
        + snippet(body, _CHAT_TEXT_CHARACTERS),
    }


def browse_rows(kind: str, result: Mapping[str, object]) -> dict[str, Any]:
    """Rows for a ``list_documents`` or ``list_sources`` page."""

    if kind == "documents":
        columns = (
            "document_id",
            "document_version_id",
            "title",
            "source_scope_id",
            "connector_type",
            "chunk_count",
        )
        items = _mappings(result.get("documents"))
    else:
        columns = (
            "source_id",
            "name",
            "connector_type",
            "ingestion_state",
            "active_document_count",
            "last_successful_ingestion_at",
        )
        items = _mappings(result.get("sources"))
    rows = [{column: item.get(column) for column in columns} for item in items]
    next_cursor = _next_cursor(result)
    more = "; more on the next page" if next_cursor else ""
    return {
        "kind": kind,
        "rows": rows,
        "next_cursor": next_cursor,
        "summary": f"{len(rows)} {kind} on this page{more}",
    }


def candidate_rows(result: Mapping[str, object]) -> dict[str, Any]:
    """Rows for a ``resolve_graph_nodes`` result."""

    rows = [
        {
            "node_key": candidate.get("node_key", ""),
            "title": candidate.get("title") or "",
            "node_kind": candidate.get("node_kind", ""),
            "entity_type": candidate.get("entity_type", ""),
            "content": candidate.get("content_availability", ""),
        }
        for candidate in _mappings(result.get("candidates"))
    ]
    resolution = result.get("resolution") or "no_match"
    return {
        "candidates": rows,
        "summary": f"{resolution.replace('_', ' ')}: {len(rows)} candidate(s)"
        if isinstance(resolution, str)
        else "",
    }


def neighborhood(start: str, result: Mapping[str, object]) -> dict[str, Any]:
    """Nodes, relations and a Mermaid chart for a ``graph_subgraph_search`` result."""

    nodes = [
        {
            "node_key": node.get("node_key", ""),
            "title": node.get("title") or node.get("entity_type") or "",
            "node_kind": node.get("node_kind", ""),
            "entity_type": node.get("entity_type", ""),
            "document_id": node.get("document_id") or "",
        }
        for node in _mappings(result.get("nodes"))
    ]
    titles = {node["node_key"]: node["title"] or node["node_key"] for node in nodes}
    relations = [
        {
            "source": titles.get(
                str(relation.get("source_node_key")), relation.get("source_node_key")
            ),
            "relation": relation.get("relation_type", ""),
            "target": titles.get(
                str(relation.get("target_node_key")), relation.get("target_node_key")
            ),
            "origin": relation.get("origin", ""),
            "source_node_key": relation.get("source_node_key", ""),
            "target_node_key": relation.get("target_node_key", ""),
        }
        for relation in _mappings(result.get("relations"))
    ]
    completion = _mapping(result.get("completion"))
    partial = "" if completion.get("complete", True) else " (bounded; more exists)"
    return {
        "start": start,
        "nodes": nodes,
        "relations": relations,
        "chart": mermaid_chart(start, nodes, relations),
        "summary": f"{len(nodes)} node(s), {len(relations)} relation(s) around {titles.get(start, start)}"
        + partial,
    }


def mermaid_chart(
    start: str,
    nodes: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
) -> str:
    """A left-to-right Mermaid flowchart; every label is reduced to safe characters."""

    ids: dict[str, str] = {}
    lines = ["graph LR"]

    def node_id(key: str, title: str) -> str:
        if key not in ids:
            ids[key] = f"n{len(ids)}"
            lines.append(f'  {ids[key]}["{_chart_label(title or key)}"]')
        return ids[key]

    for node in nodes:
        node_id(str(node["node_key"]), str(node.get("title") or ""))
    for relation in relations:
        source = node_id(str(relation["source_node_key"]), str(relation.get("source") or ""))
        target = node_id(str(relation["target_node_key"]), str(relation.get("target") or ""))
        lines.append(f"  {source} -->|{_chart_label(str(relation['relation']))}| {target}")
    if start in ids:
        lines.append(f"  style {ids[start]} stroke-width:3px")
    return "\n".join(lines) if len(lines) > 1 else ""


def _chart_label(text: str) -> str:
    cleaned = " ".join(_UNSAFE_LABEL.sub(" ", text).split())
    return snippet(cleaned, _CHART_LABEL_CHARACTERS) or "node"


def _context_chunks(context: Mapping[str, object] | None, *, anchor: str) -> list[dict[str, Any]]:
    if context is None:
        return []
    return [
        {
            "chunk_id": chunk.get("chunk_id", ""),
            "ordinal": chunk.get("ordinal"),
            "section": section_label(chunk.get("section_path")),
            "text": chunk.get("text") if isinstance(chunk.get("text"), str) else "",
            "anchor": bool(anchor) and chunk.get("chunk_id") == anchor,
        }
        for chunk in _mappings(context.get("chunks"))
    ]


def _next_cursor(result: Mapping[str, object] | None) -> str:
    cursor = (result or {}).get("next_cursor")
    return cursor if isinstance(cursor, str) else ""


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mappings(value: object) -> list[Mapping[str, Any]]:
    return [item for item in _sequence(value) if isinstance(item, Mapping)]


def _sequence(value: object) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []
