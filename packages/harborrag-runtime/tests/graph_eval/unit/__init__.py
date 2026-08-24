"""CI-collected tests over the suite's pure modules -- no FalkorDB, no credentials.

Everything here runs in the default pytest selection, which is why it lives outside
``smoke/``: that directory is manual, opt-in, and intentionally uncollected. The split
is by how a thing is run, not by what it is about, so a health test sits beside a
corpus test here while their shared library stays in ``../health/`` and ``../corpus.py``.
"""
