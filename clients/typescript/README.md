# @harborrag/api-client

TypeScript client for the HarborRAG API. The backend owns the contract:
generated wire types come from `openapi.json` (exported by `make openapi`).
The runtime includes small typed chat helpers for the JSON and SSE contracts,
alongside the existing operational API methods.
The package exports the generated `paths`, `components`, and `operations` types;
building copies their declaration into the published package.

## Regenerate

```bash
make openapi                 # repo root: writes openapi.json
cd clients/typescript
npm install
npm run generate             # writes src/schema.d.ts
npm run build
```

CI (`.github/workflows/contract.yml`) does this on every PR and gates merges
on `oasdiff breaking` against the target branch (`contract-break-approved`
label overrides, requires lead review).

## Usage (WebUI)

```ts
import { createHarborClient } from "@harborrag/api-client";

const api = createHarborClient({
  baseUrl: import.meta.env.VITE_API_BASE_URL,
  getToken: () => localStorage.getItem("harbor_token"),
});

const health = await api.get<{ status: string; version: string }>("/health");
```

All non-2xx responses throw `HarborApiRequestError` carrying the standard
envelope `{error: {code, message, details, trace_id}}`.

## Chat with streaming

`streamChat` sends an authenticated `POST /v1/chat/completions` with `stream: true`.
It yields named SSE events while decoding UTF-8 across arbitrary network chunks.
The terminal `response.completed` event contains the same response as `completeChat`,
including the session ID, title, citations, token usage, and generation cost.

```ts
const controller = new AbortController();
let answer = "";
let sessionId: string | undefined;

try {
  for await (const event of api.streamChat(
    { tenant: "ACME", prompt: "Explain our release policy", idempotency_key: crypto.randomUUID() },
    { signal: controller.signal, headers: { "X-Request-Id": crypto.randomUUID() } },
  )) {
    if (event.event === "response.started") {
      sessionId = event.data.session_id;
    } else if (event.event === "response.output_text.delta") {
      answer += event.data.content;
      console.log(answer);
    } else if (event.event === "response.completed") {
      // This final value also handles agent responses and completed-request replays.
      answer = event.data.message.content;
      sessionId = event.data.session_id;
      console.log(event.data.title, event.data.usage, event.data.cost);
    }
  }
} catch (error) {
  if (!controller.signal.aborted) throw error;
}

// A Stop button can call controller.abort() while the request is running.
```

`response.error` throws `HarborChatStreamError` with the server's error code.
A connection that closes without a terminal completion throws `stream_incomplete`.
Breaking out of the loop releases and cancels that response body.

The same client can serve multiple concurrent requests. Each call owns its parser,
body reader, and request headers. Give each independently cancellable request its own
`AbortController`, and use separate session IDs for separate conversations. Preserve
the same `idempotency_key` and original request body when retrying the same turn
(the `stream` flag may change); use a new key for a new turn. If the original request
omitted `session_id`, keep it omitted for that retry, even after receiving the new ID.

`cost.scope` is `answer_generation`: it excludes retrieval, request-scope classification,
and background memory work. HTTP admission adds one small model call per fresh request;
its usage is recorded separately as `query_scope_gate`, including rejected requests.
An unknown price has `amount_usd: null`; a known subtotal can have `complete: false`.

## Chat without streaming

```ts
const result = await api.completeChat({
  tenant: "ACME",
  prompt: "Who approves the release?",
});
const next = await api.completeChat({
  tenant: "ACME", session_id: result.session_id,
  prompt: "What evidence supports that?",
});
console.log(next.message.content, next.citations, next.cost);
```

The generic `get`, `post`, `patch`, and `delete` helpers retain their `/api/v1` prefix.
Chat helpers use `/v1/chat/completions` directly.
The server resolves the end user from a verified token claim. Provide a token
for the current user on each request; a shared credential needs a stable signed
end-user claim configured with `HARBORRAG_AUTH_USER_ID_CLAIM`. Changing that
claim changes which sessions the token can see.

## Agent resume and evidence

```ts
const resumed = await api.resumeAgent("run-...", {
  tenant: "ACME", session_id: "session-...", max_steps: 4,
});
for (const citation of resumed.citations) {
  console.log(citation.document_title, citation.content, citation.content_truncated);
}
```

`resumeAgent` posts to `/v1/agent/runs/{run_id}/resume` and accepts the same per-request
headers and abort signal as chat. It cannot change the run's prompt/model or stream.
For new agent runs use `completeAgent` / `streamAgent`; both post to
`/v1/agent/completions` and need only `prompt`. Both streaming helpers use the
same event parser and cancellation behavior.
Citation content is bounded, authorized source text; render it as text, not raw HTML.
During SSE, candidate citations arrive in `retrieval.completed`; replace them with the
final answer's citations rather than assuming every candidate was used. Agent text
arrives at completion, with progress events beforehand. HTTP scope rejections are
`HarborApiRequestError` (`422`, `details.reason: out_of_scope`), not SSE events.

## Session history

```ts
const page = await api.listSessions("chat", { tenant: "ACME", limit: 20 });
const agentPage = await api.listSessions("agent", { tenant: "ACME" });
const history = await api.getSessionHistory("chat", sessionId, {
  tenant: "ACME", limit: 50,
});
// Continue with { after: history.next_cursor } when present.
await api.renameSession("chat", sessionId, "Release policy", "ACME");
// Explicit creation is optional; completions can create sessions automatically.
const session = await api.createSession("agent", "ACME");
```

Lists contain `sessions` and optional `next_cursor`; history contains `messages`
and optional `next_cursor`. Pass a list cursor as `cursor`, a history cursor as
`after`. `deleteSession(surface, sessionId, tenant)` erases history and associated
memory. Every helper accepts headers and an abort signal as its final options
argument. Lists are filtered by creating surface; session IDs remain shared.

The conversation routes and generic `/v1/runs/*` routes are removed. Session helpers
use `/v1/{chat|agent}/sessions`; they do not use the operational `/api/v1` prefix.

## Test

```bash
npm test
npm run build
```

Run the regeneration steps first in a fresh checkout. The tests also build and
pack a temporary npm archive and typecheck its exported declarations.

The no-network parser and request tests use Node 22.15+ native type stripping and
the built-in test runner. They cover split UTF-8 and CRLF, terminal errors, truncated
streams, cancellation, and independent concurrent requests. Building uses the locked
TypeScript development dependency.
