from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.confluence.normalization import ConfluencePageInput


def text(value: str, **attrs: object) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if attrs:
        node["attrs"] = attrs
    return node


def paragraph(value: str, *, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    content = {"type": "text", "text": value}
    if marks:
        content["marks"] = marks
    return {"type": "paragraph", "content": [content]}


def heading(level: int, value: str) -> dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [text(value)],
    }


def table(
    headers: list[str],
    rows: list[list[str]],
    *,
    local_id: str = "table-1",
) -> dict[str, Any]:
    header = {
        "type": "tableRow",
        "content": [{"type": "tableHeader", "content": [paragraph(value)]} for value in headers],
    }
    body = [
        {
            "type": "tableRow",
            "content": [{"type": "tableCell", "content": [paragraph(value)]} for value in row],
        }
        for row in rows
    ]
    return {
        "type": "table",
        "attrs": {"localId": local_id},
        "content": [header, *body],
    }


def tabs_page(title: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "tabsPage", "attrs": {"title": title}, "content": content}


def tabs_container(*pages: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tabsContainer", "attrs": {}, "content": list(pages)}


def page_input(
    content: list[dict[str, Any]] | None = None,
    **changes: object,
) -> ConfluencePageInput:
    values: dict[str, object] = {
        "page_id": "42",
        "page_version": "7",
        "space_id": "space-1",
        "space_key": "ENG",
        "title": "Deployment Guide",
        "source_url": "https://example.atlassian.net/wiki/spaces/ENG/pages/42",
        "ancestors": (("1", "Engineering"),),
        "labels": ("runbook",),
        "adf": {"type": "doc", "version": 1, "content": content or []},
    }
    values.update(changes)
    return ConfluencePageInput(**values)  # type: ignore[arg-type]


def walk(block):
    yield block
    for child in block.children:
        yield from walk(child)
