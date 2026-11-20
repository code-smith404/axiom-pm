"""Core immutable data model: packages, requirements, environments.

A *package* describes one artifact: its name, version, the constraints it
places on its dependencies, and the files it installs.  A *requirement* asks
for a package inside some version range.  An *environment* is a declarative
set of requirements plus host assertions and variables.

Nothing here mutates.  Desired state is data that can be read, diffed, hashed
and checked into version control; the mutable parts of the system (the store
and the profiles) are rebuilt from this data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from .version import ANY, Version, VersionRange, parse_constraint, parse_version

__all__ = [
    "Dependency",
    "Environment",
    "EnvironmentConstraint",
    "FileSpec",
    "Package",
    "PackageFile",
    "Requirement",
    "canonical_json",
    "content_digest",
]


def canonical_json(value: object) -> str:
    """Serialise ``value`` deterministically for hashing and storage."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )


def content_digest(value: object) -> str:
    """Return the ``sha256:`` digest of a canonical JSON encoding."""
    payload = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FileSpec:
    """A file installed by a package.

    ``source`` is a path inside the package payload, ``target`` is where the
    file appears in the profile, and ``mode`` is a four-digit octal string.
    Targets are always relative and may not escape the profile root.
    """

    source: str
    target: str
    mode: str = "0644"

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_mode(self.mode))

    def to_dict(self) -> dict:
        return {"source": self.source, "target": self.target, "mode": self.mode}


def normalize_mode(mode: str | int) -> str:
    """Return ``mode`` as a canonical four-digit octal string."""
    if isinstance(mode, int):
        value = mode
    else:
        text = str(mode).strip().lower()
        text = text.removeprefix("0o")
        if not text:
            raise ValueError("file mode may not be empty")
        try:
            value = int(text, 8)
        except ValueError as exc:
            raise ValueError(f"invalid file mode: {mode!r}") from exc
    if value < 0 or value > 0o777:
        # Setuid, setgid and sticky bits are refused: a user-space package has
        # no business marking installed files that way.
        raise ValueError(
            f"file mode out of range (special bits are not allowed): {mode!r}"
        )
    return f"{value:04o}"


@dataclass(frozen=True)
class Dependency:
    """A dependency declared by a package.

    ``optional`` dependencies are only pulled in when something else already
    requires them.  ``features`` are recorded in the snapshot for consumers
    that care about build variants; the resolver keeps a single version per
    package name, so features never cause a package to be installed twice.
    """

    name: str
    constraint: VersionRange = field(default_factory=lambda: ANY)
    optional: bool = False
    features: tuple = ()

    def __str__(self) -> str:
        text = f"{self.name} {self.constraint}".strip()
        if self.optional:
            text += " (optional)"
        return text

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "constraint": str(self.constraint),
            "optional": self.optional,
            "features": list(self.features),
        }


@dataclass(frozen=True)
class Requirement:
    """A request for a package: a name plus a version range."""

    name: str
    constraint: VersionRange = field(default_factory=lambda: ANY)
    optional: bool = False
    reason: str = ""

    def __str__(self) -> str:
        text = self.name
        if not self.constraint.is_any:
            text += f" {self.constraint}"
        if self.optional:
            text += " (optional)"
        return text

    @property
    def is_exact(self) -> bool:
        return self.constraint.exact is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "constraint": str(self.constraint),
            "optional": self.optional,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PackageFile:
    """A file entry as stored in the package registry.

    ``digest`` is the hex sha256 of the file contents.  It is what makes a
    package immutable: the metadata names content, never a mutable path.
    """

    target: str
    digest: str
    mode: str = "0644"
    size: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_mode(self.mode))
        object.__setattr__(self, "size", int(self.size))
        digest = str(self.digest)
        digest = digest.removeprefix("sha256:")
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ValueError(f"invalid file digest: {self.digest!r}")
        object.__setattr__(self, "digest", digest)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "digest": self.digest,
            "mode": self.mode,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> PackageFile:
        return cls(
            target=data["target"],
            digest=data["digest"],
            mode=data.get("mode", "0644"),
            size=int(data.get("size", 0)),
        )


@dataclass(frozen=True)
class EnvironmentConstraint:
    """A condition on the *host* rather than on packages.

    ``kind`` names a fact namespace (``os``, ``arch``, ``kernel``, ``env``,
    ``python`` or ``host``); ``name`` is a key inside it.  Values are compared
    as numbers when both sides are numeric, otherwise as strings.
    """

    kind: str
    name: str
    operator: str = "=="
    value: str = ""
    message: str = ""

    def __str__(self) -> str:
        return f"{self.kind}.{self.name} {self.operator} {self.value}".strip()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "operator": self.operator,
            "value": self.value,
            "message": self.message,
        }

    def evaluate(self, facts: Mapping[str, Mapping[str, str]]) -> tuple:
        """Return ``(satisfied, detail)`` for this constraint."""
        namespace = facts.get(self.kind, {})
        actual = namespace.get(self.name)
        if actual is None:
            detail = f"{self.kind}.{self.name} is not available on this host"
            if self.message:
                detail += f" ({self.message})"
            return False, detail
        try:
            ok = _compare(str(actual), self.operator, self.value)
        except ValueError as exc:
            return False, str(exc)
        detail = (
            f"{self.kind}.{self.name}={actual} "
            f"required {self.operator} {self.value}"
        )
        if self.message:
            detail += f" ({self.message})"
        return ok, detail


_COMPARISONS = {
    "==": lambda a, b: a == b,
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _compare(actual: str, operator: str, expected: str) -> bool:
    if operator in _COMPARISONS:
        return _COMPARISONS[operator](actual, expected)
    if operator in (">", ">=", "<", "<="):
        left, right = _as_number(actual), _as_number(expected)
        if left is None or right is None:
            raise ValueError(
                f"cannot compare {actual!r} {operator} {expected!r} numerically"
            )
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        return left <= right
    if operator == "in":
        return actual in [item.strip() for item in expected.split(",")]
    if operator == "not-in":
        return actual not in [item.strip() for item in expected.split(",")]
    if operator == "contains":
        return expected in actual
    if operator == "startswith":
        return actual.startswith(expected)
    raise ValueError(f"unknown comparison operator: {operator!r}")


def _as_number(text: str):
    try:
        return int(text)
    except (TypeError, ValueError):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Package:
    """An immutable package definition.

    The definition is metadata only.  File content lives in the content store
    addressed by digest, which keeps definitions small, shareable and safe to
    compare.
    """

    name: str
    version: Version
    dependencies: tuple = ()
    description: str = ""
    homepage: str = ""
    license: str = ""
    provides: tuple = ()
    conflicts: tuple = ()
    files: tuple = ()
    environment_constraints: tuple = ()
    scripts: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("package name may not be empty")
        if not isinstance(self.version, Version):
            object.__setattr__(self, "version", parse_version(self.version))
        if self.name in (".", "..") or any(
            char in self.name for char in "/\\ \t\n\r"
        ):
            raise ValueError(f"invalid package name: {self.name!r}")
        if any(ord(char) < 32 for char in self.name):
            raise ValueError(f"invalid package name: {self.name!r}")

    def __str__(self) -> str:
        return self.identity

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def dependency(self, name: str) -> Dependency | None:
        for dep in self.dependencies:
            if dep.name == name:
                return dep
        return None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": str(self.version),
            "description": self.description,
            "homepage": self.homepage,
            "license": self.license,
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "provides": list(self.provides),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "files": [spec.to_dict() for spec in self.files],
            "environment_constraints": [
                c.to_dict() for c in self.environment_constraints
            ],
            "scripts": dict(self.scripts),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping) -> Package:
        name = data.get("name")
        if not name:
            raise ValueError("package definition requires a 'name'")
        raw_version = data.get("version")
        if not raw_version:
            raise ValueError(f"package {name!r} requires a 'version'")
        return cls(
            name=str(name),
            version=parse_version(str(raw_version)),
            dependencies=tuple(
                Dependency(
                    name=str(item["name"]),
                    constraint=parse_constraint(item.get("constraint")),
                    optional=bool(item.get("optional", False)),
                    features=tuple(item.get("features", ())),
                )
                for item in data.get("dependencies", ())
            ),
            description=str(data.get("description", "")),
            homepage=str(data.get("homepage", "")),
            license=str(data.get("license", "")),
            provides=tuple(str(item) for item in data.get("provides", ())),
            conflicts=tuple(
                _dependency_from(item) for item in data.get("conflicts", ())
            ),
            files=tuple(
                FileSpec(
                    source=str(item["source"]),
                    target=str(item.get("target", item["source"])),
                    mode=item.get("mode", "0644"),
                )
                for item in data.get("files", ())
            ),
            environment_constraints=tuple(
                EnvironmentConstraint(
                    kind=str(item.get("kind", "host")),
                    name=str(item["name"]),
                    operator=str(item.get("operator", "==")),
                    value=str(item.get("value", "")),
                    message=str(item.get("message", "")),
                )
                for item in data.get("environment_constraints", ())
            ),
            scripts=dict(data.get("scripts", {})),
        )


def _dependency_from(item: str | Mapping) -> Dependency:
    if isinstance(item, str):
        name, _, constraint = item.partition(" ")
        return Dependency(name=name.strip(), constraint=parse_constraint(constraint))
    if isinstance(item, Mapping):
        if "name" not in item:
            raise ValueError(f"dependency requires a 'name': {item!r}")
        return Dependency(
            name=str(item["name"]),
            constraint=parse_constraint(item.get("constraint")),
            optional=bool(item.get("optional", False)),
            features=tuple(item.get("features", ())),
        )
    raise ValueError(f"invalid dependency: {item!r}")


def _requirement_from(item: str | Mapping) -> Requirement:
    if isinstance(item, str):
        name, _, constraint = item.partition(" ")
        if not name.strip():
            raise ValueError(f"invalid package requirement: {item!r}")
        return Requirement(
            name=name.strip(), constraint=parse_constraint(constraint or None)
        )
    if isinstance(item, Mapping):
        if "name" not in item:
            raise ValueError(f"package requirement requires a 'name': {item!r}")
        return Requirement(
            name=str(item["name"]),
            constraint=parse_constraint(item.get("constraint")),
            optional=bool(item.get("optional", False)),
            reason=str(item.get("reason", "")),
        )
    raise ValueError(f"invalid package requirement: {item!r}")


@dataclass(frozen=True)
class Environment:
    """A declarative description of a complete profile.

    An environment states *what must be true*, not *what to run*: the packages
    that must be present, the host facts that must hold, and the variables
    that should exist.  Turning it into installed files is the job of the
    resolver and the profile manager.
    """

    name: str
    packages: tuple = ()
    environment_constraints: tuple = ()
    variables: Mapping = field(default_factory=dict)
    description: str = ""
    includes: tuple = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("environment requires a 'name'")

    def __str__(self) -> str:
        return self.name

    @classmethod
    def from_dict(cls, data: Mapping, name: str = "") -> Environment:
        env_name = str(data.get("name") or name)
        if not env_name:
            raise ValueError("environment requires a 'name'")
        return cls(
            name=env_name,
            packages=tuple(
                _requirement_from(item) for item in data.get("packages", ())
            ),
            environment_constraints=tuple(
                EnvironmentConstraint(
                    kind=str(item.get("kind", "host")),
                    name=str(item["name"]),
                    operator=str(item.get("operator", "==")),
                    value=str(item.get("value", "")),
                    message=str(item.get("message", "")),
                )
                for item in data.get("environment_constraints", ())
            ),
            variables={
                str(key): str(value)
                for key, value in dict(data.get("variables", {})).items()
            },
            description=str(data.get("description", "")),
            includes=tuple(str(item) for item in data.get("includes", ())),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "packages": [req.to_dict() for req in self.packages],
            "environment_constraints": [
                c.to_dict() for c in self.environment_constraints
            ],
            "variables": dict(self.variables),
            "includes": list(self.includes),
        }

    def requirements(self) -> tuple:
        """Merge duplicate requirements for the same name into one range.

        Two requirements for ``openssl`` from different sources become a single
        requirement whose range is the intersection of both.  Order of first
        appearance is preserved so that output is deterministic.
        """
        merged: dict = {}
        order: list = []
        for req in self.packages:
            existing = merged.get(req.name)
            if existing is None:
                merged[req.name] = req
                order.append(req.name)
            else:
                merged[req.name] = replace(
                    existing,
                    constraint=existing.constraint.intersect(req.constraint),
                    optional=existing.optional and req.optional,
                    reason="; ".join(
                        filter(None, [existing.reason, req.reason])
                    ),
                )
        return tuple(merged[name] for name in order)

    def with_packages(self, packages: Iterable[Requirement]) -> Environment:
        return replace(self, packages=tuple(packages))
