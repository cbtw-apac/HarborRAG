"""Build-health library: census -> report -> gate, and the diff between two reports.

Three pure modules, each with a CLI in ``../smoke/`` and a test in ``../unit/``:

- ``metrics.py`` turns FalkorDB census rows into a ``GraphHealthReport`` and decides
  which findings are gates. ``../smoke/graph_health.py`` owns the Cypher that produces
  those rows, because asking them needs a live graph.
- ``diffing.py`` compares two reports; ``../smoke/graph_diff.py`` is its CLI.
- ``corpus_census.py`` computes the same report from the eval corpus with no graph at
  all, which is what makes ``baselines/graph-eval.json`` a committed artifact and the
  baseline-vs-current diff a plain CI test rather than a live one.

``../smoke/gate_mutation_check.py`` seeds a violation per gated census and asserts each
gate fires -- the only check on the Cypher itself, which no unit test can reach.
"""
