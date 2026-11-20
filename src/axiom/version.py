"""Semantic versions and version constraints.

Constraints are the vocabulary of the whole system: a package declares which
versions of its dependencies it is compatible with, an environment declares
which versions it needs, and the solver looks for an assignment that satisfies
every constraint at once.

The syntax is the subset of npm-style ranges that is useful for packages and
environments:

   1.2.3          exact
   =1.2.3         exact
   !=1.2.3        any version except this one
   >1.2.3         strictly greater
   >=1.2.3        greater or equal
   <1.2.3         strictly less
   <=1.2.3        less or equal
   ^1.2.3         compatible: >=1.2.3 <2.0.0
   ~1.2.3         approximate: >=1.2.3 <1.3.0
   1.2.x, 1.*     wildcard ranges
   1.2            partial version, treated as 1.2.x
   1              partial version, treated as 1.x
   1.2.3 - 2.0.0  hyphen range, inclusive
   *              any version
   ""             any version

Ranges are combined with a comma and are ANDed together.  OR (``||``) is
deliberately not supported: a dependency either is or is not compatible, and
disjunctions make failures much harder to explain.  Caret and tilde ranges are
normalised to a pair of simple bounds at parse time, which makes intersections
and error messages straightforward.

Pre-releases follow npm's rule: a pre-release candidate only matches a range
that explicitly mentions a pre-release of the same ``major.minor.patch``.
This keeps ``^1.2.0`` from silently selecting ``1.3.0-rc.1``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import total_ordering

__all__ = [
    "ANY",
    "Constraint",
    "Version",
    "VersionRange",
    "parse_constraint",
    "parse_version",
]


_VERSION_RE = re.compile(
    r"""^
    [vV]?
    (?P<major>0|[1-9]\d*)
    (?:\.(?P<minor>0|[1-9]\d*))?
    (?:\.(?P<patch>0|[1-9]\d*))?
    (?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?
    (?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?
    $""",
    re.VERBOSE,
)

_PARTIAL_RE = re.compile(
    r"""^
    [vV]?
    (?P<major>0|[1-9]\d*)
    (?:\.(?P<minor>0|[1-9]\d*))?
    (?:\.(?P<patch>0|[1-9]\d*))?
    $""",
    re.VERBOSE,
)


def _prerelease_key(parts: Iterable) -> tuple:
    """Order pre-release identifiers the way semver 2.0.0 describes.

    Numeric identifiers compare numerically and sort below alphanumeric ones;
    a shorter identifier list sorts below a longer one when it is a prefix.
    """
    key = []
    for part in parts:
        if isinstance(part, int):
            key.append((0, part, ""))
        else:
            key.append((1, 0, str(part)))
    return tuple(key)


@total_ordering
@dataclass(frozen=True)
class Version:
    """A semantic version: ``major.minor.patch[-prerelease][+build]``.

    Build metadata is retained for display and identity but is ignored when
    comparing *precedence*, as required by semver.  The total order used by
    sorting falls back to the build string only to break ties between versions
    that have equal precedence, so sorting is always deterministic.
    """

    major: int = 0
    minor: int = 0
    patch: int = 0
    prerelease: tuple = ()
    build: str = ""

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(str(part) for part in self.prerelease)
        if self.build:
            text += "+" + self.build
        return text

    def __repr__(self) -> str:
        return f"Version({str(self)!r})"

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def core(self) -> tuple:
        """The ``(major, minor, patch)`` triple."""
        return (self.major, self.minor, self.patch)

    def precedence_key(self) -> tuple:
        """Comparison key that ignores build metadata.

        A release sorts above every pre-release of the same core version, so
        the fourth element is ``1`` for a release and ``0`` for a pre-release.
        """
        return (
            self.major,
            self.minor,
            self.patch,
            1 if not self.prerelease else 0,
            _prerelease_key(self.prerelease),
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (self.precedence_key(), self.build) < (
            other.precedence_key(),
            other.build,
        )

    def same_precedence(self, other: Version) -> bool:
        return self.precedence_key() == other.precedence_key()


def parse_version(text: str | Version) -> Version:
    """Parse ``text`` into a :class:`Version`.

    ``1`` and ``1.2`` are normalised to ``1.0.0`` and ``1.2.0``; callers that
    want npm's "partial means wildcard" behaviour should use
    :func:`parse_constraint` instead.
    """
    if isinstance(text, Version):
        return text
    if not isinstance(text, str):
        raise TypeError(f"version must be a string, got {type(text).__name__}")
    match = _VERSION_RE.match(text.strip())
    if not match:
        raise ValueError(f"invalid version: {text!r}")
    prerelease_text = match.group("prerelease")
    prerelease: tuple = ()
    if prerelease_text:
        prerelease = tuple(
            int(part) if part.isdigit() else part
            for part in prerelease_text.split(".")
        )
    return Version(
        major=int(match.group("major")),
        minor=int(match.group("minor") or 0),
        patch=int(match.group("patch") or 0),
        prerelease=prerelease,
        build=match.group("build") or "",
    )


@dataclass(frozen=True)
class Constraint:
    """A single bound on a version, such as ``>=1.2.0`` or ``*``."""

    operator: str
    version: Version | None = None

    def __str__(self) -> str:
        if self.operator == "*":
            return "*"
        if self.operator == "==":
            return f"={self.version}"
        return f"{self.operator}{self.version}"

    def __repr__(self) -> str:
        return f"Constraint({str(self)!r})"

    @property
    def is_any(self) -> bool:
        return self.operator == "*"

    def matches(self, candidate: Version) -> bool:
        if self.operator == "*":
            return True
        base = self.version
        if base is None:  # pragma: no cover - defensive
            raise ValueError(f"constraint {self!r} has no version")
        op = self.operator
        if op == "==":
            return candidate.same_precedence(base)
        if op == "!=":
            return not candidate.same_precedence(base)
        if op == ">":
            return candidate.precedence_key() > base.precedence_key()
        if op == ">=":
            return candidate.precedence_key() >= base.precedence_key()
        if op == "<":
            return candidate.precedence_key() < base.precedence_key()
        if op == "<=":
            return candidate.precedence_key() <= base.precedence_key()
        raise ValueError(f"unknown operator: {op!r}")


@dataclass(frozen=True)
class VersionRange:
    """A conjunction of :class:`Constraint` objects.

    An empty range means "unconstrained".  Ranges are immutable; combining two
    of them produces a third.
    """

    constraints: tuple = ()

    def __str__(self) -> str:
        if not self.constraints:
            return "*"
        return ",".join(str(c) for c in self.constraints)

    def __repr__(self) -> str:
        return f"VersionRange({str(self)!r})"

    @property
    def is_any(self) -> bool:
        return all(c.is_any for c in self.constraints)

    @property
    def exact(self) -> Version | None:
        """The pinned version if this range is exactly one equality."""
        if len(self.constraints) == 1 and self.constraints[0].operator == "==":
            return self.constraints[0].version
        return None

    def matches(self, candidate: Version, *, allow_prerelease: bool = False) -> bool:
        if (
            candidate.is_prerelease
            and not allow_prerelease
            and not self._allows_prerelease(candidate)
        ):
            return False
        return all(c.matches(candidate) for c in self.constraints)

    # ``contains`` reads better at call sites that talk about ranges.
    contains = matches

    def _allows_prerelease(self, candidate: Version) -> bool:
        """Whether the range explicitly opts into this pre-release line."""
        for constraint in self.constraints:
            base = constraint.version
            if (
                base is not None
                and base.is_prerelease
                and base.core == candidate.core
            ):
                return True
        return False

    def intersect(self, other: VersionRange) -> VersionRange:
        """Return the conjunction of two ranges."""
        return VersionRange(self.constraints + other.constraints)

    def is_satisfied_by_any(self, candidates: Iterable[Version]) -> bool:
        return any(self.matches(c) for c in candidates)


ANY = VersionRange()


def parse_constraint(text: str | VersionRange | None) -> VersionRange:
    """Parse a constraint expression into a :class:`VersionRange`.

    ``None`` and the empty string both mean "any version".
    """
    if isinstance(text, VersionRange):
        return text
    if text is None:
        return ANY
    if not isinstance(text, str):
        raise TypeError(
            f"constraint must be a string, got {type(text).__name__}"
        )
    raw = text.strip()
    if raw in ("", "*", "x", "X"):
        return ANY
    if "||" in raw:
        raise ValueError(
            f"OR ranges are not supported; use a single conjunction: {text!r}"
        )
    if " - " in raw:
        return _parse_hyphen(raw)
    constraints: list[Constraint] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        constraints.extend(_parse_part(piece, whole=text))
    if not constraints:
        return ANY
    return VersionRange(tuple(constraints))


def _parse_part(part: str, *, whole: str) -> list[Constraint]:
    match = re.match(r"^(?P<op>[<>=!~^]*)\s*(?P<rest>.+)$", part)
    if not match:
        raise ValueError(f"invalid constraint: {part!r} (in {whole!r})")
    operator = match.group("op")
    rest = match.group("rest").strip()
    if rest in ("*", "x", "X"):
        if operator in ("", "=", "=="):
            return [Constraint("*")]
        raise ValueError(
            f"operator {operator!r} cannot be applied to a wildcard in {part!r}"
        )

    wildcard = _wildcard_bounds(rest, operator, part)
    if wildcard is not None:
        return wildcard

    partial = _PARTIAL_RE.match(rest)
    if partial:
        major = int(partial.group("major"))
        minor_text = partial.group("minor")
        patch_text = partial.group("patch")

        # Bare or '=' partial versions behave like wildcards: "1.2" is 1.2.x.
        if operator in ("", "=", "==") and (
            minor_text is None or patch_text is None
        ):
            return _partial_equality(major, minor_text, patch_text)

        # '~1' and '~1.2' widen the usual tilde rule to the declared precision.
        if operator == "~":
            lower = Version(major, int(minor_text or 0), int(patch_text or 0))
            if minor_text is None:
                upper = Version(major + 1, 0, 0)
            else:
                upper = Version(major, int(minor_text) + 1, 0)
            return [Constraint(">=", lower), Constraint("<", upper)]

        # '^1', '^0.2' and friends take the left-most non-zero digit as base.
        if operator == "^":
            lower = Version(major, int(minor_text or 0), int(patch_text or 0))
            if major > 0 or minor_text is None:
                upper = Version(major + 1, 0, 0)
            elif int(minor_text) > 0 or patch_text is None:
                upper = Version(0, int(minor_text) + 1, 0)
            else:
                upper = Version(0, int(minor_text), int(patch_text) + 1)
            return [Constraint(">=", lower), Constraint("<", upper)]

    version = parse_version(rest)
    if operator == "":
        operator = "=="
    if operator == "=":
        operator = "=="
    if operator not in ("==", "!=", ">", ">=", "<", "<="):
        raise ValueError(f"unknown operator {operator!r} in {part!r}")
    return [Constraint(operator, version)]


def _wildcard_bounds(
    rest: str, operator: str, whole_part: str
) -> list[Constraint] | None:
    """Expand ``1.x``, ``1.2.*`` and similar wildcard patterns."""
    segments = rest.split(".")
    if not any(segment in ("x", "X", "*") for segment in segments):
        return None
    if operator not in ("", "=", "=="):
        raise ValueError(
            f"operator {operator!r} cannot be combined with a wildcard in "
            f"{whole_part!r}"
        )
    numbers = []
    for index, segment in enumerate(segments):
        if segment in ("x", "X", "*"):
            if index != len(segments) - 1:
                raise ValueError(f"wildcard must be the last segment: {whole_part!r}")
            break
        if not segment.isdigit():
            raise ValueError(f"invalid wildcard version: {whole_part!r}")
        numbers.append(int(segment))
    if not numbers:
        return [Constraint("*")]
    lower = Version(*(numbers + [0] * (3 - len(numbers))))
    if len(numbers) == 1:
        upper = Version(numbers[0] + 1, 0, 0)
    elif len(numbers) == 2:
        upper = Version(numbers[0], numbers[1] + 1, 0)
    else:
        upper = Version(numbers[0], numbers[1], numbers[2] + 1)
    return [Constraint(">=", lower), Constraint("<", upper)]


def _partial_equality(
    major: int, minor_text: str | None, patch_text: str | None
) -> list[Constraint]:
    lower = Version(major, int(minor_text or 0), int(patch_text or 0))
    if minor_text is None:
        upper = Version(major + 1, 0, 0)
    else:
        upper = Version(major, int(minor_text) + 1, 0)
    return [Constraint(">=", lower), Constraint("<", upper)]


def _parse_hyphen(raw: str) -> VersionRange:
    """Parse ``1.2.3 - 2.0.0`` (npm hyphen ranges)."""
    left_text, _, right_text = raw.partition(" - ")
    left_text, right_text = left_text.strip(), right_text.strip()
    if not left_text or not right_text:
        raise ValueError(f"invalid hyphen range: {raw!r}")

    left = _PARTIAL_RE.match(left_text)
    right = _PARTIAL_RE.match(right_text)
    if not left or not right:
        raise ValueError(f"invalid hyphen range: {raw!r}")

    lower = Version(
        int(left.group("major")),
        int(left.group("minor") or 0),
        int(left.group("patch") or 0),
    )
    constraints = [Constraint(">=", lower)]
    right_major = int(right.group("major"))
    right_minor = right.group("minor")
    right_patch = right.group("patch")
    if right_minor is None:
        constraints.append(Constraint("<", Version(right_major + 1, 0, 0)))
    elif right_patch is None:
        constraints.append(
            Constraint("<", Version(right_major, int(right_minor) + 1, 0))
        )
    else:
        constraints.append(
            Constraint(
                "<=",
                Version(right_major, int(right_minor), int(right_patch)),
            )
        )
    return VersionRange(tuple(constraints))
