"""Smoke check a real JIRA connection. See `run.py --connector jira`."""

from __future__ import annotations

from pathlib import Path

from bootstrap import (
    ConnectorConfigurationError,
    attachments_passed,
    build_connector,
    build_harbor_parser,
    connector_definition,
    load_env,
    output_path_for,
    print_document,
    print_failure,
    render_metadata_section,
    save_attachment_asset,
    save_output,
)

from harborrag_adapters.connectors.schemas import ConnectorQuery

JIRA_METADATA_FIELDS: list[tuple[str, str]] = [
    ("Issue key", "issue_key"),
    ("Project", "project_name"),
    ("Project key", "project_key"),
    ("Issue type", "issue_type"),
    ("Status", "status"),
    ("Status category", "status_category"),
    ("Priority", "priority"),
    ("Assignee", "assignee"),
    ("Reporter", "reporter"),
    ("Creator", "creator"),
    ("Labels", "labels"),
    ("Components", "components"),
    ("Fix versions", "fix_versions"),
    ("Affected versions", "affected_versions"),
    ("Created", "created_at"),
    ("Updated", "updated_at"),
    ("Resolved", "resolved_at"),
    ("Due date", "due_date"),
]


def _save_image_attachments(
    connector, output_path: Path, attachments: list[dict]
) -> dict[str, str]:
    """Download each image attachment next to `output_path` so Markdown can embed it.

    OCR text alone can't be visually inspected; without the original bytes on
    disk, an `![]()` link in the saved `.md` has nothing to point at.
    """
    asset_paths: dict[str, str] = {}
    for attachment in attachments:
        media_type = attachment.get("media_type") or ""
        download_url = attachment.get("download_url")
        if not media_type.startswith("image/") or not download_url:
            continue
        try:
            content = connector.provider.client.download_bytes(download_url)
        except Exception as exc:  # noqa: BLE001 - best-effort asset download
            print(f"[assets] failed to download {attachment.get('title')!r}: {exc}")
            continue
        if not content:
            continue
        asset_path = save_attachment_asset(
            output_path,
            attachment.get("title") or attachment.get("id") or "attachment",
            content,
        )
        asset_paths[attachment.get("id", "")] = f"{output_path.stem}.assets/{asset_path.name}"
    return asset_paths


def _render_jira_output(
    record,
    document,
    *,
    markdown: bool,
    asset_paths: dict[str, str] | None = None,
) -> str:
    """Render one loaded JIRA issue (plus any parsed attachments) for saving."""
    attachments = (document.metadata or {}).get("attachments") or []
    body = document.text()

    if not markdown:
        text = body
        for attachment in attachments:
            attachment_text = attachment.get("text")
            if attachment_text:
                text += f"\n\n--- attachment: {attachment.get('title')} ---\n{attachment_text}"
        return text

    metadata = document.metadata or {}
    title = metadata.get("title") or record.id
    lines = [
        f"# {title}",
        "",
        "- **provider**: `jira`",
        f"- **source**: `{record.id}`",
        f"- **content_type**: `{document.content_type}`",
        "",
        *render_metadata_section(metadata, JIRA_METADATA_FIELDS),
        body,
    ]
    return "\n".join(lines)


def _run_attachment_pass(
    connection_id: str,
    records: list,
    *,
    output: str | None,
    output_dir: Path | None,
) -> int:
    """Reload every record with attachment processing enabled and report status."""
    harbor_parser = build_harbor_parser()
    connector_with_attachments = build_connector(
        connection_id,
        include_attachments=True,
        parser=harbor_parser,
        expected_provider="jira",
    )

    overall_ok = True
    for record in records:
        try:
            document_with_attachments = connector_with_attachments.load(record)
        except Exception as exc:  # noqa: BLE001
            print_failure("jira", exc)
            overall_ok = False
            continue
        print_document("jira", document_with_attachments)
        if not attachments_passed("jira", document_with_attachments):
            overall_ok = False

        if output:
            _save_attachment_output(
                connector_with_attachments,
                record,
                document_with_attachments,
                output=output,
                output_dir=output_dir,
            )

    return 0 if overall_ok else 1


def _save_attachment_output(
    connector_with_attachments,
    record,
    document_with_attachments,
    *,
    output: str,
    output_dir: Path | None,
) -> None:
    """Save one loaded-with-attachments record to disk in the requested format."""
    asset_paths = None
    if output == "md":
        attachments = (document_with_attachments.metadata or {}).get("attachments") or []
        output_path = output_path_for("jira", record.id, output, output_dir)
        asset_paths = _save_image_attachments(connector_with_attachments, output_path, attachments)
    text = _render_jira_output(
        record,
        document_with_attachments,
        markdown=(output == "md"),
        asset_paths=asset_paths,
    )
    save_output("jira", record.id, text, output=output, output_dir=output_dir)


def run_jira(
    *,
    connection_id: str | None = None,
    limit: int = 3,
    output: str | None = None,
    output_dir: Path | None = None,
) -> int:
    load_env()
    identifier = connection_id or "jira"
    try:
        definition = connector_definition(identifier, expected_provider="jira")
        connector = build_connector(
            definition.name,
            include_attachments=False,
            expected_provider="jira",
        )
    except ConnectorConfigurationError as exc:
        print(f"[jira] not configured: {exc}")
        return 2

    records = list(connector.discover(ConnectorQuery(limit=limit)))
    print(f"\n[jira] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[jira] no records discovered")
        return 1

    print("\n[jira] === load without attachments (first record) ===")
    try:
        document = connector.load(records[0])
    except Exception as exc:  # noqa: BLE001 - smoke runner returns a stable exit code
        print_failure("jira", exc)
        return 1
    print_document("jira", document)

    if not definition.settings.get("include_attachments", False):
        print(
            "\n[jira] include_attachments is false in config/connectors.yaml; "
            "skipping the attachment pass"
        )
        return 0

    print(f"\n[jira] === load with attachments ({len(records)} record(s)) ===")
    return _run_attachment_pass(
        definition.name,
        records,
        output=output,
        output_dir=output_dir,
    )


def main() -> int:
    return run_jira()


if __name__ == "__main__":
    raise SystemExit(main())
