# @harborrag/api-client

Use this TypeScript client to call the HarborRAG HTTP API from your application. It includes
typed helpers for chat and sessions, a JSON fetch helper, and wire types generated from the
backend's OpenAPI contract. For the API's chat behavior, see the
[Chat guide](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/README.md).

## Install from a checkout

Use Node.js 22.15 or later and uv. From the HarborRAG root, generate the contract, build the
client, and pack it for your application:

```bash
uv sync --package harborrag-app --extra api
uv run --no-sync make openapi
cd clients/typescript
npm ci --ignore-scripts
npm run generate
npm pack
```

`npm pack` builds the package and prints the archive name. In your application, run
`npm install /absolute/path/to/the-archive.tgz` with the path to that file. The repository's
[API Contract workflow](https://github.com/cbtw-apac/HarborRAG/blob/main/.github/workflows/contract.yml)
also builds an `api-client` artifact for its runs.

## Create a client

Start the [local stack](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/quick-start.md)
or use the address of an existing HarborRAG API. This example checks the local API:

```ts
import { createHarborClient } from "@harborrag/api-client";

const api = createHarborClient({
  baseUrl: "http://127.0.0.1:8000",
  getToken: () => null,
});

const health = await api.get<{ status: string }>("/healthz");
console.log(health.status);
```

A running API returns `ok`. If your API uses authentication, provide a token through
`getToken`.

- **Client options:** `baseUrl` is the API origin, such as `http://localhost:8000`.
  `getToken` returns a bearer token, or `null` for a development backend without
  authentication. `fetchImpl` replaces the global `fetch`.
- **Request options:** the chat and session helpers take request options as their last
  argument: `headers`, an abort `signal`, and `tenant`, sent as `X-Tenant-Id`. Send
  `tenant` when the token holds several tenants.
- **Generic helpers:** `request(method, path, body?)` and its shortcuts `get`, `post`,
  `patch` and `delete` take the full route path, such as `/v1/admin/projects`, and no
  request options. They read JSON responses. Use a separate streaming request for
  ingestion events.
- **Errors:** every non-2xx response throws `HarborApiRequestError`, with the HTTP
  `status` and the standard `envelope`
  `{"error": {"code", "message", "details", "trace_id"}}`.
- **End user:** the server resolves the end user from a verified token claim, so provide
  a token for the current user on each request. A shared credential needs a stable
  signed end-user claim, named by `HARBORRAG_AUTH_USER_ID_CLAIM` (default `sub`);
  changing that claim changes which sessions the token sees.

## Ask a question

```ts
const result = await api.completeChat(
  { message: "Who approves the release?", mode: "classic" },
  { tenant: "ACME" },
);
console.log(result.answer, result.citations, result.usage);
```

`completeChat` sends `POST /v1/chat/completions` with `stream: false` and returns the
completion body. The request takes `message` and, optionally, `session_id`, `mode`,
`reasoning`, `project_ids`, `connections` and `filters`; `mode` and `reasoning` fall back
to the server defaults. Leave `session_id` out for a one-off question, or create a
session first (see [Chat sessions](#chat-sessions)) and pass its ID on every turn. The
fields and their limits are in
[Ask in a session](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/README.md#ask-in-a-session),
and the answer, its `[n]` citations and its `usage` in
[Read the answer](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/README.md#read-the-answer).
A citation's `snippet` is source text: render it as text, not raw HTML.

### Retry a turn safely

Pass `idempotencyKey` in the request options; it is sent as `Idempotency-Key`. A retry
with the same key and body within 24 hours returns the stored answer without a second
model call, even when `stream` differs. Use a new key for each new turn; when a key must
be replaced after a failure is in
[Retry without duplicates](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/limits.md#retry-without-duplicates).

## Stream an answer

```ts
const controller = new AbortController();
let answer = "";

try {
  for await (const event of api.streamChat(
    { message: "Explain our release policy" },
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

`streamChat` sends the same request with `stream: true` and yields `ChatStreamEvent`
values, decoding UTF-8 across arbitrary network chunks. To stream a turn in a session,
create one first and pass its `session_id` from [Chat sessions](#chat-sessions).

| Event | Data |
| --- | --- |
| `response.started` | `session_id`, `mode`, and `replayed`, which is `true` for an idempotent retry |
| `response.output_text.delta` | `content`; the server currently sends the whole answer in one delta |
| `response.completed` | the same body `completeChat` returns |

The server side of the stream is in
[Stream the answer](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/README.md#stream-the-answer).

- **Unknown events** are skipped, and the generator ends at `response.completed`.
- **Errors before the stream starts** throw `HarborApiRequestError`.
- **A broken stream** throws `HarborChatStreamError` with one of these codes:
  - `invalid_content_type`: the response is not `text/event-stream`;
  - `missing_body`: the response has no body;
  - `invalid_event`: an event is not a JSON object, or the completion has no answer;
  - `stream_incomplete`: the stream closed before `response.completed`;
  - `event_too_large`: one event exceeded the client's size limit (2 Mi characters).
- **Breaking out of the loop** cancels that response body.

Requests are independent. Each call owns its parser, body reader and request headers, so
give each request you may want to cancel on its own its own `AbortController`.

## Chat sessions

```ts
const session = await api.createSession(
  { title: "Release policy", project_id: "demo" },
  { tenant: "ACME" },
);
const page = await api.listSessions({ limit: 20, q: "release" }, { tenant: "ACME" });
const history = await api.getSessionHistory(session.session_id, { limit: 50 }, { tenant: "ACME" });
```

- **`createSession`:** `title` and `project_id` are optional. The project is fixed for
  the life of the session, and retrieval reads only its connections.
- **Pages:** `listSessions` returns the caller's sessions, newest activity first;
  `getSessionHistory` returns a session's messages, newest first. Both answer
  `{"items", "next_cursor"}`; pass `next_cursor` as `cursor` for the next page, and stop
  when it is `null`. `limit` is 1-100, default 20.
- **Search:** `listSessions` takes `q` to search session titles.
- **Ownership:** a session that is not the caller's answers `404`, the same as one that
  does not exist.

The session routes are described in
[Manage sessions](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/chat/README.md#manage-sessions).

## Regenerate the wire types

Repeat the generation steps under [Install from a checkout](#install-from-a-checkout).
Use `npm run build` when you only need the compiled client. `openapi.json`,
`src/schema.d.ts`, and `dist/` are generated and git-ignored.

CI (`.github/workflows/contract.yml`) runs on pull requests to, and pushes on, `main` and
`dev/v2.0.0`. It exports the contract, runs `npm audit --audit-level=moderate`, generates
the types, and builds and tests the client. On a pull request it gates the merge on
`oasdiff breaking` against the target branch; the `contract-break-approved` label
overrides the gate and requires lead review.

## Test the client

```bash
npm test
```

Run the regeneration steps first in a fresh checkout. The parser and request tests make
no network calls; they use Node 22.15+ native type stripping and the built-in test
runner, and cover split UTF-8 and CRLF framing, truncated and invalid streams,
cancellation, and independent concurrent requests. The package test packs a temporary
npm archive and type-checks its exported declarations.

### Dependency overrides

`package.json` overrides two transitive dependencies to patched releases, `minimatch` and
`js-yaml`. The `minimatch` override removes GHSA-g3hv-75wg-m249: `openapi-typescript` 7
still depends on `@redocly/openapi-core` 1.x, which pins a vulnerable `minimatch`.
Generation from the local `openapi.json` is unaffected, and the generated types are
identical. The one redocly path that calls `minimatch`, header matching for remote `$ref`
URLs, does not work under the override, so keep the generator on local files. Drop the
override once `openapi-typescript` moves to `@redocly/openapi-core` 2.
