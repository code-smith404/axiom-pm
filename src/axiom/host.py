"""Host facts and the evaluation of environment constraints.

Environment constraints describe the machine a profile is allowed to run on:
an operating system, an architecture, a kernel version, an environment
variable.  They are checked *before* anything is materialised, so an
incompatible environment fails cleanly instead of producing a broken profile.

Facts are collected into namespaces: ``host``, ``os``, ``arch``, ``kernel``,
``python``, ``env`` and ``axiom``.  Only the values referenced by constraints
are recorded in generation metadata; environment variables are not copied into
the store wholesale.
"""

from __future__ import annotations

import getpass
import os
import platform
import struct
import sys
from pathlib import Path

__all__ = [
    "check_constraints",
    "collect_facts",
    "facts_used_by",
    "normalise_arch",
]


def normalise_arch(machine: str) -> str:
    text = machine.lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "i386": "x86",
        "i686": "x86",
        "arm64": "aarch64",
        "armv8l": "aarch64",
    }
    return aliases.get(text, text)


def _os_release() -> dict:
    data = {}
    path = Path("/etc/os-release")
    if not path.is_file():
        path = Path("/usr/lib/os-release")
    try:
        for line in path.read_text("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def _is_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def collect_facts(*, include_env: bool = True) -> dict:
    """Collect host facts into namespaces used by environment constraints."""
    os_release = _os_release()
    system = platform.system() or "unknown"
    machine = normalise_arch(platform.machine() or "unknown")
    bits = str(struct.calcsize("P") * 8)
    python_version = platform.python_version()

    from . import __version__

    facts = {
        "host": {
            "name": platform.node() or "unknown",
            "user": _username(),
            "uid": str(os.getuid()) if hasattr(os, "getuid") else "",
            "container": "true" if _is_container() else "false",
            "python": python_version,
        },
        "os": {
            "system": system.lower(),
            "id": os_release.get("ID", system.lower()),
            "version": os_release.get("VERSION_ID", ""),
            "codename": os_release.get("VERSION_CODENAME", ""),
            "family": os_release.get("ID_LIKE", ""),
            "name": os_release.get("NAME", system),
        },
        "arch": {
            "machine": machine,
            "bits": bits,
            "endianness": sys.byteorder,
        },
        "kernel": {
            "name": system.lower(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "python": {
            "version": python_version,
            "major": str(sys.version_info.major),
            "minor": str(sys.version_info.minor),
            "implementation": platform.python_implementation(),
        },
        "axiom": {
            "version": __version__,
        },
    }
    # `host.os` and `host.arch` are friendly aliases for the common cases.
    facts["host"]["os"] = facts["os"]["id"]
    facts["host"]["arch"] = machine
    facts["host"]["kernel"] = facts["kernel"]["release"]
    if include_env:
        facts["env"] = {key: value for key, value in os.environ.items()}
    return facts


def _username() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # pragma: no cover - exotic environments
        return "unknown"


def check_constraints(
    constraints, facts: dict | None = None
) -> tuple:
    """Return ``(problems, facts)`` for a sequence of constraints.

    ``problems`` is a list of human-readable strings; an empty list means the
    host satisfies every constraint.
    """
    if facts is None:
        facts = collect_facts()
    problems = []
    for constraint in constraints:
        satisfied, detail = constraint.evaluate(facts)
        if not satisfied:
            problems.append(detail)
    return problems, facts


def facts_used_by(constraints, facts: dict) -> dict:
    """Extract only the fact values referenced by ``constraints``."""
    used: dict = {}
    for constraint in constraints:
        namespace = facts.get(constraint.kind, {})
        if constraint.name in namespace:
            used.setdefault(constraint.kind, {})[constraint.name] = namespace[
                constraint.name
            ]
    return used
