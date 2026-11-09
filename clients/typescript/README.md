# @harborrag/api-client

TypeScript client for the HarborRAG HTTP API. It gives you typed helpers for
chat completions (JSON and SSE) and chat sessions, a small typed fetch for every
other route, and the wire types (`paths`, `components`, `operations`) generated
from the backend's `openapi.json`. The backend owns that contract; the build
copies the generated declarations into the published package.

## Create a client

```ts
import { createHarborClient } from "@harborrag/api-client";

const api = createHarborClient({
  baseUrl: import.meta.env.VITE_API_BASE_URL,
  getToken: () => localStorage.getItem("harbor_token"),
});

const health = await api.get<{ status: string }>("/healthz");
```

- **Request options:** the chat and session helpers take request options as
  their last argument: `headers`, an abort `signal`, and `tenant`, sent as
  `X-Tenant-Id`. Send `tenant` when the token holds several tenants.
- **Generic helpers:** `get`, `post`, `patch` and `delete` take the full route
  path, such as `/v1/admin/projects`, and no request options.
- **Errors:** every non-2xx response throws `HarborApiRequestError`, with the
  HTTP `status` and the standard `envelope`
  `{"error": {"code", "message", "details", "trace_id"}}`.
- **End user:** the server resolves the end user from a verified token claim.
  Provide a token for the current user on each request. A shared credential
  needs a stable signed end-user claim, named by `HARBORRAG_AUTH_USER_ID_CLAIM`
  (default `sub`); changing that claim changes which sessions the token sees.

## Ask a question

```ts
const result = await api.completeChat(
  { message: "Who approves the release?", mode: "classic" },
  { tenant: "ACME" },
);
console.log(result.answer, result.citations, result.usage);
```

`completeChat` sends `POST /v1/chat/completions` with `stream: false`.

- **`mode`:** `classic` (the default) or `instant`. `instant` skips the extra
  model call and the graph lookup that `classic` makes. `advanced` and `auto`
  are refused with `422 mode_unavailable` until v2.1.0.
- **`reasoning`:** only `off` (the default) is accepted until v2.1.0; any other
  value is refused with `422 validation_error`.
- **`session_id`:** leave it out for a one-off question, whose answer has
  `session_id: null`. To keep a conversation, create a session first (see
  [Chat sessions](#chat-sessions)) and pass its ID on every turn.
- **Scope:** `project_ids` (one-off questions only; a session reads its own
  project), `connections` and `filters` narrow what retrieval reads.

The answer has `completion_id`, `session_id`, `trace_id`, `mode`, `answer`,
`citations` and `usage`. Each citation has `n`, the `[n]` marker in the answer,
plus `chunk_id`, `document_id`, `title`, `snippet`, `url` and `page`. `snippet`
is source text: render it as text, not raw HTML. `usage` reports `mode`,
`reasoning`, `steps`, the token counts and `cost_usd`.

### Retry a turn safely

Pass `idempotencyKey` in the request options; it is sent as `Idempotency-Key`.
A retry with the same key and the same body within 24 h returns the stored
answer without a second model call, even when `stream` differs. The same key
with a different body is refused with `422 validation_error` on the
`Idempotency-Key` field. Use a new key for each new turn, and after a request
that failed.

## Stream an answer

```ts
const controller = new AbortController();
let answer = "";

try {
  for await (const event of api.streamChat(
    { message: "Explain our release policy", session_id: sessionId },
    { signal: controller.signal, tenant: "ACME", idempotencyKey: crypto.randomUUID() },
  )) {
    if (event.event === "response.output_text.delta") {
      answer += event.data.content;
    } else if (event.event === "response.completed") {
      answer = event.data.answer;
      console.log(event.data.citations, event.data.usage);
    }
  }
} catch (error) {
  if (!controller.signal.aborted) throw error;
}

// A Stop button can call controller.abort() while the request is running.
```

`streamChat` sends the same request with `stream: true` and yields these events,
decoding UTF-8 across arbitrary network chunks:

| Event | Data |
| --- | --- |
| `response.started` | `session_id`, `mode`, and `replayed`, which is `true` for an idempotent retry |
| `response.output_text.delta` | `content`: the server currently sends the whole answer in one delta |
| `response.completed` | the same body `completeChat` returns |

- **Unknown events** are skipped, and the generator ends at
  `response.completed`.
- **Errors before the stream starts** throw `HarborApiRequestError`.
- **A broken stream** throws `HarborChatStreamError` with one of these codes:
  - `invalid_content_type`: the response is not `text/event-stream`;
  - `missing_body`: the response has no body;
  - `invalid_event`: an event is not a JSON object, or the completion has no
    answer;
  - `stream_incomplete`: the stream closed before `response.completed`.
- **Breaking out of the loop** cancels that response body.

Requests are independent. Each call owns its parser, body reader and request
headers, so give each request you may cancel on its own its own
`AbortController`.

## Chat sessions

```ts
const session = await api.createSession(
  { title: "Release policy", project_id: "demo" },
  { tenant: "ACME" },
);
const page = await api.listSessions({ limit: 20, q: "release" }, { tenant: "ACME" });
const history = await api.getSessionHistory(session.session_id, { limit: 50 }, { tenant: "ACME" });
```

- **`createSession`:** `title` and `project_id` are optional. The project is
  fixed for the life of the session, and retrieval reads only its connections.
- **Pages:** `listSessions` returns the caller's sessions, newest activity
  first; `getSessionHistory` returns a session's messages, newest first. Both
  answer `{"items", "next_cursor"}`; pass `next_cursor` as `cursor` for the
  next page, and stop when it is `null`. `limit` is 1–100, default 20.
- **Search:** `listSessions` takes `q` to search session titles.
- **Ownership:** a session that is not the caller's answers `404`, the same as
  one that does not exist.

## Regenerate the wire types

```bash
uv run make openapi          # repo root: writes openapi.json
cd clients/typescript
npm ci --ignore-scripts
npm run generate             # writes src/schema.d.ts
npm run build
```

`openapi.json`, `src/schema.d.ts` and `dist/` are generated and git-ignored.

CI (`.github/workflows/contract.yml`) exports the contract, generates the types,
builds and tests the client on every pull request, and gates merges on
`oasdiff breaking` against the target branch. The `contract-break-approved`
label overrides the gate and requires lead review.

`package.json` overrides `minimatch` to a release without GHSA-g3hv-75wg-m249:
`openapi-typescript` 7 still depends on `@redocly/openapi-core` 1.x, which pins
a vulnerable `minimatch`. Generation from the local `openapi.json` is
unaffected, and the generated types are identical. The one redocly path that
calls `minimatch`, header matching for remote `$ref` URLs, does not work under
the override, so keep the generator on local files. Drop the override once
`openapi-typescript` moves to `@redocly/openapi-core` 2.

## Test the client

```bash
npm test
```

Run the regeneration steps first in a fresh checkout. The parser and request
tests make no network calls; they use Node 22.15+ native type stripping and the
built-in test runner, and cover split UTF-8 and CRLF framing, truncated and
invalid streams, cancellation, and independent concurrent requests. The package
test packs a temporary npm archive and type-checks its exported declarations.
