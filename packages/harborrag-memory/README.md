# harborrag-memory

Scope-aware conversation memory for chat in
[HarborRAG](https://github.com/cbtw-apac/HarborRAG): recent turns, a rolling session
summary, long-term memories recalled into each turn, and facts extracted from finished
turns. This package owns the memory facade and policies; storage lives in
`harborrag-adapters` and the contracts live in `harborrag-core`.

## Install

```bash
pip install harborrag-memory
```

`harborrag-app` depends on this package, so every install of the
[`harborrag`](https://pypi.org/project/harborrag/) facade already includes it.

Model-driven policies take a `MemoryModel`: asynchronous `generate_text` and
`generate_structured` methods that receive plain system and user strings. A LangChain
`BaseChatModel` works too, through a compatibility adapter. LangChain also supplies message
conversion and window trimming; LangGraph is not required.

## Quick start

The in-memory conversation store comes from `harborrag-runtime`, so install that package
(or `harborrag`) to run this example; any `ConversationRepository` works in its place.

```python
import asyncio

from harborrag_memory import (
    ConversationIdentity,
    ConversationTurn,
    MemoryManager,
    MemoryOwner,
    ShortTermMemory,
)
from harborrag_runtime.memory import InMemoryConversationMemory


async def main() -> None:
    owner = MemoryOwner(tenant_id="tenant-1", principal_id="user-1", session_id="session-1")
    store = InMemoryConversationMemory()
    await store.create(ConversationIdentity("tenant-1", "user-1", "session-1", "user-1"))
    memory = MemoryManager(short_term=ShortTermMemory(store))

    await memory.append(owner, ConversationTurn("Who owns ingestion?", "Dana Lee."))
    snapshot = await memory.snapshot(owner)
    print(snapshot.recent_turns)


asyncio.run(main())
```

It prints the stored turn:

```text
(ConversationTurn(user_content='Who owns ingestion?', assistant_content='Dana Lee.'),)
```

## What it provides

### Two tiers, one facade

| Tier | Holds | Lifetime |
| --- | --- | --- |
| Short-term | conversation turns | the session |
| Long-term | durable memories in a repository | until deleted or expired |

`MemoryManager` is the single facade chat code imports. Each tier is optional: pass only
the ones you configured. Calling into a tier you did not configure raises
`MemoryConfigurationError`.

| Tier | Methods |
| --- | --- |
| Short-term | `recent(owner, limit=...)`, `append(owner, turn)`, `clear(owner)` |
| Long-term | `save(caller, memory)`, `get(caller, memory_id)`, `search(caller, query)`, `delete(caller, memory_id)` |
| Combined | `snapshot(owner, query=..., recent_limit=...)` |

### Everything is scoped by `MemoryOwner`

Every public operation takes a `MemoryOwner`: the isolation key a memory is written
under, or the identity a query is issued as.

```python
from harborrag_memory import MemoryOwner

owner = MemoryOwner(
    tenant_id="tenant-1",
    project_id="handbook",
    principal_id="user-1",
    session_id="session-1",
)
```

Which owner fields must match depends on the memory's `MemoryScope`:

| Scope | Owner fields that must match |
| --- | --- |
| `GLOBAL` | none |
| `TENANT` | `tenant_id` |
| `PROJECT` | `tenant_id`, `project_id` |
| `USER` | `tenant_id`, `user_id` |
| `SESSION` | `tenant_id`, `user_id`, `session_id` |

> **Security rule.** Build the owner from the authenticated request context. Never pass
> owner fields the user supplied: that lets a caller read another tenant's, project's or
> user's memory. `snapshot()` enforces this by rejecting a query whose owner does not
> match the authenticated caller.

## Assemble the context of one turn

`MemoryContextBuilder` gathers everything one chat turn needs from memory into one
immutable `MemoryContext`: the verbatim window to replay, the rolling session summary of
the turns the window dropped, and the standalone query to send to retrieval.

```python
from harborrag_memory import MemoryContextBuilder, MemoryPolicy

builder = MemoryContextBuilder(
    policy=MemoryPolicy(),
    messages=message_store,   # core ConversationMessageStore
    memories=memory_repo,     # core MemoryRepository, optional
    model=chat_model,         # MemoryModel or LangChain BaseChatModel, optional
    index=memory_index,       # core MemoryIndex, optional
    embedder=embed_text,      # core MemoryEmbedder, optional
)
context = await builder.build(owner, "and who owns it?")
context.messages          # replay window, oldest first
context.summary           # rolling session summary, or None
context.recalled          # long-term memories, already ranked and budgeted
context.standalone_query  # what retrieval should search for
context.wanted_types      # memory types the rewrite said the question wants
```

> **Contract.** Do not persist the question being answered before you call `build`. The
> window it returns is the history before this turn; append the user message afterwards.
> A caller that appends first sees its own question replayed inside the window.
>
> `MemoryScope.SESSION` keys on `user_id`, so the builder derives `user_id` from
> `principal_id` when the owner omits it, for saves and queries alike. Supply `user_id`
> consistently, or omit it consistently: alternating between the two for the same person
> produces two visibility keys and loses the session's summary.

`build` requires an owner with both `principal_id` and `session_id`, and raises
`MemoryScopeError` otherwise. It never fails the turn for a model or repository error:
every such failure is logged at WARNING and the turn continues with the trimmed history
and the original question.

### `MemoryPolicy`

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `True` | When false, return a plain window and call no model |
| `recent_max_messages` | `12` | How many messages are loaded from history |
| `recent_max_tokens` | `2000` | Token budget for the verbatim window |
| `summary_trigger_fraction` | `0.7` | Fraction of that budget that triggers a summary refresh |
| `summary_keep_messages` | `8` | Messages kept verbatim after a refresh (never above `recent_max_messages`) |
| `recall_top_k` | `6` | How many long-term memories recall may return (`0` turns recall off) |
| `recall_scopes` | `USER`, `PROJECT`, `SESSION`, `TENANT` | Scopes recall consults |
| `recency_half_life_hours` | `168.0` | Recall recency decay |
| `block_budget_fraction` | `0.15` | Share of the prompt a memory block may take |
| `query_rewrite` | `True` | Condense the question into a standalone query |
| `extraction_min_importance` | `0.3` | Importance a proposed fact must reach to be stored |
| `dedup_threshold` | `0.92` | Index similarity at or above which a proposed fact is a restatement |
| `entity_confidence_floor` | `0.5` | Confidence a resolved entity mention must reach to be stored |
| `entity_overlap_weight` | `0.5` | How strongly recall boosts entity overlap, 0 to 2 (`0` turns anchoring off) |
| `type_affinity_weight` | `0.5` | How strongly recall boosts a wanted memory type, 0 to 2 (`0` turns the hint off) |
| `extraction_project_scope` | `False` | Let extraction store facts at `PROJECT` scope, where every user of the project recalls them |
| `retention` | `MemoryRetention()` | Days a new memory is kept per scope; the default expires nothing |

`MemoryPolicy.disabled()` is the escape hatch: no model calls, no repository writes and a
four-message window. Every field is validated at construction, so an incoherent policy
raises `MemoryConfigurationError` when you wire it.

`retention` sets `expires_at` on every memory that extraction and the rolling summary
write, and repositories stop returning a memory once it has expired. `TENANT` and `GLOBAL`
memories never expire. The runtime fills it from the `HARBORRAG_MEMORY_RETENTION_DAYS_*`
settings, described in
[Conversation memory](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/memory.md#sdk-memory-settings).

### Build order

1. Derive the `ConversationIdentity` from the owner's tenant, principal and session.
2. Load the last `recent_max_messages` messages, oldest first.
3. When the policy is disabled, return that window with the question unchanged.
4. Load the newest valid `SESSION`/`SUMMARY` memory for the session; its
   `metadata["last_covered_message_id"]` records how far it reaches.
5. Refresh the summary when the window's tokens exceed
   `summary_trigger_fraction * recent_max_tokens` and both a model and a repository are
   configured:
   - Summarize everything older than the last `summary_keep_messages`, fold in the prior
     summary, and save it under the deterministic ID `summary:<session_id>`, so a session
     never accumulates duplicates.
   - `source_message_ids` lists only the messages that refresh folded in, so the row never
     grows without bound; `metadata["last_covered_message_id"]` is the frontier.
     Previously covered messages are left out of later refreshes, so unchanged history
     does not call the summary model again.
   - If the frontier is older than the recent window, unread history is summarized in at
     most four bounded pages per turn. Any remaining backlog resumes from the saved
     frontier on later turns, and the current turn still gets its recent history window.
     A history cursor that does not advance stops the catch-up safely.
   - An existing summary keeps its original ownership and provenance fields across
     credential or project-context changes within the same user-owned session.
6. Token-trim the window to `recent_max_tokens` with LangChain's `trim_messages`
   (`strategy="last"`, `start_on="human"`), so the window never opens on an answer whose
   question was cut. For the same reason, the post-summary boundary is walked back to the
   nearest user turn, so a refresh may keep more than `summary_keep_messages`. A budget
   too small for one message still keeps the newest message.
7. Condense the summary, the window and the question into one self-contained retrieval
   query, and ask in the same call which memory types the question wants. An empty or
   implausibly long rewrite is rejected, and `rewritten` is true only when the model
   actually changed the question.
8. Recall the long-term memories worth spending prompt budget on, using the standalone
   query from step 7, so a follow-up searches for what it actually means. Recall never
   fails the turn: without a repository, with `recall_top_k = 0`, or on any failure,
   `recalled` is empty.

Token budgets use `approximate_tokens` unless you inject a `token_counter`: roughly four
ASCII characters per token, with each non-ASCII character counted on its own.

## Recall and extract long-term memories

`MemoryRecall` reads durable memories back into a turn; `MemoryExtractor` writes the ones
a finished turn stated. Both take the same collaborators, and both degrade instead of
failing the turn.

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
    model=chat_model,         # MemoryModel or LangChain BaseChatModel
    index=memory_index,
    embedder=embed_text,
    entities=entity_resolver, # core MemoryEntityResolver, optional
)
stored = await extractor.extract(owner, messages=turn_messages)
```

### What is extracted

The model is asked for a list of atomic facts. Each is a self-contained third-person
statement with a type (`FACT`, `PREFERENCE`, `DECISION` or `EPISODE`), a scope, an
importance between 0 and 1, and the entities it is about. The prompt states that the
conversation is untrusted data that must never be followed as instructions, and that
secrets, credentials, keys and tokens are never stored. A fact below
`extraction_min_importance`, or one with blank content, is dropped.

### Scope rules

A conversation writes only the three narrow scopes, and never widens them:

| Judgement | Scope |
| --- | --- |
| A stated personal preference or stable trait | `USER` |
| A decision, convention or fact about the project | `PROJECT` when `extraction_project_scope` is on, otherwise `SESSION` |
| A detail that matters only inside this conversation | `SESSION` |

`TENANT` and `GLOBAL` are never inferred from chat: an unparseable or wider scope falls
back to `SESSION`. A scope the owner cannot address, such as a `PROJECT` fact from an
owner with no `project_id`, also degrades to `SESSION` rather than being stored where
another project could read it.

Recall applies the same rule in reverse. Each scope in `recall_scopes` is queried as an
owner narrowed to exactly the fields that
[`scope_owner_fields`](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-core/src/harborrag_core/ports/memory.py)
says the scope keys on, so a recall never reads a project, user or session the caller
does not already own. `user_id` is derived from `principal_id` when the owner omits it,
exactly as the rolling summary does.

### Add-only, with validity intervals

Extraction never overwrites and never deletes. Every fact is saved as a new row with
`valid_from = now`, `source_session_id`, the `source_message_ids` it came from, its
`entity_ids`, a `content_hash` and the `expires_at` its scope's retention gives. The
`entity_ids` are resolved knowledge-graph node IDs when a `MemoryEntityResolver` is wired,
otherwise the model's proposed mentions verbatim.

### Entity anchoring

Conversation memory and the document knowledge graph reinforce each other by reference:
**memory is never written into the curated document knowledge graph; it is only linked to
it by ID.** No extraction path creates, merges or changes a graph node.

The core
[`MemoryEntityResolver`](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-core/src/harborrag_core/ports/memory.py)
port makes the link:

```python
@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    mention: str          # the surface form the model proposed
    entity_id: str        # the knowledge-graph node ID it resolved to
    confidence: float     # 0..1


class MemoryEntityResolver(Protocol):
    async def resolve_entities(
        self, mentions: Sequence[str], *, tenant_id: str
    ) -> tuple[ResolvedEntity, ...]: ...
```

With a resolver wired, extraction resolves each fact's proposed mentions before saving and
stores the resolved node IDs on `Memory.entity_ids`, keeping only resolutions at or above
`entity_confidence_floor`. A mention the graph cannot resolve, or one resolved below the
floor, is dropped: it is noise for ranking, never a reason to discard the fact that carried
it. A resolver that raises is logged at WARNING and yields no anchors, so extraction still
succeeds. Without a resolver, the model's mentions are stored verbatim.

Recall reads the link back. `MemoryRecall.recall(owner, query, anchor_entity_ids=...)` and
`MemoryContextBuilder.build(owner, question, anchor_entity_ids=...)` take the graph
entities this turn's document retrieval actually surfaced, and boost the memories that
share them (see [Recall ranking](#recall-ranking)). The anchors used are echoed back as
`MemoryContext.anchor_entity_ids`. `recalled_entity_ids(context)` returns the
deduplicated union of `entity_ids` across the recalled memories, in recall order: the
seed set for a graph-anchored follow-up search.

### Supersession

When a new fact contradicts a stored one, the model copies that memory's bracketed
reference, shown to it as `[<content_hash prefix>]` next to the memory's content, into the
fact's `replaces` field. The extractor resolves that reference by **exact lookup** against
the memories it rendered into the prompt. A reference it does not recognise, or one naming
a memory in a different scope or already superseded, is ignored. A resolved prior row is
amended in place: `invalid_at = now` and `superseded_by = <new memory id>`. Its content
stays readable, so "what did we believe last week" can still be answered, and
`Memory.is_valid_at(now)` is what hides it from recall.

### Deduplication

Two checks run, cheapest first:

1. **Content hash.** `sha256(scope + normalized content)`, where normalizing collapses
   whitespace runs and folds case. A fact whose hash already exists among the owner's
   memories valid now is skipped, which makes a retried turn a no-op.
2. **Index similarity.** When an index and an embedder are both wired, a fact whose
   nearest memory in the same scope scores at or above `dedup_threshold` is skipped as a
   restatement. The comparison uses the index's own score scale.

Recall dedupes its results the same way, by `content_hash` when set and otherwise by
normalized content, keeping the highest-scoring instance.

### Recall ranking

Candidates come from the semantic index when both an index and an embedder are wired: the
index returns IDs and scores, and the canonical repository loads the memories and applies
the scope filter again. Without an embedder, or when the index raises, recall falls back
to the repository's own substring search with a flat relevance, so recency and importance
decide the order. Each candidate is then scored:

```text
final   = relevance * (1 + recency) * (1 + importance) * (1 + entity) * (1 + type)
recency = 0.5 ** (hours_since_updated / recency_half_life_hours)
entity  = entity_overlap_weight * (overlapping_ids / max(1, len(memory.entity_ids)))
type    = type_affinity_weight when memory.memory_type was asked for, else 0
```

`entity` is the share of the memory's own `entity_ids` that appear in
`anchor_entity_ids`. It is never negative, so entity overlap can only re-rank, never
exclude: a memory with no `entity_ids`, an empty anchor set or
`entity_overlap_weight = 0` scores 0 and keeps its place.

`type` is the **type hint**, and it comes from the query rewrite of step 7: the same
per-turn call that resolves the follow-up also reports which kinds of memory the question
asks for, such as decisions for "why did we pick pgvector" or preferences for "how do you
want this formatted". The type judgement therefore costs no extra model call. At most
three types are accepted, unknown or non-recallable names are dropped, and a model that
cannot return structured output falls back to the plain-text rewrite and hints nothing.
`MemoryRecall.recall(owner, query, wanted_types=...)` takes a hint directly; the builder
passes the one its rewrite returned and echoes it back as `MemoryContext.wanted_types`.

The boost is binary rather than graded by the model's ordering, and like `entity` it is
never negative: the hint **only re-ranks**. Every recall still queries all of
`RECALL_MEMORY_TYPES`, so a hint for one type never hides the others; it ranks them lower.
Retention does not depend on any of this: what a memory's type is worth to ranking is a
per-turn question, while how long a memory lives stays keyed on its scope.

Ties break toward the narrower scope (`SESSION` < `USER` < `PROJECT` < `TENANT`). The
ranking is deduplicated, capped at `recall_top_k` and then cut at the first memory that
would push the rendered block past `block_budget_fraction * recent_max_tokens`, so the
block is always a prefix of the ranking and never exceeds its share of the prompt. Rolling
summaries are not recalled; they are already `MemoryContext.summary`.

### Failure handling

| Failure | Recall |
| --- | --- |
| Embedder raises | WARNING, substring fallback |
| Index raises | WARNING, substring fallback |
| Repository raises | WARNING, no candidates for that scope |
| Entity resolver raises | WARNING, the fact is stored with no `entity_ids` |

Extraction has one rule: every failure, whether from the model, the embedder, the index
or the repository, is logged at WARNING and `extract` returns whatever was already saved,
which is `()` when the failure comes before the first save. Neither recall nor extraction
ever raises into the caller.

## Contributing

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md).
Where the code lives:

- `manager.py`: `MemoryManager`, the single facade chat imports.
- `tiers/short_term.py`: the conversation history facade.
- `tiers/long_term.py`: the canonical repository facade for durable memory.
- `schemas.py`: stable re-exports of the core-owned memory contracts.
- `identity.py`: the one derivation of the conversation identity an owner maps to.
- `config.py`: `MemoryManagerConfig`, including the default recent-turn limit.
- `errors.py`: `MemoryError`, `MemoryConfigurationError` and `MemoryScopeError`.
- `langchain/`: `HarborChatMessageHistory`, the `ConversationMessage` converters and the
  compatibility adapter for LangChain models.
- `context/`: per-turn context: `policy.py`, `builder.py`, `result.py`, `summarizer.py`,
  `condenser.py`, `trimming.py`, `tokens.py`, `model.py`, `prompts.py` and
  `prompting.py`.
- `context/recall.py` and `context/ranking.py`: long-term recall and its pure scoring
  rules.
- `context/entities.py`: mention resolution, entity anchoring and graph-seed extraction.
- `context/extraction.py`, `context/proposing.py` and `context/facts.py`: fact extraction,
  the request for proposed facts and the model-facing schema.
- `context/strict_schema.py`: makes a Pydantic schema acceptable to strict structured
  output.
- `context/hashing.py` and `context/scoping.py`: content identity and per-scope owner
  narrowing.

Tests for this package live in `packages/harborrag-memory/tests/`. Run them from the
repository root:

```bash
uv run pytest packages/harborrag-memory/tests
```

## License

Apache License 2.0.
