"""Manual, opt-in scripts that need a live FalkorDB -- deliberately not collected.

Per ``docs/developers/testing/README.md``, a ``smoke/`` directory holds standalone
``python``-runnable checks that sit outside normal pytest discovery, so nothing here
carries a ``test_`` prefix. Each exits 0 pass, 1 failure, 2 prerequisites unavailable;
exit 2 also covers a query-time failure after a successful connect, so read stderr
rather than treating it as a skip.

The pure logic these scripts drive lives in the shared library beside this package
(``../corpus.py``, ``../golden/``, ``../health/``) and is tested in ``../unit/``.

Human-readable output (leveled summary lines) goes to stderr; stdout stays
machine-readable JSON, because graph_diff.py and --output files consume it.
"""

import logging
import sys


def configure_logging() -> None:
    """Route the scripts' harborrag.graph_eval.* loggers to plain stderr lines."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
