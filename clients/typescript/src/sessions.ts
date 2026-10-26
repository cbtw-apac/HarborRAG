import { apiRequestError } from "./errors.js";
import type { HarborClientOptions, HarborRequestOptions } from "./options.js";
import type { components } from "./schema.js";

export type SessionSurface = "chat" | "agent";
export interface SessionListOptions {
  tenant?: string;
  cursor?: string;
  limit?: number;
}
export interface SessionHistoryOptions {
  tenant?: string;
  after?: string;
  limit?: number;
}

/** Session IDs are shared; lists are filtered by the surface that created them. */
export function createSessionMethods(options: HarborClientOptions) {
  const doFetch = options.fetchImpl ?? fetch;

  async function request<T>(
    surface: SessionSurface,
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
    const token = await options.getToken?.();
    requestOptions.signal?.throwIfAborted();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
    const search = params.size ? `?${params}` : "";
    const response = await doFetch(
      `${options.baseUrl.replace(/\/+$/, "")}/v1/${surface}/sessions${suffix}${search}`,
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
    createSession(
      surface: SessionSurface, tenant = "DEFAULT", requestOptions: HarborRequestOptions = {},
    ) {
      return request<components["schemas"]["ChatSessionResponse"]>(
        surface, "POST", "", {}, { tenant }, requestOptions,
      );
    },
    listSessions(
      surface: SessionSurface, query: SessionListOptions = {}, requestOptions: HarborRequestOptions = {},
    ) {
      return request<components["schemas"]["SessionListResponse"]>(
        surface, "GET", "", { ...query }, undefined, requestOptions,
      );
    },
    getSessionHistory(
      surface: SessionSurface, sessionId: string, query: SessionHistoryOptions = {},
      requestOptions: HarborRequestOptions = {},
    ) {
      return request<components["schemas"]["ConversationMessageListResponse"]>(
        surface, "GET", `${sessionPath(sessionId)}/messages`, { ...query }, undefined, requestOptions,
      );
    },
    renameSession(
      surface: SessionSurface, sessionId: string, title: string, tenant = "DEFAULT",
      requestOptions: HarborRequestOptions = {},
    ) {
      return request<components["schemas"]["ConversationRenameResponse"]>(
        surface, "PATCH", sessionPath(sessionId), { tenant }, { title }, requestOptions,
      );
    },
    deleteSession(
      surface: SessionSurface, sessionId: string, tenant = "DEFAULT",
      requestOptions: HarborRequestOptions = {},
    ) {
      return request<components["schemas"]["SessionErasureResponse"]>(
        surface, "DELETE", sessionPath(sessionId), { tenant }, undefined, requestOptions,
      );
    },
  };
}
