# harborrag-memory

Scope-aware memory shared by chat and agent orchestration. The package owns the
first-class memory facade, while storage stays in `harborrag-adapters` and core
contracts stay in `harborrag-core`.

## Module ownership

- `tiers/short_term.py` — conversation history facade.
- `tiers/working.py` — per-run scratch state facade and local store.
- `tiers/long_term.py` — canonical repository facade for durable memory.
- `manager.py` — `MemoryManager`, the single facade chat/agent import.
- `schemas.py` — stable re-exports of the core-owned memory contracts.

All public operations require a caller-derived `MemoryOwner`. Applications must
build that owner from authenticated request context; user-supplied owner fields
must never be passed through directly.
