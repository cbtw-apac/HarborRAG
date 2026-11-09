"""Smoke check a real JIRA connection. See `run.py --connector jira`."""

from __future__ import annotations

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


def _render_jira_output(record, document, *, markdown: bool) -> str:
    """Render one loaded JIRA issue for saving."""
    body = document.text()
    if not markdown:
        return body
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
    """Load every record, then each of its attachments as its own document."""
    harbor_parser = build_harbor_parser()
    connector = build_connector(
        connection_id,
        include_attachments=True,
        expected_provider="jira",
    )

    overall_ok = True
    for record in records:
        try:
            document = connector.load(record)
        except Exception as exc:  # noqa: BLE001
            print_failure("jira", exc)
            overall_ok = False
            continue
        print_document("jira", document)
        if output:
            text = _render_jira_output(record, document, markdown=(output == "md"))
            save_output("jira", record.id, text, output=output, output_dir=output_dir)
        if not attachments_loaded(
            "jira",
            connector,
            record,
            parser=harbor_parser,
            save=lambda record_id, text: save_output(
                "jira", record_id, text, output=output, output_dir=output_dir
            ),
        ):
            overall_ok = False

    return 0 if overall_ok else 1


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

    try:
        records = list(connector.discover(ConnectorQuery(limit=limit)))
    except Exception as exc:  # noqa: BLE001 - smoke runner returns a stable exit code
        print_failure("jira", exc)
        return 1
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
            "\n[jira] include_attachments is false in config/sources/connectors.yaml; "
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
