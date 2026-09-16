import { apiRequestError, HarborChatStreamError } from "./errors.js";
import type { HarborClientOptions, HarborRequestOptions } from "./options.js";
import { parseSse } from "./sse.js";

export interface ChatCompletionRequest {
  prompt: string;
  tenant?: string;
  session_id?: string;
  mode?: "rag";
  model?: string;
  project_id?: string;
  graph_search?: boolean;
  max_steps?: number;
  idempotency_key?: string;
}

export interface AgentCompletionRequest extends Omit<ChatCompletionRequest, "mode"> {
  mode?: "agent";
}

export interface ChatUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cache_read_input_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
  reasoning_tokens?: number | null;
}

export interface ChatCost {
  amount_usd: number | null;
  currency: "USD";
  status: "estimated" | "unavailable";
  complete: boolean;
  scope: "answer_generation";
  model_calls: number;
  priced_model_calls: number;
}

export interface ChatCitation {
  document_id: string;
  chunk_id: string;
  score: number | null;
  tool?: string | null;
  document_title?: string | null;
  section_path?: string[];
  location?: string | null;
  marker?: string | null;
  content?: string | null;
  content_truncated?: boolean;
}

export interface ChatCompletionResponse {
  id: string;
  created: number | null;
  model: string;
  provider: string;
  provider_model: string;
  message: { role: "assistant"; content: string };
  finish_reason: string;
  usage: ChatUsage;
  cost: ChatCost;
  latency_ms: number | null;
  retry_count: number;
  fallback_count: number;
  citations: ChatCitation[];
  citation_validation?: {
    complete: boolean;
    evidence_available: boolean;
    marker_count: number;
    validated_count: number;
    invalid_count: number;
  } | null;
  session_id: string;
  title: string | null;
  mode: "rag" | "agent";
  run_id: string | null;
  stop_reason: string | null;
  turns: number | null;
  tool_call_count: number | null;
  tool_calls: { step: number; tool: string; ok: boolean }[] | null;
  project_id: string | null;
  memory_persisted: boolean;
}

export interface AgentResumeRequest {
  session_id: string;
  tenant?: string;
  project_id?: string;
  graph_search?: boolean;
  max_steps?: number;
}

export interface AgentCompletionResponse extends ChatCompletionResponse {
  mode: "agent";
  run_id: string;
  stop_reason: string;
  turns: number;
  tool_call_count: number;
  tool_calls: { step: number; tool: string; ok: boolean }[];
}

export type ChatStreamEvent =
  | { event: "response.started"; data: { session_id: string; mode: "rag" | "agent"; replayed: boolean } }
  | { event: "response.output_text.delta"; data: { content: string; [key: string]: unknown } }
  | { event: "response.completed"; data: ChatCompletionResponse }
  | {
      event: "retrieval.completed" | "response.citations";
      data: { citations: ChatCitation[]; session_id: string; project_id: string | null };
    }
  | {
      event: "response.agent.progress" | "response.warning";
      data: Record<string, unknown>;
    };

const PROGRESS_EVENTS = new Set([
  "response.started",
  "response.output_text.delta",
  "retrieval.completed",
  "response.citations",
  "response.agent.progress",
  "response.warning",
]);

/** Per-request chat methods; readers, buffers and cancellation are never shared between calls. */
export function createChatMethods(options: HarborClientOptions) {
  const doFetch = options.fetchImpl ?? fetch;

  async function fetchCompletion(
    path: string,
    body: ((ChatCompletionRequest | AgentCompletionRequest) & { stream: boolean }) | AgentResumeRequest,
    stream: boolean,
    requestOptions: HarborRequestOptions,
  ): Promise<Response> {
    requestOptions.signal?.throwIfAborted();
    const headers = new Headers(requestOptions.headers);
    headers.set("Accept", stream ? "text/event-stream" : "application/json");
    headers.set("Content-Type", "application/json");
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
    requestOptions: HarborRequestOptions = {},
  ): Promise<ChatCompletionResponse> {
    return (await (await fetchCompletion(
      "/v1/chat/completions", { ...request, stream: false }, false, requestOptions,
    )).json()) as ChatCompletionResponse;
  }

  async function completeAgent(
    request: AgentCompletionRequest,
    requestOptions: HarborRequestOptions = {},
  ): Promise<AgentCompletionResponse> {
    return (await (await fetchCompletion(
      "/v1/agent/completions", { ...request, stream: false }, false, requestOptions,
    )).json()) as AgentCompletionResponse;
  }

  /** Resume an unfinished run, retaining its original prompt and model. JSON only. */
  async function resumeAgent(
    runId: string,
    request: AgentResumeRequest,
    requestOptions: HarborRequestOptions = {},
  ): Promise<AgentCompletionResponse> {
    if (!runId.trim()) throw new TypeError("runId must not be blank");
    return (await (await fetchCompletion(
      `/v1/agent/runs/${encodeURIComponent(runId)}/resume`, request, false, requestOptions,
    )).json()) as AgentCompletionResponse;
  }

  async function* streamCompletion(
    path: string,
    request: ChatCompletionRequest | AgentCompletionRequest,
    requestOptions: HarborRequestOptions = {},
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
      if (!PROGRESS_EVENTS.has(frame.event) &&
          frame.event !== "response.completed" && frame.event !== "response.error") continue;
      let data: Record<string, unknown>;
      try {
        const parsed: unknown = JSON.parse(frame.data);
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
        data = parsed as Record<string, unknown>;
      } catch {
        throw new HarborChatStreamError("invalid_event", "The server returned an invalid JSON event");
      }
      if (frame.event === "response.error") {
        throw new HarborChatStreamError(
          typeof data.code === "string" ? data.code : "stream_error",
          typeof data.message === "string" ? data.message : "The chat stream failed",
        );
      }
      if (frame.event === "response.completed") {
        const message = data.message;
        if (typeof data.session_id !== "string" || message === null || typeof message !== "object" ||
            !("content" in message) || typeof message.content !== "string") {
          throw new HarborChatStreamError("invalid_event", "The completion event is missing its answer or session");
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

  function streamChat(request: ChatCompletionRequest, requestOptions: HarborRequestOptions = {}) {
    return streamCompletion("/v1/chat/completions", request, requestOptions);
  }

  function streamAgent(request: AgentCompletionRequest, requestOptions: HarborRequestOptions = {}) {
    return streamCompletion("/v1/agent/completions", request, requestOptions);
  }

  return { completeChat, streamChat, completeAgent, streamAgent, resumeAgent };
}
