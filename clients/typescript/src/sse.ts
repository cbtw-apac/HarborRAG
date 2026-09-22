import { HarborChatStreamError } from "./errors.js";

export interface SseFrame {
  event: string;
  data: string;
}

const MAX_EVENT_CHARACTERS = 2 * 1024 * 1024;

/** Parse SSE independently of network chunk boundaries, including split UTF-8 and CRLF. */
export async function* parseSse(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SseFrame, void, undefined> {
  signal?.throwIfAborted();
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let event = "message";
  let data: string[] = [];
  let frameCharacters = 0;
  const abort = () => { void reader.cancel(signal?.reason).catch(() => undefined); };
  signal?.addEventListener("abort", abort, { once: true });

  try {
    while (true) {
      signal?.throwIfAborted();
      const { value, done } = await reader.read();
      signal?.throwIfAborted();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      while (true) {
        signal?.throwIfAborted();
        const end = buffer.search(/[\r\n]/);
        if (end < 0 || (!done && buffer[end] === "\r" && end === buffer.length - 1)) break;
        const line = buffer.slice(0, end);
        const separator = buffer[end] === "\r" && buffer[end + 1] === "\n" ? 2 : 1;
        buffer = buffer.slice(end + separator);
        frameCharacters += line.length;
        if (frameCharacters > MAX_EVENT_CHARACTERS) {
          throw new HarborChatStreamError("event_too_large", "SSE event exceeded the client size limit");
        }
        if (line === "") {
          if (data.length > 0) yield { event, data: data.join("\n") };
          event = "message";
          data = [];
          frameCharacters = 0;
        } else if (!line.startsWith(":")) {
          const colon = line.indexOf(":");
          const field = colon < 0 ? line : line.slice(0, colon);
          let content = colon < 0 ? "" : line.slice(colon + 1);
          if (content.startsWith(" ")) content = content.slice(1);
          if (field === "event") event = content || "message";
          if (field === "data") data.push(content);
        }
      }
      if (buffer.length + frameCharacters > MAX_EVENT_CHARACTERS) {
        throw new HarborChatStreamError("event_too_large", "SSE event exceeded the client size limit");
      }
      // An unterminated frame is discarded. streamChat detects a missing terminal event.
      if (done) return;
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
