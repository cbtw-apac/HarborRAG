"""Smoke check a real Confluence connection. See `run.py --connector confluence`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from bootstrap import (
    ConnectorConfigurationError,
    attachments_loaded,
    build_connector,
    build_harbor_parser,
    connector_definition,
    load_env,
    print_document,
    print_failure,
    render_metadata_section,
    save_output,
)
from bootstrap.confluence_markdown import confluence_html_to_markdown

from harborrag_adapters.connectors.schemas import ConnectorQuery

CONFLUENCE_METADATA_FIELDS: list[tuple[str, str]] = [
    ("Content ID", "content_id"),
    ("Content type", "content_type"),
    ("Space", "space_key"),
    ("Version", "version"),
    ("Author", "author"),
    ("Labels", "labels"),
    ("Created", "created_at"),
    ("Updated", "updated_at"),
    ("Breadcrumb", "breadcrumb"),
    ("Depth", "depth"),
]

_RENDER_DEPENDENT_STORAGE_MACRO = re.compile(
    r"\bac:name\s*=\s*['\"](?:include|excerpt-include|localtab|localtabgroup)['\"]",
    re.IGNORECASE,
)
_NON_CONTENT_RENDERED_BLOCK = re.compile(
    r"<(?P<tag>style|script)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_EMPTY_NON_CONTENT_RENDERED_BLOCK = re.compile(
    r"<(?:style|script)\b[^>]*/\s*>",
    re.IGNORECASE,
)


def _render_confluence_output(record, document, *, markdown: bool) -> str:
    """Render one loaded Confluence page for saving."""
    body, preview_representation = _confluence_body_preview(document)

    if not markdown:
        return body

    metadata = document.metadata or {}
    title = metadata.get("title") or record.id
    lines = [
        f"# {title}",
        "",
        "- **provider**: `confluence`",
        f"- **source**: `{record.id}`",
        f"- **content_type**: `{document.content_type}`",
    ]
    if preview_representation:
        lines.append(f"- **body_preview**: `{preview_representation}`")
    lines += [
        "",
        *render_metadata_section(metadata, CONFLUENCE_METADATA_FIELDS),
        body,
    ]
    return "\n".join(lines)


def _confluence_body_preview(document) -> tuple[str, str | None]:
    """Use server-rendered HTML only for storage macros that need that view.

    Normal pages intentionally return ``document.text()`` unchanged. Confluence
    already includes ``export_view`` in the loaded payload, so this diagnostic
    fallback does not make another request or change the ingestion document.
    """

    storage = document.text()
    if not _RENDER_DEPENDENT_STORAGE_MACRO.search(storage):
        return storage, None
    payload = document.raw
    if not isinstance(payload, Mapping):
        return storage, None
    body = payload.get("body")
    if not isinstance(body, Mapping):
        return storage, None
    export_view = body.get("export_view")
    if not isinstance(export_view, Mapping):
        return storage, None
    rendered = export_view.get("value")
    if not isinstance(rendered, str) or not rendered.strip():
        return storage, None
    sanitized = _NON_CONTENT_RENDERED_BLOCK.sub("", rendered)
    sanitized = _EMPTY_NON_CONTENT_RENDERED_BLOCK.sub("", sanitized).strip()
    markdown = confluence_html_to_markdown(sanitized)
    return (markdown, "export_view_markdown") if markdown else (storage, None)


def _run_attachment_pass(
    connection_id: str,
    records: list,
    *,
    output: str | None,
    output_dir: Path | None,
) -> int:
    """Load every record, then each of its attachments as its own document."""
    harbor_parser = build_harbor_parser()
    connector = build_connector(
        connection_id,
        include_attachments=True,
        expected_provider="confluence",
    )

    overall_ok = True
    for record in records:
        try:
            document = connector.load(record)
        except Exception as exc:  # noqa: BLE001
            print_failure("confluence", exc)
            overall_ok = False
            continue
        print_document("confluence", document)
        if output:
            text = _render_confluence_output(record, document, markdown=(output == "md"))
            save_output("confluence", record.id, text, output=output, output_dir=output_dir)
        if not attachments_loaded(
            "confluence",
            connector,
            record,
            parser=harbor_parser,
            save=lambda record_id, text: save_output(
                "confluence", record_id, text, output=output, output_dir=output_dir
            ),
        ):
            overall_ok = False

    return 0 if overall_ok else 1


def run_confluence(
    *,
    connection_id: str | None = None,
    limit: int = 10,
    output: str | None = None,
    output_dir: Path | None = None,
) -> int:
    load_env()
    identifier = connection_id or "confluence"
    try:
        definition = connector_definition(identifier, expected_provider="confluence")
        connector = build_connector(
            definition.name,
            include_attachments=False,
            expected_provider="confluence",
        )
    except ConnectorConfigurationError as exc:
        print(f"[confluence] not configured: {exc}")
        return 2

    try:
        records = list(connector.discover(ConnectorQuery(limit=limit)))
    except Exception as exc:  # noqa: BLE001 - smoke runner returns a stable exit code
        print_failure("confluence", exc)
        return 1
    print(f"\n[confluence] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[confluence] no records discovered")
        return 1

    print("\n[confluence] === load without attachments (first record) ===")
    try:
        document = connector.load(records[0])
    except Exception as exc:  # noqa: BLE001
        print_failure("confluence", exc)
        return 1
    print_document("confluence", document)

    if not definition.settings.get("include_attachments", False):
        print(
            "\n[confluence] include_attachments is false in config/sources/connectors.yaml; "
            "skipping the attachment pass"
        )
        return 0

    print(f"\n[confluence] === load with attachments ({len(records)} record(s)) ===")
    return _run_attachment_pass(
        definition.name,
        records,
        output=output,
        output_dir=output_dir,
    )


def main() -> int:
    return run_confluence()


if __name__ == "__main__":
    raise SystemExit(main())
