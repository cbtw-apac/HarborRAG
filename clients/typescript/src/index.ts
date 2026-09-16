/**
 * Runtime helpers for the HarborRAG operational API and chat completions.
 *
 * The generated wire types live in ./schema.d.ts (run `npm run generate`
 * against a fresh openapi.json). This wrapper adds the pieces a generator
 * can't know: bearer injection, the error envelope, and trace-id plumbing.
 */

import { createSessionMethods } from "./sessions.js";
import { createChatMethods } from "./chat.js";
import { apiRequestError } from "./errors.js";
import type { HarborClientOptions } from "./options.js";

export { HarborApiRequestError, HarborChatStreamError } from "./errors.js";
export type { HarborApiError } from "./errors.js";
export type { HarborClientOptions, HarborRequestOptions } from "./options.js";
export type { SessionSurface, SessionListOptions, SessionHistoryOptions } from "./sessions.js";
export type { paths, components, operations } from "./schema.js";
export type {
  ChatCompletionRequest,
  ChatCompletionResponse,
  ChatCost,
  ChatCitation,
  ChatUsage,
  ChatStreamEvent,
  AgentCompletionRequest,
  AgentResumeRequest,
  AgentCompletionResponse,
} from "./chat.js";

/** Minimal typed fetch seam; screens use this until generated per-resource
 *  helpers land alongside the M1 endpoints. */
export function createHarborClient(options: HarborClientOptions) {
  const doFetch = options.fetchImpl ?? fetch;

  async function request<T>(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = await options.getToken?.();
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const response = await doFetch(
      `${options.baseUrl.replace(/\/+$/, "")}/api/v1${path}`,
      {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      },
    );
    if (!response.ok) {
      throw await apiRequestError(response);
    }
    return (await response.json()) as T;
  }

  return {
    ...createChatMethods(options),
    ...createSessionMethods(options),
    request,
    get: <T>(path: string) => request<T>("GET", path),
    post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
    patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
    delete: <T>(path: string) => request<T>("DELETE", path),
  };
}
