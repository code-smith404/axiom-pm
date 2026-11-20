"""axiom - an immutable, atomic, user-space package manager.

The public Python surface is intentionally small; the command line is the
supported interface and :mod:`axiom.cli` is the only module that performs
side-effecting operations on a user's behalf.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
