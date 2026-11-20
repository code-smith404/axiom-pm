"""Shared fixtures for the axiom tests."""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from axiom.store import Store


def toml_string(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_package(
    directory: Path | str,
    name: str,
    version: str,
    *,
    files: dict | None = None,
    dependencies=(),
    provides=(),
    conflicts=(),
    environment_constraints=(),
    explicit_files: bool = True,
    description: str = "",
    auto_files: bool = False,
) -> Path:
    """Write a package directory and return its path.

    ``files`` maps a payload-relative path to either bytes/str or a
    ``(content, mode)`` pair.  When ``auto_files`` is true, the payload is
    discovered by the builder instead of being declared in ``package.toml``.
    """
    directory = Path(directory)
    payload = directory / "files"
    payload.mkdir(parents=True, exist_ok=True)

    lines = [f"name = {toml_string(name)}", f"version = {toml_string(version)}"]
    if description:
        lines.append(f"description = {toml_string(description)}")
    if provides:
        lines.append(
            "provides = ["
            + ", ".join(toml_string(item) for item in provides)
            + "]"
        )
    if conflicts:
        lines.append(
            "conflicts = ["
            + ", ".join(toml_string(item) for item in conflicts)
            + "]"
        )

    for dependency in dependencies:
        if isinstance(dependency, str):
            dep_name, _, constraint = dependency.partition(" ")
            dep = {"name": dep_name}
            if constraint:
                dep["constraint"] = constraint
        else:
            dep = dict(dependency)
        lines.append("")
        lines.append("[[dependencies]]")
        lines.append(f"name = {toml_string(dep['name'])}")
        if dep.get("constraint"):
            lines.append(f"constraint = {toml_string(dep['constraint'])}")
        if dep.get("optional"):
            lines.append("optional = true")

    for constraint in environment_constraints:
        lines.append("")
        lines.append("[[environment_constraints]]")
        for key in ("kind", "name", "operator", "value", "message"):
            if key in constraint:
                lines.append(f"{key} = {toml_string(constraint[key])}")

    if files is None:
        files = {f"share/{name}.txt": f"{name} {version}\n"}
    for target, spec in files.items():
        if isinstance(spec, tuple):
            content, mode = spec
        else:
            content, mode = spec, "0644"
        path = payload / target
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        os.chmod(path, int(str(mode), 8))
        if explicit_files and not auto_files:
            lines.append("")
            lines.append("[[files]]")
            lines.append(f"source = {toml_string(target)}")
            lines.append(f"target = {toml_string(target)}")
            lines.append(f"mode = {toml_string(str(mode))}")

    (directory / "package.toml").write_text("\n".join(lines) + "\n", "utf-8")
    return directory


def write_environment(
    path: Path | str,
    name: str,
    packages: dict | list | None = None,
    *,
    includes=(),
    variables: dict | None = None,
    constraints=(),
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"name = {toml_string(name)}"]
    if includes:
        lines.append(
            "includes = ["
            + ", ".join(toml_string(item) for item in includes)
            + "]"
        )
    lines.append("")
    lines.append("[packages]")
    if isinstance(packages, dict):
        for package_name, constraint in packages.items():
            lines.append(f"{toml_string(package_name)} = {toml_string(constraint)}")
    elif packages:
        for item in packages:
            lines.append(f"{toml_string(item)} = \"*\"")
    if variables:
        lines.append("")
        lines.append("[variables]")
        for key, value in variables.items():
            lines.append(f"{toml_string(key)} = {toml_string(value)}")
    for constraint in constraints:
        lines.append("")
        lines.append("[[environment_constraints]]")
        for key in ("kind", "name", "operator", "value", "message"):
            if key in constraint:
                lines.append(f"{key} = {toml_string(constraint[key])}")
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def publish(store: Store, directory: Path | str):
    return store.ingest_directory(directory)


def run_cli(argv, root, env: dict | None = None):
    """Run the CLI in-process, returning ``(code, stdout, stderr)``."""
    from axiom.cli import main

    old_environ = os.environ.copy()
    if env:
        os.environ.update(env)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--root", str(root)] + [str(item) for item in argv])
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
    return code, out.getvalue(), err.getvalue()
