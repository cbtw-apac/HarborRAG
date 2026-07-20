/**
 * Thin runtime wrapper for the HarborRAG Control Plane API (ST10).
 *
 * The generated wire types live in ./schema.d.ts (run `npm run generate`
 * against a fresh openapi.json). This wrapper adds the pieces a generator
 * can't know: bearer injection, the error envelope, and trace-id plumbing.
 */

/** The single error envelope every non-2xx response carries (plan §8.3). */
export interface HarborApiError {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
    trace_id: string | null;
  };
}

/** Thrown on any non-2xx response; carries the decoded envelope. */
export class HarborApiRequestError extends Error {
  readonly status: number;
  readonly envelope: HarborApiError;

  constructor(status: number, envelope: HarborApiError) {
    super(`${envelope.error.code}: ${envelope.error.message}`);
    this.name = "HarborApiRequestError";
    this.status = status;
    this.envelope = envelope;
  }
}

export interface HarborClientOptions {
  /** e.g. "http://localhost:8000" — /api/v1 is appended per call. */
  baseUrl: string;
  /** Returns the bearer token, or null for auth_mode=none dev backends. */
  getToken?: () => string | null | Promise<string | null>;
  fetchImpl?: typeof fetch;
}

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
      `${options.baseUrl}/api/v1${path}`,
      {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      },
    );
    if (!response.ok) {
      const envelope = (await response.json()) as HarborApiError;
      throw new HarborApiRequestError(response.status, envelope);
    }
    return (await response.json()) as T;
  }

  return {
    request,
    get: <T>(path: string) => request<T>("GET", path),
    post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
    patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
    delete: <T>(path: string) => request<T>("DELETE", path),
  };
}
