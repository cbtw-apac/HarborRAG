"""Server-Sent Events event framing shared by the HTTP routes and the in-process chat stream."""

from __future__ import annotations

import json


def sse_frame(name: str, payload: object) -> bytes:
    r"""Encode one ``event: <name>\ndata: <json>\n\n`` frame."""
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()
