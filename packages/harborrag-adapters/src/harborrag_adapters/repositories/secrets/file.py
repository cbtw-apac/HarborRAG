"""Dev-only SecretsPort: values live in a local JSON file, never the control-plane DB.

Read-modify-write under a process-local lock, written via a temp file + os.replace
so a crash mid-write can't corrupt the store. Single-process only (matches dev
composition) -- a multi-process deployment needs the Vault/KMS backend instead.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


@dataclass(slots=True)
class FileSecretsRepository:
    """SecretsPort over a local JSON file mapping ref -> raw value."""

    path: Path
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def put(self, value: str) -> str:
        """Store the value under a fresh opaque ref."""
        ref = f"secret://file/{uuid4().hex}"
        async with self._lock:
            values = self._read()
            values[ref] = value
            self._write(values)
        return ref

    async def resolve(self, ref: str) -> str:
        """Return the stored value; KeyError for unknown/deleted refs."""
        async with self._lock:
            return self._read()[ref]

    async def delete(self, ref: str) -> None:
        """Forget the value behind the ref; a no-op if already gone."""
        async with self._lock:
            values = self._read()
            if values.pop(ref, None) is not None:
                self._write(values)

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        values: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8"))
        return values

    def _write(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(values), encoding="utf-8")
        os.replace(tmp_path, self.path)
