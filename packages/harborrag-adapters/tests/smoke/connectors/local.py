"""Smoke check a real local-files connection. See `run.py --connector local`."""

from __future__ import annotations

from pathlib import Path

from bootstrap import (
    ConnectorConfigurationError,
    build_connector,
    build_harbor_parser,
    load_env,
    print_failure,
    print_parsed,
    save_output,
)

from harborrag_adapters.connectors.local.mappers import path_from_record
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.parsers import ParseInput

_IMAGE_SUFFIXES = frozenset({"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp", "svg"})


def _render_local_output(path: Path, parsed, *, markdown: bool) -> str:
    """Render one locally parsed document for saving."""
    if not markdown:
        return parsed.content
    lines = [
        f"# {path.name}",
        "",
        f"- **source**: `{path}`",
        f"- **parser**: `{parsed.parser_name}`",
        "",
    ]
    if path.suffix.lower().lstrip(".") in _IMAGE_SUFFIXES:
        lines += [f"![{path.name}]({path.as_uri()})", ""]
    lines.append(parsed.content)
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
            text = _render_local_output(path, parsed, markdown=(output == "md"))
            save_output("local", path.name, text, output=output, output_dir=output_dir)

    return 0 if overall_ok else 1


def main() -> int:
    return run_local()


if __name__ == "__main__":
    raise SystemExit(main())
