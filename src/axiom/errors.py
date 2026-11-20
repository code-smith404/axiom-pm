"""Error types shared by the resolver, the store and the profile manager.

Errors carry structured detail so the CLI can present an explanation instead
of a traceback.  Every error in this module is expected: it means the user's
declared state is inconsistent, not that the tool has a bug.
"""

from __future__ import annotations

__all__ = [
    "ArtifactError",
    "AxiomError",
    "ConflictError",
    "EnvironmentError",
    "ProfileError",
    "ResolutionError",
    "StoreError",
    "VerificationError",
]


class AxiomError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1

    def __init__(self, message: str, *, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = list(details or ())

    def render(self) -> str:
        lines = [self.message]
        lines.extend(f"  - {detail}" for detail in self.details)
        return "\n".join(lines)


class EnvironmentError(AxiomError):
    """An environment file is malformed or internally inconsistent."""


class ResolutionError(AxiomError):
    """No assignment of versions satisfies all constraints."""

    exit_code = 3


class ConflictError(ResolutionError):
    """Two selected packages declare a conflict."""


class StoreError(AxiomError):
    """The content store is missing data or refuses an operation."""


class ArtifactError(AxiomError):
    """A package artifact is malformed or fails verification."""


class ProfileError(AxiomError):
    """A profile cannot be read, activated or rolled back."""


class VerificationError(AxiomError):
    """Installed content does not match its recorded digests."""

    exit_code = 4
