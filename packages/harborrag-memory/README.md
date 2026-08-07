# harborrag-memory

Scope-aware memory shared by chat and agent orchestration. The package owns the
first-class memory facade, while storage stays in `harborrag-adapters` and core
contracts stay in `harborrag-core`.

## Folder ownership

- `short_term/` — conversation history facade.
- `working/` — per-run scratch state facade.
- `long_term/` — canonical repository facade for durable memory.
- `manager.py` — `MemoryManager`, the single facade chat/agent import.
- `tiers/` — compatibility shims for the older layout.
