"""Test suite for axiom.

Tests use the standard library's ``unittest`` so they run without any
third-party dependency.  They can also be collected by pytest.

Run from the repository root::

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
