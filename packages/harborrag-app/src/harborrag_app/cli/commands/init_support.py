"""Non-interactive helpers behind ``harborrag init``: sample folder, ports, summary."""

from __future__ import annotations

import socket
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from harborrag_app.scaffold import ENV_FALLBACK, SERVICE_PORTS, SOURCES, InitOptions

# Offsets tried, in order, when the default host ports are already taken.
_PORT_OFFSETS = (0, 10000, 20000, 30000)


_SAMPLE_DOCUMENT = """# Your HarborRAG workspace

Files in this folder are what `harborrag ingest run workspace` indexes: Markdown, text,
PDF, Word, PowerPoint, CSV and Excel documents (see `allowed_extensions` in
`config/connectors.yaml`). Replace this file with your own content, or point
`LOCAL_SOURCE_PATH` in `.env` at another folder.

HarborRAG keeps one authoritative version per document, so re-running the ingestion after
editing a file replaces the old chunks instead of duplicating them.
"""


def seed_source_folder(root: Path, source_path: str) -> Path | None:
    """Create a missing source folder with one sample document; never touch an existing one."""

    folder = Path(source_path)
    if not folder.is_absolute():
        folder = root / folder
    if folder.exists():
        return None
    folder.mkdir(parents=True)
    sample = folder / "README.md"
    sample.write_text(_SAMPLE_DOCUMENT, encoding="utf-8")
    return sample


def select_ports_offset(explicit: int | None) -> tuple[int, str]:
    """Pick the port offset for docker-compose.yml and .env, and a notice to print.

    An explicit ``--ports-offset`` wins. Otherwise the default ports are used when free;
    if another stack holds any of them, the first offset whose ports are all free is used
    so ``docker compose up`` does not fail late with a bind error.
    """

    if explicit is not None:
        return explicit, _ports_notice(explicit, forced=True) if explicit else ""
    busy_by_offset: dict[int, list[tuple[int, str]]] = {}
    for offset in _PORT_OFFSETS:
        candidates = tuple((base + offset, service) for _name, base, service in SERVICE_PORTS)
        busy = _busy_ports(candidates)
        if not busy:
            return offset, _ports_notice(offset, forced=False) if offset else ""
        busy_by_offset[offset] = busy
    taken = ", ".join(f"{port} ({service})" for port, service in busy_by_offset[0])
    return 0, (
        f"[yellow]![/] ports already in use: {taken}; no free offset among "
        f"{', '.join(str(o) for o in _PORT_OFFSETS[1:])}. Edit docker-compose.yml and .env."
    )


def _ports_notice(offset: int, *, forced: bool) -> str:
    mapping = ", ".join(f"{service} {base + offset}" for _name, base, service in SERVICE_PORTS)
    reason = "as requested" if forced else "because the default ports are already in use"
    return f"[yellow]![/] Using port offset {offset} {reason}: {mapping}."


def _busy_ports(ports: tuple[tuple[int, str], ...]) -> list[tuple[int, str]]:
    busy: list[tuple[int, str]] = []
    for port, service in ports:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                busy.append((port, service))
    return busy


def report(
    console: Console,
    root: Path,
    written: tuple[Path, ...],
    options: InitOptions,
    *,
    sample: Path | None,
) -> None:
    console.print(f"[bold green]✓[/] Created HarborRAG project in [cyan]{root}[/]")
    for path in (*written, *([sample] if sample else [])):
        console.print(f"  [dim]{path.relative_to(root)}[/]")
    steps: list[str] = []
    if any(path.name == ENV_FALLBACK for path in written):
        steps.append(f"Merge {ENV_FALLBACK} into your existing .env (it was left untouched)")
    blank = [options.preset.credential_variable] if not options.api_key else []
    blank += [
        variable.name
        for key in options.sources
        for variable in SOURCES[key].variables
        if not variable.yaml_only and not options.source_values.get(variable.name)
    ]
    if blank:
        steps.append(f"Edit .env and set {', '.join(blank)}")
    if options.has_local_source:
        source = options.source_path.removeprefix("./").rstrip("/")
        steps.append(
            f"Put the files to index in {source}/ (a sample README.md is there to start with)"
            if sample
            else f"Files to index are read from {source}/"
        )
    steps += ["docker compose up -d", "harborrag doctor"]
    steps += [f"harborrag ingest run {name}" for name in options.connector_names]
    steps += ['harborrag retrieve "your question"', 'harborrag chat "your question"']
    body = "\n".join(f"[bold]{index}.[/] {step}" for index, step in enumerate(steps, 1))
    prefix = "" if root == Path.cwd().resolve() else f"[dim]cd {root}[/]\n"
    console.print(Panel(prefix + body, title="Next steps", border_style="cyan"))


__all__ = ["report", "seed_source_folder", "select_ports_offset"]
