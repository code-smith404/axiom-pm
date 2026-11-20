"""Dependency resolution.

The resolver is a deterministic backtracking search.  It is deliberately
small: an environment is a conjunction of version ranges, so the job is to
find one package version per name that satisfies every range at once, plus
every conflict declaration.  When no such assignment exists, the failure is
explained in terms of the constraints that produced it.

Determinism matters more than raw speed here.  Candidate order is fixed
(highest version first, then stable before pre-release, then name), the next
requirement to expand is chosen by fewest remaining candidates and then by
alphabetical order, and the search has a hard step budget.  Solving the same
store twice therefore produces the same snapshot digest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .errors import ResolutionError
from .model import Package, Requirement, VersionRange

__all__ = ["CandidateSource", "Resolver", "Solution", "solve"]

DEFAULT_MAX_STEPS = 200_000
_MAX_RECORDED_FAILURES = 24


class CandidateSource(Protocol):
    """Anything that can enumerate available versions of a name."""

    def candidates(self, name: str) -> list:
        """Return package entries for ``name`` (by name or ``provides``)."""


@dataclass(frozen=True)
class Solution:
    """A complete, conflict-free assignment of versions."""

    packages: tuple
    features: Mapping = field(default_factory=dict)
    steps: int = 0

    def __iter__(self):
        return iter(self.packages)

    def __len__(self) -> int:
        return len(self.packages)

    def by_name(self) -> dict:
        return {package.name: package for package in self.packages}

    def identities(self) -> tuple:
        return tuple(package.identity for package in self.packages)


@dataclass
class _State:
    selected: dict
    claims: dict
    pending: dict
    provenance: dict
    features: dict = field(default_factory=dict)


@dataclass
class _Failure:
    name: str
    range: VersionRange
    detail: str = ""
    provenance: tuple = ()


class Resolver:
    """Resolve requirements against a candidate source."""

    def __init__(
        self,
        source: CandidateSource,
        *,
        allow_prerelease: bool = False,
        max_steps: int = DEFAULT_MAX_STEPS,
    ):
        self.source = source
        self.allow_prerelease = allow_prerelease
        self.max_steps = max_steps
        self._cache: dict = {}
        self._steps = 0
        self._failures: list = []

    # ------------------------------------------------------------------ public

    def solve(
        self, requirements: Iterable[Requirement], *, environment: str = ""
    ) -> Solution:
        pending: dict = {}
        provenance: dict = {}
        origin = f"environment {environment!r}" if environment else "request"
        for requirement in requirements:
            if requirement.optional:
                # An optional root requirement means "if anything else wants
                # it"; with nothing else asking, it stays out of the solution.
                continue
            pending[requirement.name] = _merge(
                pending.get(requirement.name), requirement.constraint
            )
            provenance.setdefault(requirement.name, []).append(
                (origin, requirement.constraint)
            )

        self._steps = 0
        self._failures = []
        state = _State(
            selected={},
            claims={},
            pending=pending,
            provenance=provenance,
            features={},
        )
        if not pending:
            return Solution(packages=(), steps=0)
        result = self._search(state)
        if result is None:
            raise ResolutionError(
                _failure_message(environment, self._failures),
                details=self._failure_details(),
            )
        packages = tuple(sorted(result.selected.values(), key=lambda p: p.name))
        features: dict = {}
        for name, values in result.features.items():
            if not values:
                continue
            real = result.claims.get(name, name)
            features.setdefault(real, set()).update(values)
        features = {
            name: tuple(sorted(values)) for name, values in features.items()
        }
        return Solution(packages=packages, features=features, steps=self._steps)

    # ------------------------------------------------------------------ search

    def _search(self, state: _State) -> _State | None:
        if self._steps >= self.max_steps:
            raise ResolutionError(
                "resolution exceeded its search budget "
                f"({self.max_steps} steps); narrow the constraints and retry"
            )
        if not state.pending:
            return state

        choice = None
        for name in sorted(state.pending):
            options = self._candidates(name, state)
            if not options:
                self._record_failure(name, state)
                return None
            if choice is None or len(options) < len(choice[1]):
                choice = (name, options)
        assert choice is not None
        name, options = choice
        version_range = state.pending[name]

        for package in options:
            self._steps += 1
            if self._steps >= self.max_steps:
                raise ResolutionError(
                    "resolution exceeded its search budget "
                    f"({self.max_steps} steps); narrow the constraints and retry"
                )
            conflict = self._find_conflict(package, state.selected)
            if conflict is not None:
                detail = (
                    f"{package.identity} conflicts with "
                    f"{conflict.identity}: {self._conflict_reason(conflict, package)}"
                )
                self._failures.append(
                    _Failure(name, version_range, detail)
                )
                continue

            next_state = self._apply(state, name, package)
            if next_state is None:
                continue
            result = self._search(next_state)
            if result is not None:
                return result

        # Deeper failures and conflict rejections have already been recorded;
        # recording this name again here only adds noise to the explanation.
        return None

    def _apply(self, state: _State, name: str, package: Package) -> _State | None:
        selected = dict(state.selected)
        existing = selected.get(package.name)
        if existing is not None and existing.version != package.version:
            return None
        selected[package.name] = package
        claims = dict(state.claims)
        claims[name] = package.name
        pending = dict(state.pending)
        pending.pop(name, None)
        provenance = {key: list(value) for key, value in state.provenance.items()}
        features = {key: set(value) for key, value in state.features.items()}

        for dependency in package.dependencies:
            if dependency.name == name:
                # A package requiring itself is satisfied by its own version.
                if not dependency.constraint.matches(package.version):
                    self._failures.append(
                        _Failure(
                            name,
                            dependency.constraint,
                            f"{package.identity} requires itself "
                            f"{dependency.constraint}",
                        )
                    )
                    return None
                continue
            if dependency.optional and not self._optional_is_active(
                dependency.name, selected, pending, claims
            ):
                continue

            if dependency.features:
                features.setdefault(dependency.name, set()).update(
                    dependency.features
                )

            provider = self._selected_provider(dependency.name, selected, claims)
            if provider is not None:
                claims[dependency.name] = provider.name
                if not dependency.constraint.matches(
                    provider.version, allow_prerelease=self.allow_prerelease
                ):
                    self._failures.append(
                        _Failure(
                            dependency.name,
                            dependency.constraint,
                            f"{package.identity} requires "
                            f"{dependency.name} {dependency.constraint}, but "
                            f"selected provider {provider.identity} does not match",
                        )
                    )
                    return None
                continue

            real = claims.get(dependency.name, dependency.name)
            current = selected.get(real)
            if current is not None:
                if not dependency.constraint.matches(
                    current.version, allow_prerelease=self.allow_prerelease
                ):
                    self._failures.append(
                        _Failure(
                            dependency.name,
                            dependency.constraint,
                            f"{package.identity} requires "
                            f"{dependency.name} {dependency.constraint}, but "
                            f"{current.identity} is already selected",
                        )
                    )
                    return None
                continue

            combined = _merge(pending.get(dependency.name), dependency.constraint)
            pending[dependency.name] = combined
            provenance.setdefault(dependency.name, []).append(
                (package.identity, dependency.constraint)
            )
            if not self._range_is_viable(dependency.name, combined, selected, claims):
                self._failures.append(
                    _Failure(
                        dependency.name,
                        combined,
                        f"{package.identity} requires "
                        f"{dependency.name} {dependency.constraint}",
                    )
                )
                return None

        return _State(
            selected=selected,
            claims=claims,
            pending=pending,
            provenance=provenance,
            features=features,
        )

    # --------------------------------------------------------------- helpers

    def _candidates(self, name: str, state: _State) -> list:
        """Viable packages for ``name`` under the current state, best first."""
        version_range = state.pending[name]
        claim = state.claims.get(name)
        result = []
        for package in self._packages(name):
            if claim is not None and package.name != claim:
                continue
            if not version_range.matches(
                package.version, allow_prerelease=self.allow_prerelease
            ):
                continue
            selected = state.selected.get(package.name)
            if selected is not None and selected.version != package.version:
                continue
            result.append(package)
        result.sort(key=lambda pkg: (pkg.version, pkg.name), reverse=True)
        if not self.allow_prerelease:
            # Stable versions first while preserving version order otherwise.
            result.sort(key=lambda pkg: pkg.version.is_prerelease)
        else:
            # Prefer direct name matches over virtual providers.
            result.sort(key=lambda pkg: pkg.name != name)
        return result

    def _range_is_viable(
        self,
        name: str,
        version_range: VersionRange,
        selected: dict,
        claims: dict,
    ) -> bool:
        claim = claims.get(name)
        for package in self._packages(name):
            if claim is not None and package.name != claim:
                continue
            if not version_range.matches(
                package.version, allow_prerelease=self.allow_prerelease
            ):
                continue
            current = selected.get(package.name)
            if current is not None and current.version != package.version:
                continue
            return True
        return False

    def _packages(self, name: str) -> list:
        if name not in self._cache:
            entries = self.source.candidates(name)
            packages = []
            seen = set()
            for entry in entries:
                package = getattr(entry, "package", entry)
                if not isinstance(package, Package):  # pragma: no cover - defensive
                    raise TypeError(
                        f"candidate source returned {type(entry).__name__}, "
                        "expected Package or StoreEntry"
                    )
                key = (package.name, str(package.version), package.to_json())
                if key in seen:
                    continue
                seen.add(key)
                packages.append(package)
            self._cache[name] = packages
        return self._cache[name]

    def _selected_provider(
        self, name: str, selected: Mapping, claims: Mapping
    ) -> Package | None:
        if name in claims:
            return selected.get(claims[name])
        for package in selected.values():
            if name in package.provides and package.name != name:
                return package
        return None

    def _optional_is_active(
        self, name: str, selected: dict, pending: dict, claims: dict
    ) -> bool:
        if name in pending:
            return True
        real = claims.get(name, name)
        if real in selected:
            return True
        # A virtual name may be provided by an already-selected package.
        for package in selected.values():
            if name in package.provides:
                return True
        return False

    def _find_conflict(
        self, candidate: Package, selected: Mapping
    ) -> Package | None:
        for other in selected.values():
            if other.name == candidate.name:
                continue
            if self._conflicting(candidate, other) or self._conflicting(
                other, candidate
            ):
                return other
        return None

    def _conflicting(self, source: Package, target: Package) -> bool:
        for conflict in source.conflicts:
            if conflict.name in (target.name, *target.provides) and (
                conflict.constraint.matches(
                    target.version, allow_prerelease=self.allow_prerelease
                )
            ):
                return True
        return False

    def _conflict_reason(self, first: Package, second: Package) -> str:
        for source, target in ((first, second), (second, first)):
            for conflict in source.conflicts:
                if conflict.name in (target.name, *target.provides):
                    return (
                        f"{source.identity} declares conflict with "
                        f"{conflict.name} {conflict.constraint}"
                    )
        return "declared conflict"  # pragma: no cover - defensive

    def _record_failure(self, name: str, state: _State) -> None:
        range_ = state.pending[name]
        claim = state.claims.get(name)
        available = sorted(
            {
                package.version
                for package in self._packages(name)
                if claim in (None, package.name)
            },
            reverse=True,
        )
        if available:
            detail = "available: " + ", ".join(str(version) for version in available)
        else:
            detail = f"no package named {name!r} is registered"
        provenance = tuple(state.provenance.get(name, ()))
        if provenance:
            requesters = []
            for requirer, requested in provenance[-4:]:
                requesters.append(f"{requirer} wants {requested}")
            detail += "; " + "; ".join(requesters)
        self._failures.append(
            _Failure(
                name,
                range_,
                detail,
                provenance,
            )
        )

    def _failure_details(self) -> list:
        if not self._failures:
            return []
        lines: list = []
        seen = set()
        for failure in self._failures[-_MAX_RECORDED_FAILURES:]:
            text = f"{failure.name} {failure.range}"
            if failure.detail:
                text += f" ({failure.detail})"
            if text in seen:
                continue
            seen.add(text)
            lines.append(text)
        return lines


def _merge(current: VersionRange | None, new: VersionRange) -> VersionRange:
    if current is None:
        return new
    return current.intersect(new)


def _failure_message(environment: str, failures: list) -> str:
    target = f"environment {environment!r}" if environment else "the request"
    if not failures:
        return f"cannot resolve {target}"
    names = []
    for failure in failures[-6:]:
        if failure.name not in names:
            names.append(failure.name)
    if names:
        return (
            f"cannot resolve {target}: no assignment satisfies "
            + ", ".join(repr(name) for name in names)
        )
    return f"cannot resolve {target}: constraints are inconsistent"


def solve(
    requirements: Iterable[Requirement],
    source: CandidateSource,
    *,
    allow_prerelease: bool = False,
    environment: str = "",
) -> Solution:
    """Convenience wrapper around :class:`Resolver`."""
    return Resolver(source, allow_prerelease=allow_prerelease).solve(
        requirements, environment=environment
    )
