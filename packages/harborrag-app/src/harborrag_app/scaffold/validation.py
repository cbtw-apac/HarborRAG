"""Checks every `init` answer before it is written into .env or a YAML catalog."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .render import InitOptions

type Rule = Callable[[str], str | None]

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_YAML_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
_PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def plain_text(value: str) -> str | None:
    """Reject a value that could start a new line in .env or YAML."""
    if _CONTROL_CHARACTERS.search(value):
        return "must not contain line breaks or other control characters"
    return None


def yaml_token(value: str) -> str | None:
    """Accept a name that reads as one plain YAML scalar, such as a model or branch name."""
    if not _YAML_TOKEN.match(value):
        return "may only contain letters, digits and . _ : / @ + -"
    return None


def http_url(value: str) -> str | None:
    """Accept an absolute http or https URL with a host; empty means fill in later."""
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "must be an http:// or https:// URL"
    return None


def project_keys(value: str) -> str | None:
    """Accept a comma-separated list of Jira project keys, such as ENG, OPS."""
    keys = [item.strip() for item in value.split(",") if item.strip()]
    if not keys or not all(_PROJECT_KEY.match(key) for key in keys):
        return "must be comma-separated project keys such as ENG, OPS"
    return None


_OPTION_RULES: Mapping[str, tuple[Rule, ...]] = {
    "--chat-model": (yaml_token,),
    "--embed-model": (yaml_token,),
    "--api-base": (http_url,),
}
_SOURCE_RULES: Mapping[str, tuple[Rule, ...]] = {
    "GITHUB_BRANCH": (yaml_token,),
    "JIRA_PROJECT_KEYS": (project_keys,),
}


@dataclass(frozen=True, slots=True)
class ScaffoldInputPolicy:
    """Apply ``plain_text`` to every answer, then each answer's own rules.

    Option rules are keyed by the ``init`` option that sets the answer; source rules
    by variable name, with every ``*_URL`` variable checked by ``http_url``.
    """

    option_rules: Mapping[str, tuple[Rule, ...]] = field(default_factory=lambda: _OPTION_RULES)
    source_rules: Mapping[str, tuple[Rule, ...]] = field(default_factory=lambda: _SOURCE_RULES)

    def check(self, options: InitOptions) -> None:
        """Validate ``options``.

        Raises:
            ValueError: One or more answers are unsafe; every problem is listed.
        """
        problems: list[str] = []
        answers = {
            "--chat-model": options.chat_model,
            "--embed-model": options.embed_model,
            "--api-key": options.api_key,
            "--api-base": options.api_base or "",
            "--source": options.source_path,
        }
        for name, value in answers.items():
            problems.extend(_apply(name, value, self.option_rules.get(name, ())))
        for name, value in options.source_values.items():
            rules = self.source_rules.get(name, (http_url,) if name.endswith("_URL") else ())
            problems.extend(_apply(name, value, rules if value else ()))
        if problems:
            raise ValueError("; ".join(problems))


def _apply(name: str, value: str, rules: tuple[Rule, ...]) -> list[str]:
    """Return ``name``'s problems: control characters first, then its own rules."""
    for rule in (plain_text, *rules):
        problem = rule(value)
        if problem is not None:
            return [f"{name} {problem}"]
    return []
