# harborrag-memory

Scope-aware memory shared by chat and agent orchestration in
[HarborRAG](https://github.com/cbtw-apac/HarborRAG). This package owns the memory facade;
storage lives in `harborrag-adapters` and the contracts live in `harborrag-core`.

Memory policy is written against LangChain primitives (`langchain-core` messages and
`trim_messages`, LangGraph's `BaseStore`). It stays provider-neutral in the sense that
matters: no LLM-provider SDK is imported here, and the chat model is injected as a
`BaseChatModel` -- HarborRAG's LiteLLM-backed implementation of that interface lives in
`harborrag-adapters`.

It is a required dependency of `harborrag-runtime`, so any HarborRAG install already has it.

```bash
pip install harborrag-memory
```

## Three tiers, one facade

| Tier | Holds | Lifetime |
| --- | --- | --- |
| Short-term | conversation turns | the session |
| Working | per-run scratch state | the run, bounded by a TTL |
| Long-term | durable memories in a repository | until deleted |

`MemoryManager` is the single facade chat and agent code imports. Each tier is optional -
pass only the ones you configured, and calling into an unconfigured tier raises
`MemoryConfigurationError`.

## Everything is scoped by `MemoryOwner`

Every public operation takes a `MemoryOwner`: the isolation key a memory is written under,
or the identity a query is issued as.

```python
from harborrag_memory import MemoryOwner

owner = MemoryOwner(
    tenant_id="tenant-1",
    project_id="handbook",
    principal_id="user-1",
    session_id="session-1",
    run_id="run-1",
)
```

How much of that owner is load-bearing depends on the memory's `MemoryScope`:

| Scope | Owner fields that must match |
| --- | --- |
| `GLOBAL` | none |
| `TENANT` | `tenant_id` |
| `PROJECT` | `tenant_id`, `project_id` |
| `USER` | `tenant_id`, `principal_id` |
| `SESSION` | `tenant_id`, `principal_id`, `session_id` |
| `RUN` | every field through `run_id` |

> **Security rule.** Build the owner from authenticated request context. Never pass
> user-supplied owner fields through - doing so lets a caller read another tenant's,
> project's, or user's memory. `snapshot()` enforces this by rejecting a query whose owner
> does not match the authenticated caller.

## Worked example

```python
import asyncio

from harborrag_memory import (
    InMemoryWorkingMemoryStore,
    MemoryManager,
    MemoryOwner,
    WorkingMemory,
)


async def main() -> None:
    owner = MemoryOwner(
        tenant_id="tenant-1",
        principal_id="user-1",
        session_id="session-1",
        run_id="run-1",
    )
    memory = MemoryManager(working=WorkingMemory(InMemoryWorkingMemoryStore()))

    await memory.update(owner, {"step": "retrieval", "candidates": 12})
    print(await memory.scratch(owner))          # {'step': 'retrieval', 'candidates': 12}

    snapshot = await memory.snapshot(owner)     # recent turns + working state + memories
    print(snapshot.working_state)


asyncio.run(main())
```

## Facade surface

| Tier | Methods |
| --- | --- |
| Short-term | `recent(owner, limit=...)`, `append(owner, turn)`, `clear(owner)` |
| Working | `scratch(owner)`, `update(owner, state, ttl_seconds=...)`, `clear_working(owner)` |
| Long-term | `save(caller, memory)`, `get(caller, memory_id)`, `search(caller, query)`, `delete(caller, memory_id)` |
| Combined | `snapshot(owner, query=..., recent_limit=...)` |

## Per-turn context

`MemoryContextBuilder` assembles everything one chat or agent turn needs from memory into
one immutable `MemoryContext`: the verbatim window to replay, the rolling session summary
covering the turns the window dropped, and the standalone query to send to retrieval.

```python
from harborrag_memory import MemoryContextBuilder, MemoryPolicy

builder = MemoryContextBuilder(
    policy=MemoryPolicy(),
    messages=message_store,   # core ConversationMessageStore
    memories=memory_repo,     # core MemoryRepository, optional
    model=chat_model,         # langchain_core BaseChatModel, optional
    index=memory_index,       # core MemoryIndex, optional
    embedder=embed_text,      # core MemoryEmbedder, optional
)
context = await builder.build(owner, "and who owns it?")
context.messages          # replay window, oldest-first
context.summary           # rolling session summary, or None
context.recalled          # long-term memories, already ranked and budgeted
context.standalone_query  # what retrieval should search for
context.wanted_types      # memory types the rewrite said the question wants
```

> **Contract.** The question being answered must not be persisted yet when `build` is
> called. The window it returns is the history *before* this turn; append the user message
> afterwards. A caller that appends first sees its own question replayed inside the window.
>
> `MemoryScope.SESSION` keys on `user_id`, so the builder derives `user_id` from
> `principal_id` when the owner omits it -- for saves and for queries alike. Supply
> `user_id` consistently, or consistently omit it: alternating between the two for the same
> human produces two visibility keys and loses the session's summary.

`build` requires an owner with both `principal_id` and `session_id` (`MemoryScopeError`
otherwise) and never fails the turn for a model or repository error: every such failure is
logged at WARNING and the turn continues with the trimmed history and the original
question.

### `MemoryPolicy`

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `True` | when false, return a plain window and call no model |
| `recent_max_messages` | `12` | how many messages are loaded from history |
| `recent_max_tokens` | `2000` | token budget for the verbatim window |
| `summary_trigger_fraction` | `0.7` | fraction of that budget that triggers a summary refresh |
| `summary_keep_messages` | `8` | messages kept verbatim after a refresh (never above `recent_max_messages`) |
| `recall_top_k` | `6` | how many long-term memories recall may return (`0` disables recall) |
| `recall_scopes` | `USER`, `PROJECT`, `SESSION`, `TENANT` | scopes recall consults |
| `recency_half_life_hours` | `168.0` | recall recency decay |
| `block_budget_fraction` | `0.15` | share of the prompt a memory block may take |
| `query_rewrite` | `True` | condense the question into a standalone query |
| `extraction_min_importance` | `0.3` | importance floor a proposed fact must clear to be stored |
| `dedup_threshold` | `0.92` | index similarity at or above which a proposed fact is a restatement |
| `entity_confidence_floor` | `0.5` | confidence a resolved entity mention must clear to be stored |
| `entity_overlap_weight` | `0.5` | how strongly recall boosts entity overlap, 0..2 (`0` disables anchoring) |
| `type_affinity_weight` | `0.5` | how strongly recall boosts a wanted memory type, 0..2 (`0` disables the hint) |

`MemoryPolicy.disabled()` is the escape hatch: no model calls, no repository writes, a
four-message window. Every field is validated at construction, so an incoherent policy
raises `MemoryConfigurationError` at wiring time.

### Build order

1. Derive the `ConversationIdentity` from the owner's tenant, principal, and session.
2. Load the last `recent_max_messages` messages, oldest-first.
3. When disabled, return that window with the question unchanged.
4. Load the newest valid `SESSION`/`SUMMARY` memory for the session; its
   `metadata["last_covered_message_id"]` records how far it reaches.
5. Refresh the summary when the window's tokens exceed
   `summary_trigger_fraction * recent_max_tokens` and both a model and a repository are
   configured: summarize everything older than the last `summary_keep_messages`, fold in
   the prior summary, and save it under the deterministic id `summary:<session_id>` so a
   session never accumulates duplicates. `source_message_ids` lists only the messages
   that refresh folded in, so the row never grows without bound;
   `metadata["last_covered_message_id"]` is the frontier.
6. Token-trim the window to `recent_max_tokens` with LangChain's `trim_messages`
   (`strategy="last"`, `start_on="human"`), so the window never opens on a tool result
   separated from the assistant tool call that produced it. The post-summary boundary is
   walked back to the nearest user turn for the same reason, so a refresh may keep more
   than `summary_keep_messages` -- and when the whole window belongs to one user turn
   (an agent turn that is one question followed by a long tool chain) nothing is older
   than the boundary, so no summary is written and the turn rides on token-trimming
   alone. A budget too small for one message still keeps the newest message.
7. Condense the summary, the window, and the question into one self-contained retrieval
   query, and ask the same call which memory types the question wants. An empty or
   implausibly long rewrite is rejected, and `rewritten` is true only when the model
   actually changed the question.

8. Recall the long-term memories worth spending prompt budget on, using the
   standalone query from step 7 so a follow-up searches for what it actually means.
   Recall never fails the turn: without a repository, with `recall_top_k = 0`, or on
   any failure, `recalled` is empty.

Token budgets use `approximate_tokens` unless a `token_counter` is injected -- roughly
four ASCII characters per token, with non-ASCII characters counted individually.

## Long-term memory

`MemoryRecall` reads durable memories back into a turn; `MemoryExtractor` writes the ones
a finished turn stated. Both are wired with the same collaborators, and both degrade
instead of failing the turn.

```python
from harborrag_memory import MemoryExtractor, MemoryPolicy, MemoryRecall

recall = MemoryRecall(
    policy=MemoryPolicy(),
    memories=memory_repo,     # core MemoryRepository
    index=memory_index,       # core MemoryIndex, optional
    embedder=embed_text,      # core MemoryEmbedder, optional
)
memories = await recall.recall(owner, "who owns the ingest pipeline?")

extractor = MemoryExtractor(
    policy=MemoryPolicy(),
    memories=memory_repo,
    model=chat_model,         # langchain_core BaseChatModel
    index=memory_index,
    embedder=embed_text,
    entities=entity_resolver, # core MemoryEntityResolver, optional
)
stored = await extractor.extract(owner, messages=turn_messages)
```

### What is extracted

The model is asked for a list of atomic facts, each one a self-contained third-person
statement with a type (`FACT`, `PREFERENCE`, `DECISION`, or `EPISODE`), a scope, an
importance between 0 and 1, and the entities it is about. The prompt states that the
conversation is untrusted data and must never be followed as instructions, and that
secrets, credentials, keys, and tokens are never to be stored. A fact below
`extraction_min_importance`, or one whose content is blank, is dropped.

### Scope rules

A conversation may only write the three narrow scopes, and never widens them:

| Judgement | Scope |
| --- | --- |
| a stated personal preference or stable trait | `USER` |
| a decision, convention, or fact about the project | `PROJECT` |
| a detail that only matters inside this conversation | `SESSION` |

`TENANT` and `GLOBAL` are never inferred from chat: an unparseable or wider scope falls
back to `SESSION`. A scope the owner cannot address -- a `PROJECT` fact from an owner with
no `project_id` -- degrades to `SESSION` rather than being stored where another project
could read it. Recall applies the same rule in reverse: each scope in `recall_scopes` is
queried as an owner narrowed to exactly the fields
[`scope_owner_fields`](../harborrag-core/src/harborrag_core/ports/memory.py) says that
scope keys on, so a recall can never read a project, user, or session the caller does not
already own. `user_id` is derived from `principal_id` when the owner omits it, exactly as
the rolling summary does.

### Add-only, with validity intervals

Extraction never overwrites and never deletes. Every fact is saved as a new row with
`valid_from = now`, `source_session_id`, the `source_message_ids` it came from, its
`entity_ids` (resolved knowledge-graph node ids when a `MemoryEntityResolver` is wired,
otherwise the model's proposed mentions verbatim), and a `content_hash`.

### Entity anchoring

Conversation memory and the document knowledge graph reinforce each other by *reference*:
**memory is never written into the curated document knowledge graph -- it is only linked
to it by id.** No extraction path creates, merges, or mutates a graph node.

The link is made by the core
[`MemoryEntityResolver`](../harborrag-core/src/harborrag_core/ports/memory.py) port:

```python
@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    mention: str          # the surface form the model proposed
    entity_id: str        # the knowledge-graph node id it resolved to
    confidence: float     # 0..1


class MemoryEntityResolver(Protocol):
    async def resolve_entities(
        self, mentions: Sequence[str], *, tenant_id: str
    ) -> tuple[ResolvedEntity, ...]: ...
```

With a resolver wired, extraction resolves each fact's proposed mentions before saving and
stores the resolved node ids on `Memory.entity_ids`, keeping only resolutions at or above
`entity_confidence_floor`. A mention the graph cannot resolve, or one resolved below the
floor, is simply dropped -- it is noise for ranking, never a reason to discard the fact
that carried it. A resolver that raises is logged at WARNING and yields no anchors at all,
so extraction still succeeds. Without a resolver, the model's mentions are stored verbatim.

Recall reads the link back. `MemoryRecall.recall(owner, query, anchor_entity_ids=...)` and
`MemoryContextBuilder.build(owner, question, anchor_entity_ids=...)` take the graph
entities this turn's *document* retrieval actually surfaced, and boost the memories that
share them (see [Recall ranking](#recall-ranking)). The anchors used are echoed back as
`MemoryContext.anchor_entity_ids`, and `recalled_entity_ids(context)` returns the
deduplicated union of `entity_ids` across the recalled memories in recall order -- the
seed set for a graph-anchored follow-up search.

### Supersession

When a new fact contradicts a stored one, the model copies that memory's bracketed
reference -- shown to it as `[<content_hash prefix>]` next to the memory's content -- into
the fact's `replaces` field. The extractor resolves that reference by **exact lookup**
against the memories it rendered into the prompt; a reference it does not recognise, or
one naming a memory in a different scope or already superseded, is ignored. A resolved
prior row is amended in place: `invalid_at = now` and `superseded_by = <new memory id>`.
Its content stays readable, so "what did we believe last week" is still answerable, and
`Memory.is_valid_at(now)` is what hides it from recall.

### Deduplication

Two gates, cheapest first:

1. **Content hash.** `sha256(scope + normalized content)`, where normalizing collapses
   whitespace runs and folds case. A fact whose hash already exists among the owner's
   memories valid now is skipped, which is what makes a retried turn a no-op.
2. **Index similarity.** When an index and an embedder are both wired, a fact whose
   nearest memory in the same scope scores at or above `dedup_threshold` is skipped as a
   restatement. The comparison is against the index's own score scale.

Recall dedupes its results the same way -- by `content_hash` when set, else by normalized
content -- keeping the highest-scoring instance.

### Recall ranking

Candidates come from the semantic index when both an index and an embedder are wired (the
index returns ids and scores; the canonical repository hydrates them and re-applies the
scope filter). Without an embedder, or when the index raises, recall falls back to the
repository's own substring search with a flat relevance, so recency and importance decide
the order. Each candidate is then scored:

```text
final   = relevance * (1 + recency) * (1 + importance) * (1 + entity) * (1 + type)
recency = 0.5 ** (hours_since_updated / recency_half_life_hours)
entity  = entity_overlap_weight * (overlapping_ids / max(1, len(memory.entity_ids)))
type    = type_affinity_weight when memory.memory_type was asked for, else 0
```

`entity` is the share of the memory's own `entity_ids` that appear in
`anchor_entity_ids`. It is non-negative by construction, so entity overlap can only
re-rank -- never exclude: a memory with no `entity_ids`, an empty anchor set, or
`entity_overlap_weight = 0` scores 0 and is left exactly where it was.

`type` is the **type hint**, and it comes from the query rewrite of step 7: the same
per-turn call that resolves the follow-up also reports which kinds of memory the question
is asking for -- decisions for "why did we pick pgvector", preferences for "how do you
want this formatted" -- so the type judgement costs no extra model call. At most three
types are accepted, unknown or non-recallable names are dropped, and a model that cannot
be asked for structured output falls back to the plain-text rewrite and hints nothing.
`MemoryRecall.recall(owner, query, wanted_types=...)` takes a hint directly; the builder
passes the one its rewrite returned and echoes it back as `MemoryContext.wanted_types`.

The boost is binary rather than graded by the model's ordering, and like `entity` it is
non-negative by construction: the hint **re-ranks only**. Every recall still queries all
of `RECALL_MEMORY_TYPES`, so a hint for one type never hides the others -- it ranks them
lower. Retention is deliberately untouched by all of this: what a memory's type is worth
to *ranking* is a per-turn question, while how long a memory lives stays keyed on its
scope.

Ties break toward the narrower scope (`SESSION` < `USER` < `PROJECT` < `TENANT`). The
ranking is deduplicated, capped at `recall_top_k`, and then truncated at the first memory
that would push the rendered block past `block_budget_fraction * recent_max_tokens`, so
the block is always a prefix of the ranking and never exceeds its share of the prompt.
Rolling summaries are not recalled -- they are already `MemoryContext.summary`.

### Failure semantics

| Failure | Recall |
| --- | --- |
| embedder raises | WARNING, substring fallback |
| index raises | WARNING, substring fallback |
| repository raises | WARNING, no candidates for that scope |
| entity resolver raises | WARNING, the fact is stored with no `entity_ids` |

Extraction has one rule: every failure -- the model, the embedder, the index, or the
repository -- is logged at WARNING and `extract` returns whatever was already saved, which
is `()` when the failure lands before the first save. Neither ever raises into the caller.

## Module ownership

- `tiers/short_term.py` - conversation history facade.
- `tiers/working.py` - per-run scratch state facade and local store.
- `tiers/long_term.py` - canonical repository facade for durable memory.
- `manager.py` - `MemoryManager`, the single facade chat and agent import.
- `schemas.py` - stable re-exports of the core-owned memory contracts.
- `config.py` - `MemoryManagerConfig`, including the default recent-turn limit.
- `errors.py` - `MemoryError`, `MemoryConfigurationError`, `MemoryScopeError`.
- `langchain/` - `HarborChatMessageHistory` and the `ConversationMessage` converters.
- `context/` - per-turn context: `policy.py`, `builder.py`, `result.py`, `summarizer.py`,
  `condenser.py`, `trimming.py`, `tokens.py`, `prompts.py`, `prompting.py`.
- `context/recall.py`, `context/ranking.py` - long-term recall and its pure scoring rules.
- `context/entities.py` - mention resolution, entity anchoring, and graph-seed extraction.
- `context/extraction.py`, `context/facts.py` - fact extraction and the model-facing schema.
- `context/hashing.py`, `context/scoping.py` - content identity and per-scope owner narrowing.

## Development

Tests for this package live in `packages/harborrag-memory/tests/`. Run them from the
repository root:

```bash
uv run pytest packages/harborrag-memory/tests
```

Licensed under the Apache License 2.0.
