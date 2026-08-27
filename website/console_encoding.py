#!/usr/bin/env python3
"""UTF-8 console output for the website command-line scripts."""

from __future__ import annotations

import contextlib
import sys


def enable_utf8_output() -> None:
    """Force UTF-8 on this process's output streams.

    The website scripts report progress with emoji. CPython only selects UTF-8
    for a real Win32 console, so under Git Bash -- whose MinTTY terminal is a
    pipe rather than a console -- or any redirected Windows stream it falls back
    to the ANSI code page (cp1252), and the first emoji raises
    ``UnicodeEncodeError`` before anything is built. macOS, Linux, and WSL
    already run UTF-8 locales, where this is a no-op.

    Streams that cannot be reconfigured -- one replaced by a test harness, or one
    already detached -- are left as they are.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")
