"""Smoke check a real local-files connection. See `run.py --connector local`."""

from __future__ import annotations

from pathlib import Path

from bootstrap import (
    ConnectorConfigurationError,
    build_connector,
    build_harbor_parser,
    load_env,
    output_path_for,
    print_failure,
    print_parsed,
    render_metadata_section,
    save_output,
)

from harborrag_adapters.connectors.local.mappers import path_from_record
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.parsers import ParseInput

_IMAGE_SUFFIXES = frozenset({"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp", "svg"})

LOCAL_METADATA_FIELDS: list[tuple[str, str]] = [
    ("Parser", "parser_name"),
    ("Parser version", "parser_version"),
    ("Source engine", "source_engine"),
    ("Page count", "page_count"),
    ("OCR engine", "docling_ocr_engine"),
    ("OCR enabled", "docling_do_ocr"),
    ("Table structure", "docling_do_table_structure"),
    ("Figures extracted", "figure_count"),
    ("Warnings", "warning_count"),
]


def _local_metadata(parsed, figure_count: int) -> dict:
    """Flatten `ParsedDocument` fields that live outside `.metadata` into it."""
    metadata = dict(parsed.metadata or {})
    metadata.setdefault("parser_name", parsed.parser_name)
    metadata.setdefault("parser_version", parsed.parser_version)
    metadata["figure_count"] = figure_count
    metadata["warning_count"] = len(parsed.warnings or [])
    return metadata


def _save_local_figures(parsed, output_path: Path) -> list[Path]:
    """Copy Docling's extracted figure crops next to `output_path` so Markdown can embed them.

    Docling (when `pdf-docling.image_output_dir` is configured) also renders a
    full-page image and table crops into the same `docling_image_paths` list;
    only `picture-*` entries are figures actually embedded in the document, so
    page/table renders are intentionally skipped here.
    """
    image_paths = (parsed.metadata or {}).get("docling_image_paths") or []
    figures = sorted(
        (Path(p) for p in image_paths if Path(p).name.startswith("picture-")),
        key=lambda p: p.name,
    )
    if not figures:
        return []
    assets_dir = output_path.parent / f"{output_path.stem}.assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for figure in figures:
        target = assets_dir / figure.name
        target.write_bytes(figure.read_bytes())
        saved.append(target)
    return saved


def _render_local_output(
    path: Path,
    parsed,
    *,
    markdown: bool,
    figures: list[Path] | None = None,
    output_path: Path | None = None,
) -> str:
    """Render one locally parsed document for saving."""
    if not markdown:
        return parsed.content
    figures = figures or []
    lines = [
        f"# {path.name}",
        "",
        f"- **source**: `{path}`",
        f"- **parser**: `{parsed.parser_name}`",
        "",
        *render_metadata_section(_local_metadata(parsed, len(figures)), LOCAL_METADATA_FIELDS),
    ]
    if path.suffix.lower().lstrip(".") in _IMAGE_SUFFIXES:
        lines += [f"![{path.name}]({path.as_uri()})", ""]
    lines.append(parsed.content)
    if figures and output_path is not None:
        lines += ["", "## Figures", ""]
        for figure in figures:
            asset_rel = f"{output_path.stem}.assets/{figure.name}"
            lines += [f"### {figure.name}", "", f"![{figure.name}]({asset_rel})", ""]
    return "\n".join(lines)


def run_local(*, limit: int = 5, output: str | None = None, output_dir: Path | None = None) -> int:
    load_env()
    try:
        connector = build_connector("local", include_attachments=False)
    except ConnectorConfigurationError as exc:
        print(f"[local] not configured: {exc}")
        return 2

    records = list(connector.discover(ConnectorQuery(limit=limit)))
    print(f"\n[local] discovered {len(records)} record(s)")
    for record in records:
        print(f"  - {record.id} ({record.source_type})")
    if not records:
        print("[local] no records discovered")
        return 1

    harbor_parser = build_harbor_parser()
    overall_ok = True
    for record in records:
        path = path_from_record(record)
        try:
            parsed = harbor_parser.parse(ParseInput(path=path))
        except Exception as exc:  # noqa: BLE001
            print_failure("local", exc)
            overall_ok = False
            continue
        if not parsed.content.strip():
            print(f"[local] failed: parser returned empty extracted content for {path}")
            overall_ok = False
            continue

        print_parsed("local", parsed, source=str(path))
        if output:
            figures = None
            output_path = None
            if output == "md":
                output_path = output_path_for("local", path.name, output, output_dir)
                figures = _save_local_figures(parsed, output_path)
            text = _render_local_output(
                path,
                parsed,
                markdown=(output == "md"),
                figures=figures,
                output_path=output_path,
            )
            save_output("local", path.name, text, output=output, output_dir=output_dir)

    return 0 if overall_ok else 1


def main() -> int:
    return run_local()


if __name__ == "__main__":
    raise SystemExit(main())
