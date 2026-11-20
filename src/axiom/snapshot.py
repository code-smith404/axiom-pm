"""Immutable snapshots: the result of resolving an environment.

A snapshot is the complete, self-contained description of an installed
profile: the environment it came from, the exact versions selected, the digest
of every file, the variables to export and the host assertions that were
checked.  It is the unit that gets activated atomically and the unit that
rollback restores.

The snapshot digest is computed over canonical JSON and deliberately excludes
timestamps, hostnames and absolute paths.  The same environment resolved
against the same store therefore yields the same digest on any machine, which
is what makes ``axiom.lock.json`` portable and ``axiom diff`` meaningful.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import AxiomError, EnvironmentError
from .model import (
    Environment,
    EnvironmentConstraint,
    content_digest,
    normalize_mode,
)
from .solver import Solution
from .version import Version, parse_version

__all__ = [
    "LOCKFILE_FORMAT",
    "SNAPSHOT_FORMAT",
    "FileChange",
    "Snapshot",
    "SnapshotDiff",
    "SnapshotFile",
    "SnapshotPackage",
    "snapshot_from_solution",
]

SNAPSHOT_FORMAT = 1
LOCKFILE_FORMAT = 1
LOCKFILE_NAME = "axiom.lock.json"


@dataclass(frozen=True)
class SnapshotFile:
    target: str
    digest: str
    mode: str = "0644"
    size: int = 0
    package: str = ""

    def __post_init__(self) -> None:
        if not self.target or self.target.startswith("/"):
            raise ValueError(f"invalid snapshot file target: {self.target!r}")
        if ".." in PurePosixPath(self.target).parts or "\\" in self.target:
            raise ValueError(f"snapshot file target escapes its root: {self.target!r}")
        if any(ord(char) < 32 for char in self.target):
            raise ValueError(f"invalid snapshot file target: {self.target!r}")
        object.__setattr__(self, "mode", normalize_mode(self.mode))
        object.__setattr__(self, "size", int(self.size))
        digest = str(self.digest)
        digest = digest.removeprefix("sha256:")
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ValueError(f"invalid file digest in snapshot: {self.digest!r}")
        object.__setattr__(self, "digest", digest)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "digest": self.digest,
            "mode": self.mode,
            "size": self.size,
            "package": self.package,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> SnapshotFile:
        return cls(
            target=str(data["target"]),
            digest=str(data["digest"]),
            mode=str(data.get("mode", "0644")),
            size=int(data.get("size", 0)),
            package=str(data.get("package", "")),
        )


@dataclass(frozen=True)
class SnapshotPackage:
    name: str
    version: Version
    digest: str
    artifact: str = ""
    features: tuple = ()
    files: tuple = ()

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": str(self.version),
            "digest": self.digest,
            "artifact": self.artifact,
            "features": list(self.features),
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> SnapshotPackage:
        return cls(
            name=str(data["name"]),
            version=parse_version(str(data["version"])),
            digest=str(data.get("digest", "")),
            artifact=str(data.get("artifact", "")),
            features=tuple(str(item) for item in data.get("features", ())),
            files=tuple(
                SnapshotFile.from_dict(item) for item in data.get("files", ())
            ),
        )


@dataclass(frozen=True)
class Snapshot:
    """A resolved environment, content-addressed by :attr:`digest`."""

    environment: str
    packages: tuple = ()
    variables: Mapping = field(default_factory=dict)
    environment_constraints: tuple = ()
    format: int = SNAPSHOT_FORMAT

    def __post_init__(self) -> None:
        names = set()
        targets = {}
        for package in self.packages:
            if package.name in names:
                raise ValueError(
                    f"snapshot contains {package.name!r} more than once"
                )
            names.add(package.name)
            for record in package.files:
                if record.target in targets:
                    raise ValueError(
                        f"snapshot installs {record.target!r} from both "
                        f"{targets[record.target]} and {package.identity}"
                    )
                targets[record.target] = package.identity

    @property
    def digest(self) -> str:
        return content_digest(self.to_dict())

    def __str__(self) -> str:
        return f"{self.environment}@{self.digest}"

    @property
    def short_digest(self) -> str:
        return self.digest.split(":", 1)[-1][:12]

    def to_dict(self) -> dict:
        packages = []
        for package in sorted(self.packages, key=lambda item: item.name):
            data = package.to_dict()
            data["files"] = [
                record.to_dict()
                for record in sorted(package.files, key=lambda item: item.target)
            ]
            packages.append(data)
        return {
            "format": self.format,
            "environment": self.environment,
            "packages": packages,
            "variables": dict(sorted(self.variables.items())),
            "environment_constraints": [
                constraint.to_dict()
                for constraint in self.environment_constraints
            ],
        }

    def to_json(self, *, pretty: bool = True) -> str:
        if pretty:
            return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        )

    def lockfile_dict(self) -> dict:
        return {
            "lockfile_version": LOCKFILE_FORMAT,
            "digest": self.digest,
            "snapshot": self.to_dict(),
        }

    def write_lockfile(self, path: Path | str) -> Path:
        from .util import atomic_write_text

        path = Path(path)
        atomic_write_text(path, json.dumps(self.lockfile_dict(), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def from_dict(cls, data: Mapping, *, verify_digest: bool = True) -> Snapshot:
        recorded = data.get("digest")
        if "snapshot" in data and "format" not in data:
            data = data["snapshot"]
            recorded = recorded or data.get("digest")
        try:
            snapshot = cls(
                environment=str(data["environment"]),
                packages=tuple(
                    SnapshotPackage.from_dict(item)
                    for item in data.get("packages", ())
                ),
                variables={
                    str(key): str(value)
                    for key, value in dict(data.get("variables", {})).items()
                },
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
                format=int(data.get("format", SNAPSHOT_FORMAT)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EnvironmentError(f"invalid snapshot: {exc}") from None
        if snapshot.format != SNAPSHOT_FORMAT:
            raise EnvironmentError(
                f"snapshot uses unsupported format {snapshot.format!r}; "
                "this version of axiom understands format "
                f"{SNAPSHOT_FORMAT}"
            )
        if verify_digest and recorded and recorded != snapshot.digest:
            raise EnvironmentError(
                "snapshot digest does not match its contents; the lockfile "
                "may be corrupt or was modified by hand"
            )
        return snapshot

    @classmethod
    def read_lockfile(cls, path: Path | str) -> Snapshot:
        path = Path(path)
        try:
            data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            raise EnvironmentError(f"lockfile not found: {path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvironmentError(f"cannot read lockfile {path}: {exc}") from None
        return cls.from_dict(data, verify_digest=True)

    # --------------------------------------------------------------- queries

    def package_map(self) -> dict:
        return {package.name: package for package in self.packages}

    def file_map(self) -> dict:
        return {item.target: item for item in self.files()}

    def files(self) -> tuple:
        records = [
            item
            for package in self.packages
            for item in package.files
        ]
        return tuple(sorted(records, key=lambda item: item.target))

    def total_size(self) -> int:
        return sum(item.size for item in self.files())

    def package_of(self, target: str) -> str:
        record = self.file_map().get(target)
        return record.package if record else ""

    def diff(self, other: Snapshot) -> SnapshotDiff:
        return SnapshotDiff.between(self, other)


@dataclass(frozen=True)
class FileChange:
    action: str  # "add", "remove" or "change"
    target: str
    old: SnapshotFile | None = None
    new: SnapshotFile | None = None

    def __str__(self) -> str:
        return f"{self.action:6} {self.target}"


@dataclass(frozen=True)
class SnapshotDiff:
    from_digest: str
    to_digest: str
    added_packages: tuple = ()
    removed_packages: tuple = ()
    changed_packages: tuple = ()
    file_changes: tuple = ()
    variables_changed: tuple = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.added_packages
            or self.removed_packages
            or self.changed_packages
            or self.file_changes
            or self.variables_changed
        )

    @classmethod
    def between(cls, before: Snapshot, after: Snapshot) -> SnapshotDiff:
        old_packages = before.package_map()
        new_packages = after.package_map()
        added = tuple(sorted(set(new_packages) - set(old_packages)))
        removed = tuple(sorted(set(old_packages) - set(new_packages)))
        changed = tuple(
            sorted(
                name
                for name in set(old_packages) & set(new_packages)
                if old_packages[name].version != new_packages[name].version
                or old_packages[name].digest != new_packages[name].digest
            )
        )
        old_files = before.file_map()
        new_files = after.file_map()
        changes = []
        for target in sorted(set(old_files) | set(new_files)):
            old = old_files.get(target)
            new = new_files.get(target)
            if old is None:
                changes.append(FileChange("add", target, None, new))
            elif new is None:
                changes.append(FileChange("remove", target, old, None))
            elif old.digest != new.digest or old.mode != new.mode:
                changes.append(FileChange("change", target, old, new))
        old_vars = dict(before.variables)
        new_vars = dict(after.variables)
        variable_changes = tuple(
            sorted(
                key
                for key in set(old_vars) | set(new_vars)
                if old_vars.get(key) != new_vars.get(key)
            )
        )
        return cls(
            from_digest=before.digest,
            to_digest=after.digest,
            added_packages=added,
            removed_packages=removed,
            changed_packages=changed,
            file_changes=tuple(changes),
            variables_changed=variable_changes,
        )

    def render(self) -> str:
        lines = []
        for name in self.added_packages:
            lines.append(f"  + package {name}")
        for name in self.removed_packages:
            lines.append(f"  - package {name}")
        for name in self.changed_packages:
            lines.append(f"  ~ package {name}")
        for change in self.file_changes:
            lines.append(f"  {change}")
        for key in self.variables_changed:
            lines.append(f"  ~ variable {key}")
        if not lines:
            lines.append("  (identical)")
        return "\n".join(lines)


def snapshot_from_solution(
    environment: Environment,
    solution: Solution,
    lookup,
) -> Snapshot:
    """Turn a solver result into a snapshot.

    ``lookup`` maps a selected :class:`~axiom.model.Package` to a store entry
    carrying the file records for that package.  Duplicate install targets are
    rejected here: two packages may not own the same path in one profile.
    """
    packages = []
    targets: dict = {}
    for package in sorted(solution.packages, key=lambda item: item.name):
        entry = lookup(package)
        records = []
        for record in getattr(entry, "files", ()):
            if record.target in targets:
                raise AxiomError(
                    f"install conflict: {package.identity} and "
                    f"{targets[record.target]} both install {record.target!r}"
                )
            targets[record.target] = package.identity
            records.append(
                SnapshotFile(
                    target=record.target,
                    digest=_bare(record.digest),
                    mode=record.mode,
                    size=record.size,
                    package=package.identity,
                )
            )
        packages.append(
            SnapshotPackage(
                name=package.name,
                version=package.version,
                digest=getattr(entry, "content_digest", ""),
                artifact=getattr(entry, "artifact_digest", ""),
                features=tuple(solution.features.get(package.name, ())),
                files=tuple(sorted(records, key=lambda item: item.target)),
            )
        )

    # A file cannot also be the directory of another installed file.
    for target, owner in targets.items():
        parts = target.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in targets:
                raise AxiomError(
                    f"install conflict: {targets[parent]} installs the file "
                    f"{parent!r}, but {owner} installs {target!r} inside it"
                )

    return Snapshot(
        environment=environment.name,
        packages=tuple(packages),
        variables=dict(environment.variables),
        environment_constraints=tuple(environment.environment_constraints),
    )


def _bare(digest: str) -> str:
    text = str(digest)
    return text.removeprefix("sha256:")
