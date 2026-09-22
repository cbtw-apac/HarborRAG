"""FakeMcpQueryLogRepository/FakeMcpConfigSnapshotRepository: split out of
control_plane_fakes.py to keep that file under the repo's file-length gate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from harborrag_core.domain.mcp_usage import (
    McpClientUsage,
    McpConfigSnapshot,
    McpToolUsage,
    McpUsageEntry,
)


@dataclass(slots=True)
class FakeMcpQueryLogRepository:
    """List-backed McpQueryLogRepositoryPort."""

    entries: list[McpUsageEntry] = field(default_factory=list)

    async def record(self, entry: McpUsageEntry) -> None:
        self.entries.append(entry)

    async def usage_by_client(self) -> list[McpClientUsage]:
        counts: Counter[str] = Counter(entry.client for entry in self.entries)
        last_seen: dict[str, datetime] = {}
        for entry in self.entries:
            current = last_seen.get(entry.client)
            if current is None or entry.created_at > current:
                last_seen[entry.client] = entry.created_at
        return sorted(
            (
                McpClientUsage(client=client, query_count=count, last_seen_at=last_seen[client])
                for client, count in counts.items()
            ),
            key=lambda usage: usage.last_seen_at,
            reverse=True,
        )

    async def usage_by_tool(self) -> list[McpToolUsage]:
        latencies: dict[str, list[int]] = defaultdict(list)
        for entry in self.entries:
            latencies[entry.tool].append(entry.latency_ms)
        return sorted(
            (
                McpToolUsage(
                    tool=tool,
                    call_count=len(values),
                    avg_latency_ms=sum(values) / len(values),
                )
                for tool, values in latencies.items()
            ),
            key=lambda usage: usage.call_count,
            reverse=True,
        )

    async def list_since(self, *, since: datetime, limit: int) -> list[McpUsageEntry]:
        matching = [entry for entry in self.entries if entry.created_at >= since]
        ordered = sorted(matching, key=lambda entry: entry.created_at, reverse=True)
        return ordered[:limit]

    async def ping(self) -> bool:
        return True


@dataclass(slots=True)
class FakeMcpConfigSnapshotRepository:
    """Single-slot McpConfigSnapshotPort."""

    snapshot: McpConfigSnapshot | None = None

    async def get(self) -> McpConfigSnapshot | None:
        return self.snapshot

    async def put(self, snapshot: McpConfigSnapshot) -> McpConfigSnapshot:
        self.snapshot = snapshot
        return snapshot
