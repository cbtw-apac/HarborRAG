export interface HarborClientOptions {
  /** API origin, for example "http://localhost:8000". */
  baseUrl: string;
  /** Returns a bearer token, or null for a development backend without auth. */
  getToken?: () => string | null | Promise<string | null>;
  fetchImpl?: typeof fetch;
}

export interface HarborRequestOptions {
  headers?: HeadersInit;
  /** Use a separate AbortController for each request that can be cancelled independently. */
  signal?: AbortSignal;
}
