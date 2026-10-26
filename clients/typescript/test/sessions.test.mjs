import assert from "node:assert/strict";
import test from "node:test";
import { createHarborClient, HarborApiRequestError } from "../src/index.ts";

test("session helpers use public surface paths, paging, auth and per-request cancellation", async () => {
  const calls = [];
  const controller = new AbortController();
  const api = createHarborClient({
    baseUrl: "https://example.test/",
    getToken: async () => "token",
    fetchImpl: async (url, init) => {
      calls.push({ url, method: init.method, body: init.body && JSON.parse(init.body) });
      assert.equal(init.headers.get("Authorization"), "Bearer token");
      assert.equal(init.headers.get("X-Trace"), "trace");
      assert.equal(init.signal, controller.signal);
      return Response.json({ sessions: [], messages: [] });
    },
  });
  const options = { signal: controller.signal, headers: { "X-Trace": "trace" } };
  await api.createSession("agent", "ACME", options);
  await api.listSessions("agent", { tenant: "ACME", cursor: "a+b", limit: 2 }, options);
  await api.getSessionHistory("chat", "session:1", { tenant: "ACME", after: "msg:1", limit: 1 }, options);
  await api.renameSession("agent", "session:1", "Title", "ACME", options);
  await api.deleteSession("agent", "session:1", "ACME", options);
  assert.deepEqual(calls.map(c => [c.method, c.url.replace("https://example.test", "")]), [
    ["POST", "/v1/agent/sessions"],
    ["GET", "/v1/agent/sessions?tenant=ACME&cursor=a%2Bb&limit=2"],
    ["GET", "/v1/chat/sessions/session%3A1/messages?tenant=ACME&after=msg%3A1&limit=1"],
    ["PATCH", "/v1/agent/sessions/session%3A1?tenant=ACME"],
    ["DELETE", "/v1/agent/sessions/session%3A1?tenant=ACME"],
  ]);
  assert.deepEqual(calls[0].body, { tenant: "ACME" });
  assert.deepEqual(calls[3].body, { title: "Title" });
  assert.equal(calls[1].body, undefined);
});

test("session helpers reject HTTP errors and avoid fetching aborted or blank-ID requests", async () => {
  let fetches = 0;
  const api = createHarborClient({
    baseUrl: "https://example.test",
    fetchImpl: async () => {
      fetches++;
      return Response.json({ error: { code: "harbor_not_found_error", message: "Not found" } }, { status: 404 });
    },
  });
  await assert.rejects(api.getSessionHistory("agent", "missing"), HarborApiRequestError);
  assert.throws(() => api.deleteSession("chat", " "), TypeError);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(api.listSessions("chat", {}, { signal: controller.signal }), { name: "AbortError" });
  assert.equal(fetches, 1);
});
