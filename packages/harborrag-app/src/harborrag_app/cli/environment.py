"""Load the project's protected environment files into the CLI process.

The deployment already keeps connector credentials in ``env/.env.connector`` and model
credentials in ``env/.env.models``; compose hands those files to the worker and API
containers with ``env_file:``. Nothing did the equivalent for the CLI, which runs on the
host, so every command that resolves a connector or an embedding model had to be
prefixed with the same variables by hand even though the project already had them
configured.

Loading here keeps one source of truth. The real environment always wins, so an inline
prefix or an exported variable still overrides the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("harborrag.app.cli.environment")

# Mirrors the files scripts/deployment/dev.sh manages and compose mounts into the worker,
# and honours the same override variables so a non-default layout stays consistent
# between the CLI and the containers. env/.env.api and env/.env.database are deliberately
# absent: they point services at in-cluster hostnames and at the API's own control-plane
# DSN, which would silently repoint a host CLI at addresses it cannot reach.
_ENV_FILES: tuple[tuple[str, str], ...] = (
    ("CONNECTOR_ENV_FILE", "env/.env.connector"),
    ("PARSER_ENV_FILE", "env/.env.parser"),
    ("MODEL_ENV_FILE", "env/.env.models"),
)


def load_project_environment(root: Path | None = None) -> tuple[Path, ...]:
    """Load the connector, parser, and model env files; return the ones applied."""

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv ships with the CLI extras
        logger.debug("python-dotenv is unavailable; skipping project environment files")
        return ()
    base = root or Path.cwd()
    loaded: list[Path] = []
    for variable, default in _ENV_FILES:
        candidate = Path(os.environ.get(variable, default))
        path = candidate if candidate.is_absolute() else base / candidate
        if not path.is_file():
            continue
        # override=False: an operator who exports a variable, or prefixes one onto the
        # command, is being explicit and must outrank the file.
        load_dotenv(path, override=False)
        loaded.append(path)
    if loaded:
        logger.debug("Loaded project environment files: %s", ", ".join(str(p) for p in loaded))
    return tuple(loaded)


__all__ = ["load_project_environment"]
