"""Writing parsed smoke output and attachment assets to disk."""

from __future__ import annotations

import re
from pathlib import Path

from .paths import DEFAULT_OUTPUT_DIR

SUPPORTED_OUTPUT_FORMATS = ("txt", "md")


def sanitize_output_id(record_id: str) -> str:
    """Turn a record id/filename into a safe path segment shared by every saver."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", record_id).strip("_") or "document"


def output_path_for(
    provider: str,
    record_id: str,
    output: str,
    output_dir: Path | None = None,
) -> Path:
    """Compute where `save_output` will write, without writing anything.

    Callers that need to save attachment assets alongside the output file
    (so a saved `.md` can embed working image links) must know this path
    before the text to embed those links even exists.
    """
    target_dir = output_dir or DEFAULT_OUTPUT_DIR
    safe_id = sanitize_output_id(record_id)
    return target_dir / f"{provider}-{safe_id}.{output}"


def save_output(
    provider: str,
    record_id: str,
    text: str,
    *,
    output: str | None,
    output_dir: Path | None = None,
) -> Path | None:
    """Write parsed output to a file when `--output` was requested."""
    if not output:
        return None
    if output not in SUPPORTED_OUTPUT_FORMATS:
        choices = ", ".join(SUPPORTED_OUTPUT_FORMATS)
        raise ValueError(f"Unsupported output format {output!r}; choose: {choices}")

    target_path = output_path_for(provider, record_id, output, output_dir)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(text, encoding="utf-8")
    print(f"[{provider}] saved parsed output to {target_path}")
    return target_path


def save_attachment_asset(
    output_path: Path,
    title: str,
    content: bytes,
) -> Path:
    """Save one downloaded attachment next to its saved output file.

    Assets live in a `<output-file-stem>.assets/` sibling directory so an `.md`
    file can embed `![title](stem.assets/title)` and actually resolve.
    """
    assets_dir = output_path.parent / f"{output_path.stem}.assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_output_id(title) or "attachment"
    asset_path = assets_dir / safe_name
    asset_path.write_bytes(content)
    return asset_path
