"""Non-interactive helpers behind ``harborrag init``: sample folder, ports, summary."""

from __future__ import annotations

import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel

from harborrag_app.scaffold import (
    COMPOSE_FALLBACK,
    COMPOSE_FILE,
    ENV_FALLBACK,
    ENV_FILE,
    SERVICE_PORTS,
    SOURCES,
    InitOptions,
)

# Offsets tried, in order, when the default host ports are already taken.
_PORT_OFFSETS = (0, 10000, 20000, 30000)
_MAX_PORTS_OFFSET = 50000  # the --ports-offset bound
# The Qdrant HTTP port anchors the offset: base 6333 in both .env and the compose file.
_QDRANT_HTTP_BASE = next(base for name, base, _s in SERVICE_PORTS if name == "qdrant_http_port")
_ENV_QDRANT_URL = re.compile(r"^\s*HARBORRAG_QDRANT_URL\s*=\s*(\S+)\s*$", re.MULTILINE)
_COMPOSE_QDRANT_PORT = re.compile(rf"127\.0\.0\.1:(\d+):{_QDRANT_HTTP_BASE}\b")


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


def recorded_ports_offset(root: Path) -> int | None:
    """The port offset an earlier ``init`` left in ``root``'s .env, else its compose file.

    Read from the Qdrant HTTP port, the one both files always publish. ``None`` when
    neither file exists or the port does not decode to a valid offset (a hand edit).
    """

    env, compose = root / ENV_FILE, root / COMPOSE_FILE
    port: int | None = None
    if env.is_file():
        match = _ENV_QDRANT_URL.search(env.read_text(encoding="utf-8", errors="replace"))
        try:
            port = urlparse(match.group(1)).port if match else None
        except ValueError:
            port = None
    if port is None and compose.is_file():
        match = _COMPOSE_QDRANT_PORT.search(compose.read_text(encoding="utf-8", errors="replace"))
        port = int(match.group(1)) if match else None
    if port is None or not 0 <= port - _QDRANT_HTTP_BASE <= _MAX_PORTS_OFFSET:
        return None
    return port - _QDRANT_HTTP_BASE


def select_ports_offset(explicit: int | None, *, recorded: int | None = None) -> tuple[int, str]:
    """Pick the port offset for docker-compose.yml and .env, and a notice to print.

    An explicit ``--ports-offset`` wins. A re-run then keeps the offset ``recorded`` in
    the existing project without probing: the project's own running stack holds those
    ports, and moving away from them would leave the kept .env pointing at the old ones.
    Otherwise the default ports are used when free; if another stack holds any of them,
    the first offset whose ports are all free is used so ``docker compose up`` does not
    fail late with a bind error.
    """

    if explicit is not None:
        notice = _ports_notice(explicit, forced=True) if explicit else ""
        if recorded is not None and recorded != explicit:
            notice = (notice + "\n" if notice else "") + (
                f"[yellow]![/] The existing {ENV_FILE} uses port offset {recorded} and is kept;"
                f" copy only the port lines from {ENV_FALLBACK} into it."
            )
        return explicit, notice
    if recorded is not None:
        return recorded, (
            f"[yellow]![/] Keeping port offset {recorded} from the existing project."
            if recorded
            else ""
        )
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
        # `--source` may name an absolute or `../` folder, which is not under root. The
        # scaffold is already written by now, so this must not raise over a display path.
        try:
            shown = path.relative_to(root)
        except ValueError:
            shown = path
        console.print(f"  [dim]{shown}[/]")
    steps: list[str] = []
    names = {path.name for path in written}
    if ENV_FALLBACK in names:
        # A fresh .env carries a new encryption key and MinIO secret; copying those over
        # would make the secrets already stored with the old key unreadable.
        steps.append(
            f"Merge {ENV_FALLBACK} into your existing .env (it was left untouched); keep"
            " your HARBORRAG_SECRETS_ENCRYPTION_KEY and object-store secret"
        )
    if COMPOSE_FALLBACK in names:
        steps.append(
            f"Merge {COMPOSE_FALLBACK} into your existing {COMPOSE_FILE} (it was left untouched)"
        )
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


__all__ = ["recorded_ports_offset", "report", "seed_source_folder", "select_ports_offset"]
