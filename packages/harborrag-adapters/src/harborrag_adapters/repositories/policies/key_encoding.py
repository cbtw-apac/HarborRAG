from __future__ import annotations


def escape_key_part(value: str) -> str:
    """Escape a key segment so a literal ':' cannot collide with the path separator."""
    return value.replace("%", "%25").replace(":", "%3A")
