from __future__ import annotations

from typing import ClassVar


class GraphTraversalSyntax:
    """Maps portable graph directions to openCypher path arrows."""

    arrows_by_direction: ClassVar[dict[str, tuple[str, str]]] = {
        "outgoing": ("-", "->"),
        "incoming": ("<-", "-"),
        "both": ("-", "-"),
    }

    @classmethod
    def arrows(cls, direction: str) -> tuple[str, str]:
        """Return validated left and right path arrows for a portable direction."""
        try:
            return cls.arrows_by_direction[direction]
        except KeyError as exc:
            raise ValueError(f"unsupported graph direction {direction!r}") from exc
