"""Runtime invariant checks that survive `python -O`.

`assert` is stripped when Python runs optimised, so any invariant that guards a value the
next statement actually uses must raise instead. `require` narrows an optional exactly the
way an assert does for type checkers, while still failing loudly in an optimised build.
"""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborError


class HarborInvariantError(HarborError):
    """Raised when internal state violates an invariant the code depends on."""


def require[T](value: T | None, message: str) -> T:
    """Return `value` when present, otherwise raise `HarborInvariantError`."""

    if value is None:
        raise HarborInvariantError(message)
    return value


__all__ = ["HarborInvariantError", "require"]
