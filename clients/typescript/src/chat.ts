import { apiRequestError, HarborChatStreamError } from "./errors.js";
import type { HarborClientOptions, HarborRequestOptions } from "./options.js";
import type { components } from "./schema.js";
import { parseSse } from "./sse.js";

type Schemas = components["schemas"];
type CompletionBody = Schemas["ChatCompletionRequest"];

/** The completion body without ``stream``; ``mode`` and ``reasoning`` fall back to the server defaults. */
export type ChatCompletionRequest =
  Omit<CompletionBody, "stream" | "mode" | "reasoning"> &
  Partial<Pick<CompletionBody, "mode" | "reasoning">>;
export type ChatCompletionResponse = Schemas["ChatCompletionResponse"];
export type ChatCitation = Schemas["Citation"];
export type ChatUsage = Schemas["Usage"];

export interface ChatRequestOptions extends HarborRequestOptions {
  /** Sent as Idempotency-Key: a retry with the same key and body within 24 h returns the stored result. */
  idempotencyKey?: string;
}

export type ChatStreamEvent =
  | { event: "response.started"; data: { session_id: string | null; mode: string; replayed: boolean } }
  | { event: "response.output_text.delta"; data: { content: string } }
  | { event: "response.completed"; data: ChatCompletionResponse };

const PROGRESS_EVENTS = new Set(["response.started", "response.output_text.delta"]);

/** Per-request chat methods; readers, buffers and cancellation are never shared between calls. */
export function createChatMethods(options: HarborClientOptions) {
  const doFetch = options.fetchImpl ?? fetch;

  async function fetchCompletion(
    path: string,
    body: ChatCompletionRequest & { stream: boolean },
    stream: boolean,
    requestOptions: ChatRequestOptions,
  ): Promise<Response> {
    requestOptions.signal?.throwIfAborted();
    const headers = new Headers(requestOptions.headers);
    headers.set("Accept", stream ? "text/event-stream" : "application/json");
    headers.set("Content-Type", "application/json");
    if (requestOptions.tenant) headers.set("X-Tenant-Id", requestOptions.tenant);
    if (requestOptions.idempotencyKey) headers.set("Idempotency-Key", requestOptions.idempotencyKey);
    const token = await options.getToken?.();
    requestOptions.signal?.throwIfAborted();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await doFetch(`${options.baseUrl.replace(/\/+$/, "")}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: requestOptions.signal,
    });
    if (!response.ok) throw await apiRequestError(response);
    return response;
  }

  async function completeChat(
    request: ChatCompletionRequest,
    requestOptions: ChatRequestOptions = {},
  ): Promise<ChatCompletionResponse> {
    return (await (await fetchCompletion(
      "/v1/chat/completions", { ...request, stream: false }, false, requestOptions,
    )).json()) as ChatCompletionResponse;
  }

  async function* streamCompletion(
    path: string,
    request: ChatCompletionRequest,
    requestOptions: ChatRequestOptions = {},
  ): AsyncGenerator<ChatStreamEvent, void, undefined> {
    const response = await fetchCompletion(
      path, { ...request, stream: true }, true, requestOptions,
    );
    if (!response.headers.get("content-type")?.toLowerCase().startsWith("text/event-stream")) {
      await response.body?.cancel();
      throw new HarborChatStreamError("invalid_content_type", "Expected a text/event-stream response");
    }
    if (response.body === null) {
      throw new HarborChatStreamError("missing_body", "The response has no event stream");
    }
    for await (const frame of parseSse(response.body, requestOptions.signal)) {
      if (!PROGRESS_EVENTS.has(frame.event) && frame.event !== "response.completed") continue;
      let data: Record<string, unknown>;
      try {
        const parsed: unknown = JSON.parse(frame.data);
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
        data = parsed as Record<string, unknown>;
      } catch {
        throw new HarborChatStreamError("invalid_event", "The server returned an invalid JSON event");
      }
      if (frame.event === "response.completed") {
        if (typeof data.completion_id !== "string" || typeof data.answer !== "string") {
          throw new HarborChatStreamError("invalid_event", "The completion event is missing its answer");
        }
        yield { event: frame.event, data: data as unknown as ChatCompletionResponse };
        return;
      }
      if (PROGRESS_EVENTS.has(frame.event)) {
        yield { event: frame.event, data } as ChatStreamEvent;
      }
    }
    throw new HarborChatStreamError("stream_incomplete", "The stream ended before response.completed");
  }

  function streamChat(request: ChatCompletionRequest, requestOptions: ChatRequestOptions = {}) {
    return streamCompletion("/v1/chat/completions", request, requestOptions);
  }

  return { completeChat, streamChat };
}
