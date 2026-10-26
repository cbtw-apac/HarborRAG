import assert from "node:assert/strict";
import test from "node:test";

import { createHarborClient, HarborApiRequestError, HarborChatStreamError } from "../src/index.ts";
import { parseSse } from "../src/sse.ts";

const encoder = new TextEncoder();
const completion = {
  id: "answer-1",
  session_id: "session-1",
  title: "Release policy",
  message: { role: "assistant", content: "Hello 🌊" },
  outcome: "answered",
  refusal_reason: null,
  usage: { prompt_tokens: 20, completion_tokens: 5, total_tokens: 25 },
  cost: { amount_usd: 0.001, currency: "USD", status: "estimated", complete: true,
    scope: "answer_generation", model_calls: 1, priced_model_calls: 1 },
  memory_persisted: true,
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

test("streamChat posts auth and custom headers and exposes the durable completion", async () => {
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
      assert.equal(init.headers.get("X-Request-Id"), "trace-1");
      assert.deepEqual(JSON.parse(init.body), { prompt: "hello", tenant: "ACME", stream: true });
      return sseResponse(byteStream(
        frame("response.started", { session_id: "session-1", mode: "rag", replayed: false }) +
        frame("response.output_text.delta", { content: "Hello 🌊" }) +
        frame("response.completed", completion),
      ));
    },
  });
  const events = await collect(api.streamChat({ prompt: "hello", tenant: "ACME" }, {
    signal: controller.signal, headers: { "X-Request-Id": "trace-1" },
  }));
  assert.equal(events[1].data.content, "Hello 🌊");
  assert.deepEqual(events.at(-1), { event: "response.completed", data: completion });
});

for (const [method, surface] of [["completeChat", "chat"], ["completeAgent", "agent"]]) {
  test(`${method} uses its own endpoint with stream false`, async () => {
    const api = createHarborClient({
      baseUrl: "https://example.test",
      fetchImpl: async (url, init) => {
        assert.equal(url, `https://example.test/v1/${surface}/completions`);
        assert.equal(JSON.parse(init.body).stream, false);
        assert.equal(init.headers.get("Accept"), "application/json");
        return Response.json(completion);
      },
    });
    assert.deepEqual(await api[method]({ prompt: "hello" }), completion);
  });
}

test("resumeAgent uses canonical JSON resume with auth, cancellation and evidence", async () => {
  const controller = new AbortController();
  const result = { ...completion, mode: "agent", run_id: "run-1", citations: [
    { chunk_id: "chunk-1", document_id: "doc-1", score: null, content: "Source\npassage" },
  ] };
  const api = createHarborClient({ baseUrl: "https://example.test/", getToken: () => "token",
    fetchImpl: async (url, init) => {
      assert.equal(url, "https://example.test/v1/agent/runs/run-1/resume");
      assert.deepEqual(JSON.parse(init.body), { session_id: "session-1", max_steps: 3 });
      assert.equal(init.headers.get("Authorization"), "Bearer token");
      assert.equal(init.headers.get("Accept"), "application/json");
      assert.equal(init.signal, controller.signal);
      return Response.json(result);
    },
  });
  assert.deepEqual(await api.resumeAgent("run-1", { session_id: "session-1", max_steps: 3 },
    { signal: controller.signal }), result);
});

test("evidence survives split-byte SSE and the final answer remains authoritative", async () => {
  const citations = [{ document_id: "doc-1", chunk_id: "chunk-1", score: null,
    content: "Original passage\nwith Unicode 🌊", marker: "[Source 1]", content_truncated: true }];
  const result = { ...completion, citations };
  const api = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async () =>
    sseResponse(byteStream(frame("retrieval.completed", {
      citations, session_id: "session-1", project_id: null,
    }) + frame("response.completed", result) + frame("response.error", { code: "too_late" }))),
  });
  const events = await collect(api.streamAgent({ prompt: "Question" }));
  assert.equal(events.length, 2);
  assert.deepEqual(events[0].data.citations, citations);
  assert.deepEqual(events[1].data, result);
});

test("scope refusal uses the normal completion shape in JSON and SSE", async () => {
  const refusal = { ...completion, outcome: "refused", refusal_reason: "out_of_scope",
    finish_reason: "out_of_scope", message: { role: "assistant", content: "Outside indexed knowledge" },
    citations: [], memory_persisted: false };
  const api = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async (_url, init) =>
    JSON.parse(init.body).stream
      ? sseResponse(byteStream(frame("response.started", {
          session_id: refusal.session_id, mode: "rag", replayed: false,
        }) + frame("response.completed", refusal)))
      : Response.json(refusal),
  });
  const json = await api.completeChat({ prompt: "Unrelated request" });
  const events = await collect(api.streamChat({ prompt: "Unrelated request" }));
  assert.deepEqual(json, refusal);
  assert.deepEqual(events.at(-1), { event: "response.completed", data: refusal });
});

test("streamChat preserves HTTP error envelopes and supports non-JSON proxy errors", async () => {
  const envelope = { error: { code: "forbidden", message: "Denied", details: {}, trace_id: "trace-1" } };
  const api = createHarborClient({
    baseUrl: "https://example.test",
    fetchImpl: async () => Response.json(envelope, { status: 403 }),
  });
  await assert.rejects(collect(api.streamChat({ prompt: "hello" })), (error) => {
    assert.ok(error instanceof HarborApiRequestError);
    assert.equal(error.status, 403);
    assert.deepEqual(error.envelope, envelope);
    return true;
  });
  const proxy = createHarborClient({ baseUrl: "https://example.test",
    fetchImpl: async () => new Response("Unavailable", { status: 502 }) });
  await assert.rejects(collect(proxy.streamChat({ prompt: "hello" })), (error) =>
    error instanceof HarborApiRequestError && error.status === 502);
});

test("terminal response.error throws and a truncated stream never reports success", async () => {
  const failed = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async () =>
    sseResponse(byteStream(frame("response.error", { code: "chat_stream_error", message: "Failed" }))) });
  await assert.rejects(collect(failed.streamChat({ prompt: "hello" })), (error) =>
    error instanceof HarborChatStreamError && error.code === "chat_stream_error");
  const truncated = createHarborClient({ baseUrl: "https://example.test", fetchImpl: async () =>
    sseResponse(byteStream(frame("response.output_text.delta", { content: "partial" }))) });
  await assert.rejects(collect(truncated.streamChat({ prompt: "hello" })), (error) =>
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
    sseResponse(JSON.parse(init.body).prompt === "first" ? first.body : second.body) });
  const firstAbort = new AbortController();
  const secondAbort = new AbortController();
  const a = api.streamChat({ prompt: "first" }, { signal: firstAbort.signal });
  const b = api.streamChat({ prompt: "second" }, { signal: secondAbort.signal });
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
  for await (const _event of api.streamChat({ prompt: "hello" })) break;
  assert.equal(cancelled, true);
});

test("operational helpers keep the existing api/v1 route prefix", async () => {
  const api = createHarborClient({ baseUrl: "https://example.test/", fetchImpl: async (url) => {
    assert.equal(url, "https://example.test/api/v1/health");
    return Response.json({ status: "ok" });
  } });
  assert.deepEqual(await api.get("/health"), { status: "ok" });
});
