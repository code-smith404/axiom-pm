"""Loading declarative TOML: environments, package definitions, lockfiles.

TOML is the authoring format because it is pleasant to hand-edit and designed
to be unambiguous.  The loaders here normalise both the ``[packages]`` table
form and the ``[[packages]]`` array form into the same immutable model, resolve
``includes`` relative to the including file, and turn every syntax or schema
problem into an :class:`EnvironmentError` that names the file and the key.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

from .errors import EnvironmentError
from .model import (
    Dependency,
    Environment,
    EnvironmentConstraint,
    FileSpec,
    Package,
    Requirement,
)
from .version import parse_constraint, parse_version

__all__ = [
    "load_environment",
    "load_environment_data",
    "load_package",
    "load_toml",
    "package_from_dict",
]

MAX_INCLUDE_DEPTH = 16


def load_toml(path: Path | str) -> dict:
    """Read a TOML file, reporting failures with the file name."""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise EnvironmentError(f"file not found: {path}") from None
    except IsADirectoryError:
        raise EnvironmentError(f"expected a TOML file, got a directory: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise EnvironmentError(f"invalid TOML in {path}: {exc}") from None
    except OSError as exc:
        raise EnvironmentError(f"cannot read {path}: {exc}") from None


def load_environment(path: Path | str) -> Environment:
    """Load an environment file, resolving ``includes`` recursively."""
    path = Path(path)
    data = load_toml(path)
    return load_environment_data(data, name=path.stem, source=path)


def load_environment_data(
    data: Mapping,
    *,
    name: str = "",
    source: Path | None = None,
    _stack: tuple[Path, ...] = (),
) -> Environment:
    if source is not None:
        resolved = source.resolve()
        if resolved in _stack:
            chain = " -> ".join(str(item) for item in (*_stack, resolved))
            raise EnvironmentError(f"include cycle detected: {chain}")
        if len(_stack) >= MAX_INCLUDE_DEPTH:
            raise EnvironmentError(
                f"environment includes nested more than {MAX_INCLUDE_DEPTH} levels"
            )
        _stack = (*_stack, resolved)

    packages = [
        _normalize_requirement(item, source)
        for item in _sequence(data.get("packages"), "packages", source)
    ]
    constraints = [
        _constraint_from(item)
        for item in _sequence(
            data.get("environment_constraints"),
            "environment_constraints",
            source,
        )
    ]

    normalised = dict(data)
    normalised["packages"] = [req.to_dict() for req in packages]
    normalised["environment_constraints"] = [
        item.to_dict() for item in constraints
    ]
    try:
        environment = Environment.from_dict(normalised, name=name)
    except (KeyError, TypeError, ValueError) as exc:
        location = f" in {source}" if source else ""
        raise EnvironmentError(f"invalid environment{location}: {exc}") from None

    # A diamond include graph can mention the same constraint twice; the
    # environment is a conjunction, so duplicates are removed before hashing.
    unique_constraints = []
    seen_constraints = set()
    for constraint in constraints:
        if constraint in seen_constraints:
            continue
        seen_constraints.add(constraint)
        unique_constraints.append(constraint)
    constraints = unique_constraints

    variables: dict = {}
    includes: list = []

    base_dir = source.parent if source else Path.cwd()
    for include in environment.includes:
        include_path = Path(include)
        if not include_path.is_absolute():
            include_path = base_dir / include_path
        included_data = load_toml(include_path)
        included = load_environment_data(
            included_data,
            name=include_path.stem,
            source=include_path,
            _stack=_stack,
        )
        packages.extend(included.packages)
        constraints.extend(included.environment_constraints)
        variables.update(included.variables)
        includes.extend(included.includes)

    variables.update(environment.variables)
    merged = Environment(
        name=environment.name,
        packages=tuple(packages),
        environment_constraints=tuple(constraints),
        variables=variables,
        description=environment.description,
        includes=tuple(includes),
    )
    return merged


def _sequence(value, key: str, source: Path | None) -> list:
    """Accept both a TOML array and a TOML table for repeated entries."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, Mapping):
        items = []
        for name, entry in value.items():
            if isinstance(entry, str):
                items.append({"name": name, "constraint": entry})
            elif isinstance(entry, Mapping):
                item = dict(entry)
                item.setdefault("name", name)
                items.append(item)
            elif entry is None:
                items.append({"name": name})
            else:
                raise EnvironmentError(
                    f"invalid entry for {key}.{name}: expected a string or table"
                )
        return items
    raise EnvironmentError(f"{key} must be an array or a table")


def _normalize_requirement(item, source: Path | None) -> Requirement:
    if isinstance(item, str):
        name, _, constraint = item.partition(" ")
        return Requirement(name=name.strip(), constraint=parse_constraint(constraint))
    if isinstance(item, Mapping):
        try:
            return Requirement(
                name=str(item["name"]),
                constraint=parse_constraint(item.get("constraint")),
                optional=bool(item.get("optional", False)),
                reason=str(item.get("reason", "")),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise EnvironmentError(f"invalid package requirement {item!r}: {exc}") from None
    raise EnvironmentError(f"invalid package requirement: {item!r}")


def load_package(path: Path | str) -> Package:
    """Load a ``package.toml`` definition."""
    path = Path(path)
    data = load_toml(path)
    try:
        return package_from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvironmentError(f"invalid package definition in {path}: {exc}") from None


def package_from_dict(data: Mapping) -> Package:
    """Normalise a package description, accepting table or array forms."""
    name = data.get("name")
    if not name:
        raise EnvironmentError("package definition requires a 'name'")
    raw_version = data.get("version")
    if not raw_version:
        raise EnvironmentError(f"package {name!r} requires a 'version'")

    provides = data.get("provides", ())
    if isinstance(provides, str):
        provides = [provides]
    scripts = data.get("scripts", {})
    if not isinstance(scripts, Mapping):
        raise EnvironmentError("scripts must be a table")

    return Package(
        name=str(name),
        version=parse_version(str(raw_version)),
        dependencies=tuple(
            _dependency_from(item)
            for item in _sequence(
                data.get("dependencies"), "dependencies", None
            )
        ),
        description=str(data.get("description", "")),
        homepage=str(data.get("homepage", "")),
        license=str(data.get("license", "")),
        provides=tuple(str(item) for item in provides),
        conflicts=tuple(
            _dependency_from(item)
            for item in _sequence(data.get("conflicts"), "conflicts", None)
        ),
        files=tuple(
            _file_from(item)
            for item in _sequence(data.get("files"), "files", None)
        ),
        environment_constraints=tuple(
            _constraint_from(item)
            for item in _sequence(
                data.get("environment_constraints"),
                "environment_constraints",
                None,
            )
        ),
        scripts={str(key): str(value) for key, value in scripts.items()},
    )


def _dependency_from(item) -> Dependency:
    if isinstance(item, str):
        name, _, constraint = item.partition(" ")
        return Dependency(name=name.strip(), constraint=parse_constraint(constraint))
    if isinstance(item, Mapping):
        return Dependency(
            name=str(item["name"]),
            constraint=parse_constraint(item.get("constraint")),
            optional=bool(item.get("optional", False)),
            features=tuple(str(feature) for feature in item.get("features", ())),
        )
    raise EnvironmentError(f"invalid dependency: {item!r}")


def _file_from(item) -> FileSpec:
    if isinstance(item, str):
        return FileSpec(source=item, target=item)
    if isinstance(item, Mapping):
        return FileSpec(
            source=str(item["source"]),
            target=str(item.get("target", item["source"])),
            mode=item.get("mode", "0644"),
        )
    raise EnvironmentError(f"invalid file entry: {item!r}")


def _constraint_from(item) -> EnvironmentConstraint:
    if isinstance(item, Mapping):
        if "name" not in item:
            raise EnvironmentError(f"environment constraint requires a name: {item!r}")
        return EnvironmentConstraint(
            kind=str(item.get("kind", "host")),
            name=str(item["name"]),
            operator=str(item.get("operator", "==")),
            value=str(item.get("value", "")),
            message=str(item.get("message", "")),
        )
    raise EnvironmentError(f"invalid environment constraint: {item!r}")


def environment_from_requirements(
    name: str, requirements: Iterable[Requirement], variables: Mapping | None = None
) -> Environment:
    """Build an ad-hoc environment, used by ``axiom install pkg@version``."""
    return Environment(
        name=name,
        packages=tuple(requirements),
        variables=dict(variables or {}),
    )
