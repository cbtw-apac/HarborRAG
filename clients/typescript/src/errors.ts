/** The standard error envelope returned by the HarborRAG API. */
export interface HarborApiError {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
    trace_id: string | null;
  };
}

/** A failed HTTP request, including its status and decoded error envelope. */
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

/** Decode API errors while handling proxy responses that contain no JSON. */
export async function apiRequestError(response: Response): Promise<HarborApiRequestError> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    value = null;
  }
  if (value !== null && typeof value === "object" && "error" in value) {
    const error = value.error;
    if (error !== null && typeof error === "object" && "code" in error &&
        "message" in error && typeof error.code === "string" && typeof error.message === "string") {
      return new HarborApiRequestError(response.status, value as HarborApiError);
    }
  }
  return new HarborApiRequestError(response.status, {
    error: {
      code: "http_error",
      message: `Request failed with HTTP ${response.status}`,
      details: {},
      trace_id: response.headers.get("x-request-id"),
    },
  });
}

/** A terminal SSE error or a stream that ended without a completion event. */
export class HarborChatStreamError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(`${code}: ${message}`);
    this.name = "HarborChatStreamError";
    this.code = code;
  }
}
