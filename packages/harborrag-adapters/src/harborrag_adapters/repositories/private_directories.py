"""Owner-only creation of the directories local stores write beneath.

A directory this creates is mode 0700 whatever the umask; one that already exists
keeps its mode.
"""

from __future__ import annotations

import os
from pathlib import Path


def make_private_directories(directory: Path) -> None:
    """Create ``directory`` and each missing directory above it with mode 0700.

    ``Path.mkdir(parents=True)`` gives the parents the umask default instead, so a
    first write could leave a new ``.harborrag`` readable by other users. A
    directory another process creates first is left as it is.

    Args:
        directory: Directory that must exist afterwards.

    Raises:
        FileExistsError: A path on the way is a file, not a directory.
    """
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not path.is_dir():
                raise
            continue
        os.chmod(path, 0o700)
