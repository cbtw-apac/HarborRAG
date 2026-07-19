from __future__ import annotations

import os
import sys

# Make the sibling support modules (model_runtime_support, model_invocation_support,
# telemetry_support) importable without packaging this directory; the shared root
# pyproject cannot carry a per-package pythonpath entry.
sys.path.insert(0, os.path.dirname(__file__))
