import { apiRequestError } from "./errors.js";
import type { HarborClientOptions, HarborRequestOptions } from "./options.js";
import type { components } from "./schema.js";

type Schemas = components["schemas"];

export type SessionCreateRequest = Schemas["ChatSessionCreateRequest"];
export interface SessionListOptions {
  cursor?: string;
  limit?: number;
  /** Search session titles. */
  q?: string;
}
export interface SessionHistoryOptions {
  cursor?: string;
  limit?: number;
}

/** Chat sessions: create one, list the caller's own, and read a session's messages. */
export function createSessionMethods(options: HarborClientOptions) {
  const doFetch = options.fetchImpl ?? fetch;

  async function request<T>(
    method: string,
    suffix: string,
    query: Record<string, string | number | undefined>,
    body: unknown,
    requestOptions: HarborRequestOptions,
  ): Promise<T> {
    requestOptions.signal?.throwIfAborted();
    const headers = new Headers(requestOptions.headers);
    headers.set("Accept", "application/json");
    if (body !== undefined) headers.set("Content-Type", "application/json");
    if (requestOptions.tenant) headers.set("X-Tenant-Id", requestOptions.tenant);
    const token = await options.getToken?.();
    requestOptions.signal?.throwIfAborted();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
    const search = params.size ? `?${params}` : "";
    const response = await doFetch(
      `${options.baseUrl.replace(/\/+$/, "")}/v1/chat/sessions${suffix}${search}`,
      {
        method, headers, signal: requestOptions.signal,
        body: body === undefined ? undefined : JSON.stringify(body),
      },
    );
    if (!response.ok) throw await apiRequestError(response);
    return await response.json() as T;
  }

  function sessionPath(sessionId: string): string {
    if (!sessionId.trim()) throw new TypeError("sessionId must not be blank");
    return `/${encodeURIComponent(sessionId)}`;
  }

  return {
    createSession(body: SessionCreateRequest = {}, requestOptions: HarborRequestOptions = {}) {
      return request<Schemas["Session"]>("POST", "", {}, body, requestOptions);
    },
    listSessions(query: SessionListOptions = {}, requestOptions: HarborRequestOptions = {}) {
      return request<Schemas["SessionList"]>("GET", "", { ...query }, undefined, requestOptions);
    },
    getSessionHistory(
      sessionId: string, query: SessionHistoryOptions = {}, requestOptions: HarborRequestOptions = {},
    ) {
      return request<Schemas["MessageList"]>(
        "GET", `${sessionPath(sessionId)}/messages`, { ...query }, undefined, requestOptions,
      );
    },
  };
}
