"""Turning an environment into an active profile.

This module is the seam between the pure parts (model, solver, snapshot) and
the mutable parts (store, profile).  It performs the steps in the only order
that can be made crash-safe:

1. check host constraints, before touching anything;
2. resolve every requirement to a concrete version;
3. build the immutable snapshot and verify that its blobs exist;
4. materialise a new generation and atomically flip ``current``.

A lockfile can pin the snapshot.  ``--locked`` is strict: the environment is
resolved again and compared with the lockfile, so adding a dependency without
updating the lock fails instead of silently installing something different.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import EnvironmentError, StoreError
from .host import check_constraints, collect_facts, facts_used_by
from .manifest import load_environment
from .model import Environment, Package
from .profile import Generation, Profile
from .snapshot import LOCKFILE_NAME, Snapshot, snapshot_from_solution
from .solver import Resolver, Solution
from .store import Store

__all__ = [
    "InstallResult",
    "Plan",
    "install_environment",
    "install_snapshot",
    "plan_environment",
    "resolve_environment",
]

DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class Plan:
    """Everything needed to install, computed without side effects."""

    environment: Environment
    solution: Solution
    snapshot: Snapshot
    facts: Mapping = field(default_factory=dict)
    host_problems: tuple = ()

    @property
    def ok(self) -> bool:
        return not self.host_problems


@dataclass(frozen=True)
class InstallResult:
    plan: Plan
    generation: Generation
    changed: bool

    @property
    def snapshot(self) -> Snapshot:
        return self.plan.snapshot


def resolve_environment(
    environment: Environment,
    store: Store,
    *,
    allow_prerelease: bool = False,
    facts: dict | None = None,
) -> Plan:
    """Resolve ``environment`` and build its snapshot, without installing."""
    facts = facts if facts is not None else collect_facts()
    host_problems, facts = check_constraints(
        environment.environment_constraints, facts
    )
    solution = Resolver(store, allow_prerelease=allow_prerelease).solve(
        environment.requirements(), environment=environment.name
    )
    for package in solution.packages:
        problems, _ = check_constraints(package.environment_constraints, facts)
        host_problems.extend(
            f"{package.identity}: {problem}" for problem in problems
        )
    def lookup(package: Package):
        entry = store.get_entry(package.name, package.version)
        if entry is None:
            raise StoreError(
                f"{package.identity} disappeared from the store during resolution"
            )
        return entry

    snapshot = snapshot_from_solution(environment, solution, lookup)
    missing = [
        record.target
        for record in snapshot.files()
        if not store.has_blob(record.digest)
    ]
    if missing:
        raise StoreError(
            "snapshot refers to content that is missing from the store: "
            + ", ".join(sorted(missing)[:5])
        )
    used = facts_used_by(environment.environment_constraints, facts)
    for package in solution.packages:
        for key, value in facts_used_by(package.environment_constraints, facts).items():
            used.setdefault(key, {}).update(value)
    return Plan(
        environment=environment,
        solution=solution,
        snapshot=snapshot,
        facts=used,
        host_problems=tuple(host_problems),
    )


def plan_environment(
    environment: Environment | Path | str,
    store: Store,
    *,
    allow_prerelease: bool = False,
) -> Plan:
    if not isinstance(environment, Environment):
        environment = load_environment(environment)
    return resolve_environment(
        environment, store, allow_prerelease=allow_prerelease
    )


def install_environment(
    environment: Environment | Path | str,
    store: Store,
    profile: Profile,
    *,
    allow_prerelease: bool = False,
    ignore_host: bool = False,
    force: bool = False,
    verify_blobs: bool = False,
    write_lock: Path | str | None = None,
) -> InstallResult:
    """Resolve, optionally pin and activate an environment."""
    if not isinstance(environment, Environment):
        environment = load_environment(environment)
    plan = resolve_environment(
        environment, store, allow_prerelease=allow_prerelease
    )
    if plan.host_problems and not ignore_host:
        raise EnvironmentError(
            f"environment {environment.name!r} does not match this host",
            details=list(plan.host_problems),
        )
    if write_lock is not None:
        plan.snapshot.write_lockfile(write_lock)
    generation, changed = profile.install(
        plan.snapshot,
        store,
        facts=plan.facts,
        force=force,
        verify_blobs=verify_blobs,
    )
    return InstallResult(plan=plan, generation=generation, changed=changed)


def install_snapshot(
    snapshot: Snapshot,
    store: Store,
    profile: Profile,
    *,
    environment: Environment | None = None,
    verify_blobs: bool = False,
    force: bool = False,
    allow_prerelease: bool = False,
) -> Generation:
    """Activate a pinned snapshot, optionally checking it against an environment."""
    facts = {}
    if environment is not None:
        current_plan = resolve_environment(
            environment, store, allow_prerelease=allow_prerelease
        )
        facts = dict(current_plan.facts)
        if current_plan.snapshot.digest != snapshot.digest:
            raise EnvironmentError(
                f"lockfile is out of date for environment {environment.name!r}: "
                "the environment resolves to a different snapshot; run "
                "`axiom lock` to update it"
            )
        if current_plan.host_problems:
            raise EnvironmentError(
                f"environment {environment.name!r} does not match this host",
                details=list(current_plan.host_problems),
            )
    for record in snapshot.files():
        if not store.has_blob(record.digest):
            raise StoreError(
                f"lockfile references missing content for {record.target!r}"
            )
    generation, _changed = profile.install(
        snapshot, store, facts=facts, verify_blobs=verify_blobs, force=force
    )
    return generation


def default_lockfile(environment_path: Path | str) -> Path:
    return Path(environment_path).resolve().parent / LOCKFILE_NAME
