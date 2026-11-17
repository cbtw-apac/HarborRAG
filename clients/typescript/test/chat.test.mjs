import assert from "node:assert/strict";
import test from "node:test";

import { createHarborClient, HarborApiRequestError, HarborChatStreamError } from "../src/index.ts";
import { parseSse } from "../src/sse.ts";

const encoder = new TextEncoder();
const completion = {
  completion_id: "message-2",
  session_id: "session-1",
  trace_id: "0123456789abcdef0123456789abcdef",
  mode: "classic",
  answer: "Hello 🌊",
  citations: [],
  usage: { mode: "classic", reasoning: "off", steps: 1, prompt_tokens: 20, completion_tokens: 5,
    reasoning_tokens: 0, cost_usd: 0.001 },
};

function frame(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function byteStream(text) {
  return new ReadableStream({
    start(controller) {
      // A byte at a time splits both UTF-8 code points and every framing separator.
      for (const byte of encoder.encode(text)) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
}

function sseResponse(body) {
  return new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

async function collect(events) {
  const result = [];
  for await (const event of events) result.push(event);
  return result;
}

test("SSE parser handles split UTF-8, CRLF, comments and multiline data", async () => {
  const text = ': heartbeat\r\nevent: custom\r\ndata: {"text":\r\ndata: "🌊"}\r\n\r\n' +
    'event: second\rdata: {}\r\r';
  assert.deepEqual(await collect(parseSse(byteStream(text))), [
    { event: "custom", data: '{"text":\n"🌊"}' },
    { event: "second", data: "{}" },
  ]);
});

test("streamChat posts auth, tenant, idempotency and custom headers and exposes the completion", async () => {
  const controller = new AbortController();
  const api = createHarborClient({
    baseUrl: "https://example.test/",
    getToken: async () => "test-token",
    fetchImpl: async (url, init) => {
      assert.equal(url, "https://example.test/v1/chat/completions");
      assert.equal(init.method, "POST");
      assert.equal(init.signal, controller.signal);
      assert.equal(init.headers.get("Authorization"), "Bearer test-token");
      assert.equal(init.headers.get("Accept"), "text/event-stream");
      assert.equal(init.headers.get("X-Trace-Id"), "trace-1");
      assert.equal(init.headers.get("X-Tenant-Id"), "ACME");
      assert.equal(init.headers.get("Idempotency-Key"), "turn-1");
      assert.deepEqual(JSON.parse(init.body), { message: "hello", session_id: "session-1", stream: true });
      return sseResponse(byteStream(
        frame("response.started", { session_id: "session-1", mode: "classic", replayed: false }) +
        frame("response.output_text.delta", { content: "Hello 🌊" }) +
        frame("response.completed", completion),
      ));
    },
  });
  const events = await collect(api.streamChat({ message: "hello", session_id: "session-1" }, {
    signal: controller.signal, headers: { "X-Trace-Id": "trace-1" }, tenant: "ACME", idempotencyKey: "turn-1",
  }));
  assert.equal(events[1].data.content, "Hello 🌊");
  assert.deepEqual(events.at(-1), { event: "response.completed", data: completion });
});

test("completeChat posts to the chat endpoint with stream false", async () => {
  const api = createHarborClient({
    baseUrl: "https://example.test",
    fetchImpl: async (url, init) => {
      assert.equal(url, "https://example.test/v1/chat/completions");
      assert.deepEqual(JSON.parse(init.body), { message: "hello", mode: "instant", stream: false });
      assert.equal(init.headers.get("Accept"), "application/json");
      assert.equal(init.headers.get("X-Tenant-Id"), null);
      assert.equal(init.headers.get("Idempotency-Key"), null);
      return Response.json(completion);
    },
  });
  assert.deepEqual(await api.completeChat({ message: "hello", mode: "instant" }), completion);
});

test("citations survive split-byte SSE, unknown events are skipped and the stream ends at completion", async () => {
  const citations = [{ n: 1, chunk_id: "chunk-1", document_id: "doc-1", title: "Release policy",
    snippet: "Original passage\nwith Unicode 🌊", url: null, page: null }];
  const result = { ...completion, citations };
  const api = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async () =>
    sseResponse(byteStream(frame("retrieval.results", { citations }) +
      frame("response.completed", result) + frame("response.output_text.delta", { content: "late" }))),
  });
  const events = await collect(api.streamChat({ message: "Question" }));
  assert.equal(events.length, 1);
  assert.deepEqual(events[0], { event: "response.completed", data: result });
});

test("a one-off question has a null session_id in JSON and SSE", async () => {
  const oneOff = { ...completion, session_id: null };
  const api = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async (_url, init) =>
    JSON.parse(init.body).stream
      ? sseResponse(byteStream(frame("response.started", {
          session_id: null, mode: "classic", replayed: false,
        }) + frame("response.completed", oneOff)))
      : Response.json(oneOff),
  });
  const json = await api.completeChat({ message: "One question" });
  const events = await collect(api.streamChat({ message: "One question" }));
  assert.deepEqual(json, oneOff);
  assert.equal(events[0].data.session_id, null);
  assert.deepEqual(events.at(-1), { event: "response.completed", data: oneOff });
});

test("streamChat preserves HTTP error envelopes and supports non-JSON proxy errors", async () => {
  const envelope = { error: { code: "forbidden", message: "Denied", details: {}, trace_id: "trace-1" } };
  const api = createHarborClient({
    baseUrl: "https://example.test",
    fetchImpl: async () => Response.json(envelope, { status: 403 }),
  });
  await assert.rejects(collect(api.streamChat({ message: "hello" })), (error) => {
    assert.ok(error instanceof HarborApiRequestError);
    assert.equal(error.status, 403);
    assert.deepEqual(error.envelope, envelope);
    return true;
  });
  const proxy = createHarborClient({ baseUrl: "https://example.test",
    fetchImpl: async () => new Response("Unavailable", { status: 502 }) });
  await assert.rejects(collect(proxy.streamChat({ message: "hello" })), (error) =>
    error instanceof HarborApiRequestError && error.status === 502);
});

test("a completion without its answer is invalid and a truncated stream never reports success", async () => {
  const { answer: _answer, ...unanswered } = completion;
  const invalid = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async () =>
    sseResponse(byteStream(frame("response.completed", unanswered))) });
  await assert.rejects(collect(invalid.streamChat({ message: "hello" })), (error) =>
    error instanceof HarborChatStreamError && error.code === "invalid_event");
  const truncated = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async () =>
    sseResponse(byteStream(frame("response.output_text.delta", { content: "partial" }))) });
  await assert.rejects(collect(truncated.streamChat({ message: "hello" })), (error) =>
    error instanceof HarborChatStreamError && error.code === "stream_incomplete");
});

test("two interleaved requests keep independent buffers and cancellation", async () => {
  function controlled() {
    let writer;
    let cancelled = 0;
    const body = new ReadableStream({
      start(controller) { writer = controller; },
      cancel() { cancelled += 1; },
    });
    return { body, push: (event, data) => writer.enqueue(encoder.encode(frame(event, data))),
      get cancelled() { return cancelled; } };
  }
  const first = controlled();
  const second = controlled();
  const api = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async (_url, init) =>
    sseResponse(JSON.parse(init.body).message === "first" ? first.body : second.body) });
  const firstAbort = new AbortController();
  const secondAbort = new AbortController();
  const a = api.streamChat({ message: "first" }, { signal: firstAbort.signal });
  const b = api.streamChat({ message: "second" }, { signal: secondAbort.signal });
  first.push("response.output_text.delta", { content: "one" });
  second.push("response.output_text.delta", { content: "two" });
  const [one, two] = await Promise.all([a.next(), b.next()]);
  assert.equal(one.value.data.content, "one");
  assert.equal(two.value.data.content, "two");
  const pending = a.next();
  firstAbort.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(first.cancelled, 1);
  assert.equal(second.cancelled, 0);
  second.push("response.completed", { ...completion, session_id: "session-2" });
  assert.equal((await b.next()).value.data.session_id, "session-2");
  assert.equal((await b.next()).done, true);
  assert.equal(secondAbort.signal.aborted, false);
});

test("breaking iteration cancels the response body", async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(controller) { controller.enqueue(encoder.encode(frame("response.output_text.delta", { content: "one" }))); },
    cancel() { cancelled = true; },
  });
  const api = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async () => sseResponse(body) });
  for await (const _event of api.streamChat({ message: "hello" })) break;
  assert.equal(cancelled, true);
});

test("generic helpers send the full route path", async () => {
  const api = createHarborClient({ baseUrl: "https://example.test/", fetchImpl: async (url) => {
    assert.equal(url, "https://example.test/healthz");
    return Response.json({ status: "ok" });
  } });
  assert.deepEqual(await api.get("/healthz"), { status: "ok" });
});
