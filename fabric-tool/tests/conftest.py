"""Paths for the fabric-tool tests: the tool package, this directory, and SparkRing beside it.

The equivalence and apply tests import SparkRing's SIRCL package
(``sparkring_sircl``) and its host-command simulator when the repository is
present; in the standalone archive (``fabric-tool`` alone) they skip.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SIRCL = PROJECT.parent / "sircl"          # SparkRing's SIRCL package lives here (sparkring_sircl/)
for path in (PROJECT, Path(__file__).resolve().parent, SIRCL):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def sircl_available() -> bool:
    """Whether SparkRing's SIRCL package can be imported (the repository is beside the tool)."""
    try:
        import sparkring_sircl  # noqa: F401
    except ImportError:
        return False
    return True
