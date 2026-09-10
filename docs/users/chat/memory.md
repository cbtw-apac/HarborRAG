# Conversation memory

What a chat turn remembers between messages and between sessions, how
it is recalled, and how to inspect or erase it.

A conversation is owned by the **end user**, not by the credential that
carried the request. Memory is keyed by
`(tenant, end user, session_id)`, with the authenticated principal kept
alongside it for audit and provenance. That distinction matters when one
service credential fronts several people: `session_id` is not what keeps their
histories apart, so nobody reads another person's conversation by guessing a
session ID.

Each turn is stored as two messages -- the user prompt and the assistant
answer, the latter carrying its `citations` and completion token count. They
are written separately: the prompt as soon as the turn is committed to, the
answer as soon as any answer text exists. An answer that stopped early is
marked partial (see [Streaming](README.md#streaming)). Agent runs store their question
and final answer the same way, tagged with the `run_id`; intermediate tool
calls stay in the run checkpoint, and an interrupted run keeps its checkpoint
so it can be resumed. Configure it through `HARBORRAG_CONTROL_DB_URL`, using a
`postgresql+asyncpg://...` DSN in deployed environments.

How much of that history reaches the model is a policy, not a fixed number of
turns -- see [Which history is used](#which-history-is-used) below.

Alongside the principal, each turn records the **end-user identity** the
memory belongs to. With `HARBORRAG_AUTH_MODE=hmac` it is read from the JWT
claim named by `HARBORRAG_AUTH_USER_ID_CLAIM` (default `sub`, so principal and
user coincide). Set it to `oid`, `email`, or whichever claim your identity
provider issues when one service credential acts on behalf of several people;
a token without that claim falls back to `sub`, while a token carrying it with
anything but a non-empty string is rejected with `401`. With
`HARBORRAG_AUTH_MODE=none` the identity is the implicit `dev` principal, and
the CLI always acts as `harborrag-cli`.

The provider-neutral `ConversationMemory` port lives in `harborrag-core`; its
SQL implementation lives in `harborrag-adapters`. Chat and agent orchestration
therefore do not depend on SQLAlchemy or PostgreSQL.

## Which history is used

Before each turn the memory layer assembles one context and uses it for both
the prompt and retrieval. It runs in this order:

1. **Recent window.** The last `HARBORRAG_MEMORY_RECENT_MAX_MESSAGES` messages,
   then trimmed to `HARBORRAG_MEMORY_RECENT_MAX_TOKENS` by dropping the oldest
   first. A tool result is never separated from the tool call it answers.
2. **Rolling summary.** When the loaded window exceeds
   `HARBORRAG_MEMORY_SUMMARY_TRIGGER_FRACTION` of that token budget, everything
   older than the last `HARBORRAG_MEMORY_SUMMARY_KEEP_MESSAGES` messages is
   summarized into one cumulative session summary and the window shrinks to
   that tail. The summary is stored as a session-scoped memory, so it is
   written once and reused on later turns rather than recomputed.
3. **Standalone query.** With `HARBORRAG_MEMORY_QUERY_REWRITE=true` the summary,
   the window, and the new question are condensed into one self-contained
   retrieval query. This is what makes follow-ups like "and who owns it?"
   retrieve the right documents: pronouns and ellipsis are resolved from
   history before the search runs. The model still receives the question as the
   user typed it; only retrieval sees the rewritten form, and the response
   metadata reports it as `retrieval_query`.
4. **Injection.** The summary and any recalled memories are placed in one
   `<conversation_memory trust="untrusted">` block ahead of the retrieved
   context. It is labeled untrusted and its delimiters are escaped, so text a
   user pasted in an earlier turn cannot issue instructions in a later one.

Every step degrades rather than fails. If the memory store or the memory model
is unavailable the turn still answers from retrieval alone, logged with
identifiers only. Setting `HARBORRAG_MEMORY_ENABLED=false` restores a short
verbatim window with no summary, rewriting, or recall.

The agent surface uses the same window and summary. Its retrieval tools choose
their own queries, so the standalone query is not forced on them; the summary
reaches the agent as part of its instructions, also labeled untrusted.

## Long-term memory

The window and the summary only ever describe *one* session. Long-term memory
is what carries across them: durable facts distilled from finished exchanges
and recalled on later turns, possibly weeks later and in another session.

**What gets extracted.** After a turn is written to conversation history, the
exchange is handed to the memory model, which proposes candidate facts --
preferences, decisions, stable attributes -- each with an importance between
`0` and `1`. Anything below
`HARBORRAG_MEMORY_EXTRACTION_MIN_IMPORTANCE` is discarded, and -- when a
vector index and an embedder are both wired -- a candidate whose embedding
scores at or above `HARBORRAG_MEMORY_DEDUP_THRESHOLD` against a memory already
stored in that scope is treated as a restatement rather than a new fact.
Passing questions, small talk, and anything that only made sense inside that
one turn are not memories.

**Add-only, with validity intervals.** Nothing is edited in place. A fact that
stops being true is not rewritten: the stored row gets an `invalid_at` and a
`superseded_by` pointing at its replacement, and the replacement is written as
a new row with its own `valid_from`. Recall only ever returns memories valid
*now*, so the correction takes effect immediately, while the history of what
was believed and when stays readable and auditable.

**Recall ranking.** Each turn consults the scopes listed in
`HARBORRAG_MEMORY_RECALL_SCOPES` and keeps at most
`HARBORRAG_MEMORY_RECALL_TOP_K` memories. Relevance to the question leads --
a memory that does not answer it is worthless however fresh -- and is then
modulated by the stored importance and by recency decayed with a half-life of
`HARBORRAG_MEMORY_RECALL_RECENCY_HALF_LIFE_HOURS`, so a strong old preference
can still outrank a weak recent one; the narrower scope wins a tie. When
`HARBORRAG_MEMORY_EMBED_PROFILE` (or the embed catalog's default model) and a
vector index are both available, relevance is semantic; without them recall
falls back to the store's lexical filter, which is a weaker ranking rather
than a broken turn. Whatever survives is injected in the same untrusted
`<conversation_memory>` block as the summary.

**The type hint.** Some questions ask for a kind of memory rather than a
subject: "what did we decide" wants decisions, "how do I like my summaries"
wants preferences. The same model call that condenses history into the
standalone query also reports which memory types the question is reaching for,
so the hint costs no extra request and adds no latency of its own. Recall then
boosts the memories whose type matches, weighted by
`HARBORRAG_MEMORY_TYPE_AFFINITY_WEIGHT`. It is a boost, not a filter: every
recallable type is still searched, so a fact that answers a question about
decisions is never hidden because it is a fact. The hint is absent on the first
turn of a session, when `HARBORRAG_MEMORY_QUERY_REWRITE=false`, when the memory
model's deployment does not declare structured output, and whenever the call
fails -- in each case ranking behaves exactly as it did before, on relevance,
recency, importance, and entity overlap. Setting the weight to `0` disables the
type term the same way.

Retention stays keyed on scope, not type, and deliberately so: how long a
memory is worth keeping follows who it belongs to, not what shape it is. The
type hint changes what a turn *retrieves*, never what survives.

The vector index is a dedicated `memories` collection per tenant, built over
the same Qdrant client retrieval already holds, so a process keeps one
connection pool. It stores only ids, scope, ownership, and validity in the
payload -- never memory content, because PostgreSQL is the system of record
and the index can be rebuilt from it. Documents and memories therefore never
share a collection, which is what lets a user erase their own memories without
touching indexed documents. If Qdrant is unreachable, or recall is switched
off with `HARBORRAG_MEMORY_RECALL_TOP_K=0`, no collection is created and
recall uses the lexical fallback.

**Scopes.** A memory is stored at the narrowest scope it is true for, and a
scope decides who can ever see it again:

| Scope | Visible to | Typical content |
| --- | --- | --- |
| `session` | the same `(tenant, user, session)` | the rolling session summary |
| `user` | the same `(tenant, user)` | preferences, stable personal facts |
| `project` | anyone in the `(tenant, project)` | project conventions and decisions |
| `tenant` | anyone in the tenant | organization-wide facts |

Visibility is enforced in the query itself, not filtered afterwards: a caller
missing a field the scope requires can never match a memory at that scope.

**Extraction is best effort.** It costs a model call, so it never runs inside
the request. A finished exchange is put on a bounded in-process queue drained
by a small worker pool that the API process starts on boot and drains on
shutdown. That means:

* the answer is returned before extraction runs, and never waits for it;
* if the queue is full the exchange is dropped with a warning (identifiers
  only, never prompt text) -- the conversation is already persisted, so what
  is lost is a set of derived facts, not the turn;
* only a turn that was actually written to history is submitted, because
  extracted memories cite its message ids as provenance;
* an extraction that fails or exceeds its per-item timeout is logged and
  abandoned; it never retries and never blocks shutdown.

Set `HARBORRAG_MEMORY_EXTRACTION_ENABLED=false` to turn extraction off
entirely; recall of already-stored memories keeps working. The CLI never runs
the queue, so CLI turns are remembered verbatim but never extracted.

## Entity anchoring

Recall ranks on relevance, recency, and importance -- none of which know what
this particular turn is *about*. Entity anchoring adds that missing signal by
joining conversation memory to the document knowledge graph.

**Memory is linked to the graph, never written into it.** When extraction
proposes a fact it also proposes the entities it mentions, as plain surface
forms ("the Atlas migration"). Those forms are resolved against the tenant's
knowledge graph and the resulting node ids -- not the words -- are stored on
the memory's `entity_ids`. Resolution is a read of one node: nothing is added
to the graph, nothing in it is modified, and the graph stays the curated system
of record for what entities exist. A mention the graph does not know is simply
dropped, and the fact is still stored without it.

Confidence is the exactness of the match, because a node lookup returns no
score: `1.0` when the mention already *is* a node id, `0.9` for an exact title
match, `0.75` when only a case-insensitive title matched. Anything below
`entity_confidence_floor` (`0.5`) is not stored as an anchor. Resolution is
tenant-scoped, capped at 16 mentions per exchange, and never fails a turn --
an unreachable graph means unresolved mentions and nothing more.

**Anchors flow both ways within a turn.** The order of one chat turn is:

1. assemble the context, which produces the standalone retrieval query;
2. search documents for that query, seeded with the graph entities the
   memories recalled in step 1 already reference, so graph traversal can start
   from ground the user's own history established;
3. read back which graph nodes the search results actually sat on, and re-run
   **only** the recall step anchored on them.

Step 3 re-ranks; it never drops or adds candidates, and it never costs a second
model call -- the standalone query from step 1 is reused verbatim, so the
rewrite and the rolling summary are paid for exactly once per turn. It is
skipped entirely when the search reported no graph nodes, when no memory store
is wired, or when recall is switched off, and any failure keeps the unanchored
context rather than losing the memories the prompt already had.

Both directions need graph observation on for the turn, which is the same
`graph_search` switch document retrieval already uses -- with it off, seeds are
inert and the search reports no nodes to anchor on.

Set `HARBORRAG_MEMORY_ENTITY_LINKING=false` to stop resolving mentions: no new
anchors are recorded, so recall gradually returns to ranking on relevance,
recency, importance, and the type hint alone. Ids already stored on existing memories are not
deleted -- they keep seeding and re-ranking until the memories carrying them
expire, and `entity_overlap_weight` set to `0` disables the re-ranking
contribution outright.

## Inspect and erase memory

Every route below derives the memory owner from the verified token -- the
authenticated subject plus the claim named by `HARBORRAG_AUTH_USER_ID_CLAIM`
-- so a caller can only ever see or erase their own memory. `tenant` defaults
to `DEFAULT` and must be one the token may access.

List what is remembered about you, with provenance:

```bash
curl -s "http://localhost:8000/v1/memory/memories?tenant=DEFAULT&limit=20" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "memories": [
    {
      "memory_id": "mem-...",
      "scope": "user",
      "memory_type": "preference",
      "content": "Prefers metric units",
      "importance": 0.6,
      "valid_from": "2026-01-05T09:12:44+00:00",
      "invalid_at": null,
      "superseded_by": null,
      "source_session_id": "session-...",
      "source_message_ids": ["msg-...", "msg-..."],
      "entity_ids": [],
      "created_at": "2026-01-05T09:12:44+00:00",
      "updated_at": "2026-01-05T09:12:44+00:00"
    }
  ]
}
```

Optional `project_id`, `session_id`, and `scope` narrow the listing (a
`session`-scoped memory is only visible when you name its `session_id`);
`limit` is `1..100`.

Forget one memory (`404` when it is not visible to you), which also drops its
vector point:

```bash
curl -s -X DELETE \
  "http://localhost:8000/v1/memory/memories/mem-...?tenant=DEFAULT" \
  -H "Authorization: Bearer $TOKEN"
```

Erase one of your own sessions -- its messages, its session-scoped memories,
and their vectors:

```bash
curl -s -X DELETE \
  "http://localhost:8000/v1/memory/sessions/session-...?tenant=DEFAULT" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "session_id": "session-...",
  "memories": 3,
  "index_points": 3,
  "sessions": 1,
  "conversation_messages_cleared": 1,
  "agent_run_checkpoints": 0
}
```

Right to erasure for one end user (**`admin` role**), across the tenant:

```bash
curl -s -X DELETE \
  "http://localhost:8000/v1/memory/users/alice?tenant=DEFAULT" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

The response uses the same counts, and is deliberately a report of what was
removed rather than a claim of completeness. Two limits are worth knowing:

* Sessions are reached *through* the memories they produced -- the erasure
  path does not enumerate the user's conversations -- so a conversation
  that never produced a user-scoped memory keeps its messages. Erase it by
  session id instead, or with `DELETE /v1/chat/conversations/{session_id}`.
* `agent_run_checkpoints` is `0` unless the configured checkpoint store
  offers an owner-scoped delete; the endpoint never implies more than it did.

The three read/erase routes require the `reader` role; user erasure requires
`admin`. Every erasure is logged at `INFO` with the tenant, the actor, the
target, and the counts -- never memory content.

## Retention

Retention is separate from validity: `invalid_at` means the fact stopped being
true, `expires_at` means the row is garbage. Each scope has its own retention
window (`HARBORRAG_MEMORY_RETENTION_DAYS_*`, `0` disables expiry), applied when
the memory is written; expired memories are never recalled. Retention is a
floor, not a substitute for erasure -- the endpoints above are what a deletion
request is answered with.

## Configure memory

| Setting | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_MEMORY_ENABLED` | `true` | Master switch for the whole layer |
| `HARBORRAG_MEMORY_RECENT_MAX_MESSAGES` | `12` | Messages loaded for the verbatim window |
| `HARBORRAG_MEMORY_RECENT_MAX_TOKENS` | `2000` | Token ceiling for that window |
| `HARBORRAG_MEMORY_SUMMARY_TRIGGER_FRACTION` | `0.7` | Share of the ceiling that triggers a summary |
| `HARBORRAG_MEMORY_SUMMARY_KEEP_MESSAGES` | `8` | Messages kept verbatim after summarizing |
| `HARBORRAG_MEMORY_QUERY_REWRITE` | `true` | Condense history into the retrieval query |
| `HARBORRAG_MEMORY_RECALL_TOP_K` | `6` | Long-term memories injected per turn |
| `HARBORRAG_MEMORY_RECALL_SCOPES` | `user,project,session,tenant` | Scopes searched, in priority order |
| `HARBORRAG_MEMORY_RECALL_RECENCY_HALF_LIFE_HOURS` | `168` | Recency decay applied to recalled memories |
| `HARBORRAG_MEMORY_TYPE_AFFINITY_WEIGHT` | `0.5` | Boost for memories whose type the question asked for; `0` disables |
| `HARBORRAG_MEMORY_EXTRACTION_ENABLED` | `true` | Distil durable facts from finished exchanges |
| `HARBORRAG_MEMORY_EXTRACTION_MIN_IMPORTANCE` | `0.3` | Importance a candidate fact must reach to be stored |
| `HARBORRAG_MEMORY_DEDUP_THRESHOLD` | `0.92` | Cosine similarity above which a fact is a restatement |
| `HARBORRAG_MEMORY_BLOCK_BUDGET_FRACTION` | `0.15` | Share of the prompt budget the memory block may use |
| `HARBORRAG_MEMORY_ENTITY_LINKING` | `true` | Resolve mentions to graph node ids and anchor recall on them |
| `HARBORRAG_MEMORY_AGENT_TOOLS` | `false` | Expose `search_memory`/`manage_memory` to agent runs |
| `HARBORRAG_MEMORY_MODEL_PROFILE` | `memory` | Logical chat model for rewriting and summarizing |
| `HARBORRAG_MEMORY_EMBED_PROFILE` | unset | Embedding profile for memory search and dedup |
| `HARBORRAG_MEMORY_RETENTION_DAYS_SESSION` | `90` | Session-memory retention; `0` disables expiry |
| `HARBORRAG_MEMORY_RETENTION_DAYS_USER` | `365` | User-memory retention; `0` disables expiry |
| `HARBORRAG_MEMORY_RETENTION_DAYS_PROJECT` | `0` | Project-memory retention; `0` disables expiry |

`HARBORRAG_MEMORY_MODEL_PROFILE` names a logical model in the `chat` section of
`config/models.yaml`. Point it at a small, cheap, structured-output-capable
deployment: rewriting and summarizing run on every qualifying turn and do not
need the model that writes the answer. When the named profile is absent the
layer falls back to the default chat model, and when no catalog can be loaded
it skips rewriting and summarizing entirely. `config/models.example.yaml`
contains a ready `memory` profile to copy.

`HARBORRAG_MEMORY_EMBED_PROFILE` names a logical model in the `embed` section
of the same catalog; unset, memory search uses that section's `default_model`.
It does not have to match the ingestion embedding model -- memory vectors live
in their own collection -- but it must stay stable, because changing it
invalidates every vector already stored for recall.

Memory model and embedding calls inherit the `sensitive` flag, so their
responses are never cached and prompt text stays out of application logs.

## See also

- [Chat](README.md) - the HTTP and CLI surfaces
- [Chat models](models.md) - the model catalog and per-tenant overrides
- [Conversation memory](memory.md) - what a turn remembers and for how long
- [Agent](agent.md) - the bounded multi-turn surface
- [Limits and accounting](limits.md) - deadlines, admission control, usage
