"""Command-line interface.

The CLI is thin on purpose: every command either reads declared state and
prints it, or delegates a single side effect to a module that knows how to do
it atomically.  Errors are rendered without tracebacks unless ``--debug`` is
passed, and every failure has a stable exit code:

===  ==========================================================
0    success
1    invalid input, missing data or a refused operation
2    command-line usage error
3    unsatisfiable dependency constraints
4    verification found drift
130  interrupted
===  ==========================================================
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .artifact import build_artifact
from .errors import AxiomError, EnvironmentError, ProfileError, VerificationError
from .host import check_constraints
from .installer import (
    DEFAULT_PROFILE,
    InstallResult,
    default_lockfile,
    install_environment,
    install_snapshot,
    plan_environment,
)
from .manifest import load_environment
from .profile import Profile
from .snapshot import LOCKFILE_NAME, Snapshot
from .store import Store
from .util import atomic_write_text, default_root, format_bytes, short_digest
from .version import parse_constraint

# --------------------------------------------------------------------- helpers


def _common() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--root",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="store root (default: $AXIOM_ROOT or ~/.axiom)",
    )
    parser.add_argument(
        "--profile",
        "-p",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="profile to operate on (default: $AXIOM_PROFILE or 'default')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit machine-readable JSON",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        help="increase verbosity",
    )
    return parser


def _root(args) -> Path:
    root = getattr(args, "root", None) or default_root()
    return Path(root).expanduser()


def _store(args, *, initialize: bool = True) -> Store:
    store = Store(_root(args))
    if initialize:
        store.initialize()
    return store


def _profile(args, name: str | None = None) -> Profile:
    profile_name = name or getattr(args, "profile", None) or os.environ.get(
        "AXIOM_PROFILE"
    ) or DEFAULT_PROFILE
    return Profile(_root(args), profile_name).initialize()


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _table(headers: list, rows: list) -> str:
    widths = [len(header) for header in headers]
    str_rows = [[str(cell) for cell in row] for row in rows]
    for row in str_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in str_rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def _parse_spec(text: str) -> tuple:
    """Split ``name@range`` into a name and an optional range."""
    if "@" in text and not text.startswith("@"):
        name, _, constraint = text.rpartition("@")
        if name:
            return name, parse_constraint(constraint or None)
    return text, None


def _package_payload(package) -> dict:
    return {
        "name": package.name,
        "version": str(package.version),
        "description": package.description,
        "license": package.license,
        "homepage": package.homepage,
        "dependencies": [
            {
                "name": dep.name,
                "constraint": str(dep.constraint),
                "optional": dep.optional,
            }
            for dep in package.dependencies
        ],
        "provides": list(package.provides),
    }


def _plan_payload(plan) -> dict:
    return {
        "environment": plan.environment.name,
        "snapshot": plan.snapshot.digest,
        "packages": [
            {
                "name": package.name,
                "version": str(package.version),
                "digest": package.digest,
                "files": len(package.files),
                "size": sum(item.size for item in package.files),
            }
            for package in plan.snapshot.packages
        ],
        "host_problems": list(plan.host_problems),
        "file_count": len(plan.snapshot.files()),
        "total_size": plan.snapshot.total_size(),
    }


def _print_plan(plan) -> None:
    for problem in plan.host_problems:
        print(f"warning: host constraint not satisfied: {problem}", file=sys.stderr)
    rows = [
        (
            package.name,
            str(package.version),
            short_digest(package.digest),
            len(package.files),
            format_bytes(sum(item.size for item in package.files)),
        )
        for package in plan.snapshot.packages
    ]
    print(_table(["NAME", "VERSION", "DIGEST", "FILES", "SIZE"], rows))
    print()
    print(
        f"{len(plan.snapshot.packages)} packages, "
        f"{len(plan.snapshot.files())} files, "
        f"{format_bytes(plan.snapshot.total_size())}"
    )
    print(f"snapshot: {plan.snapshot.digest}")


def _snapshot_from_reference(args, reference: str, *, store: Store) -> Snapshot:
    path = Path(reference)
    if path.is_file():
        if path.suffix == ".toml":
            environment = load_environment(path)
            return plan_environment(environment, store).snapshot
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvironmentError(f"cannot read snapshot {path}: {exc}") from None
        return Snapshot.from_dict(data)
    profile = _profile(args)
    return profile.resolve_reference(reference).snapshot


# -------------------------------------------------------------------- commands


def cmd_init(args) -> int:
    store = Store(_root(args)).initialize()
    payload = {
        "root": str(store.root),
        "directories": {
            "blobs": str(store.blobs),
            "packages": str(store.packages),
            "artifacts": str(store.artifacts),
            "profiles": str(store.profiles),
        },
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"initialised axiom store at {store.root}")
    return 0


def cmd_build(args) -> int:
    artifact = build_artifact(args.path, args.output)
    payload = {
        "identity": artifact.package.identity,
        "path": str(artifact.path),
        "artifact_digest": artifact.artifact_digest,
        "content_digest": artifact.content_digest,
        "files": [record.to_dict() for record in artifact.files],
        "size": artifact.total_size,
    }
    if args.publish:
        store = _store(args)
        entry = store.ingest_artifact(artifact.path)
        payload["published"] = True
        payload["store_digest"] = entry.content_digest
    if args.json:
        _emit_json(payload)
    else:
        print(f"built {artifact.package.identity}")
        print(f"  artifact  {artifact.path}")
        print(f"  sha256    {artifact.artifact_digest}")
        print(f"  content   {artifact.content_digest}")
        print(f"  files     {len(artifact.files)} ({format_bytes(artifact.total_size)})")
        if args.publish:
            print("  published to the local store")
    return 0


def cmd_publish(args) -> int:
    store = _store(args)
    published = []
    for target in args.targets:
        path = Path(target)
        if path.is_dir():
            entry = store.ingest_directory(path, expected_digest=args.digest)
        else:
            entry = store.ingest_artifact(path, expected_digest=args.digest)
        published.append(entry)
    payload = [
        {
            "identity": entry.identity,
            "content_digest": entry.content_digest,
            "artifact": entry.artifact_digest,
            "files": len(entry.files),
        }
        for entry in published
    ]
    if args.json:
        _emit_json(payload)
    else:
        for entry in published:
            print(
                f"published {entry.identity} "
                f"({len(entry.files)} files, {short_digest(entry.content_digest)})"
            )
    return 0


def cmd_search(args) -> int:
    store = _store(args)
    query = (args.query or "").lower()
    grouped: dict = {}
    for entry in store.entries():
        package = entry.package
        if query and query not in package.name.lower() and query not in package.description.lower():
            continue
        group = grouped.setdefault(
            package.name,
            {
                "name": package.name,
                "description": package.description,
                "versions": [],
            },
        )
        if package.description and not group["description"]:
            group["description"] = package.description
        group["versions"].append(str(package.version))
    results = [grouped[name] for name in sorted(grouped)]
    for result in results:
        result["versions"].sort(reverse=True)
    if args.json:
        _emit_json(results)
    else:
        if not results:
            print("no matching packages")
        else:
            print(
                _table(
                    ["NAME", "VERSIONS", "DESCRIPTION"],
                    [
                        (
                            result["name"],
                            ", ".join(result["versions"]),
                            result["description"][:50],
                        )
                        for result in results
                    ],
                )
            )
    return 0


def cmd_info(args) -> int:
    store = _store(args)
    name, version_range = _parse_spec(args.package)
    candidates = [entry for entry in store.candidates(name)]
    if version_range is not None:
        candidates = [
            entry
            for entry in candidates
            if version_range.matches(entry.package.version, allow_prerelease=True)
        ]
    if not candidates:
        raise AxiomError(f"package not found in the store: {args.package}")
    entry = candidates[0]
    payload = _package_payload(entry.package)
    payload.update(
        {
            "content_digest": entry.content_digest,
            "artifact": entry.artifact_digest,
            "files": [record.to_dict() for record in entry.files],
            "total_size": sum(record.size for record in entry.files),
        }
    )
    if args.json:
        _emit_json(payload)
        return 0
    print(f"{entry.package.name} {entry.package.version}")
    if entry.package.description:
        print(f"  {entry.package.description}")
    if entry.package.license:
        print(f"  license: {entry.package.license}")
    if entry.package.homepage:
        print(f"  homepage: {entry.package.homepage}")
    print(f"  content: {entry.content_digest}")
    if entry.package.dependencies:
        print("  dependencies:")
        for dependency in entry.package.dependencies:
            print(f"    {dependency}")
    if entry.package.provides:
        print(f"  provides: {', '.join(entry.package.provides)}")
    if entry.package.conflicts:
        print(f"  conflicts: {', '.join(str(item) for item in entry.package.conflicts)}")
    print(f"  files ({len(entry.files)}, {format_bytes(payload['total_size'])}):")
    for record in entry.files:
        print(f"    {record.mode} {record.target} ({format_bytes(record.size)})")
    return 0


def cmd_resolve(args) -> int:
    store = _store(args)
    plan = plan_environment(
        load_environment(args.environment),
        store,
        allow_prerelease=args.prerelease,
    )
    if args.json:
        _emit_json(_plan_payload(plan))
    else:
        _print_plan(plan)
    return 0


def cmd_lock(args) -> int:
    store = _store(args)
    path = Path(args.environment)
    plan = plan_environment(
        load_environment(path), store, allow_prerelease=args.prerelease
    )
    output = Path(args.output) if args.output else default_lockfile(path)
    plan.snapshot.write_lockfile(output)
    payload = {
        "lockfile": str(output),
        "snapshot": plan.snapshot.digest,
        "environment": plan.environment.name,
        "packages": len(plan.snapshot.packages),
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"wrote {output}")
        print(f"snapshot: {plan.snapshot.digest}")
    return 0


def cmd_install(args) -> int:
    store = _store(args)
    environment = load_environment(args.environment)
    profile_name = (
        getattr(args, "profile", None)
        or os.environ.get("AXIOM_PROFILE")
        or environment.name
    )
    profile = _profile(args, profile_name)
    lockfile = Path(args.lockfile) if args.lockfile else default_lockfile(args.environment)

    if args.locked:
        if not lockfile.is_file():
            raise EnvironmentError(
                f"--locked requested but no lockfile exists at {lockfile}"
            )
        pinned = Snapshot.read_lockfile(lockfile)
        if args.dry_run:
            payload = {
                "profile": profile.name,
                "lockfile": str(lockfile),
                "snapshot": pinned.digest,
                "dry_run": True,
                "packages": [
                    {"name": package.name, "version": str(package.version)}
                    for package in pinned.packages
                ],
            }
            if args.json:
                _emit_json(payload)
            else:
                print(f"would install locked snapshot {pinned.digest}")
                print(f"  profile: {profile.name}")
                print(f"  packages: {len(pinned.packages)}")
            return 0
        before = profile.current_generation()
        generation = install_snapshot(
            pinned,
            store,
            profile,
            environment=environment,
            verify_blobs=args.verify_blobs,
            force=args.force,
            allow_prerelease=args.prerelease,
        )
        changed = before is None or before.snapshot.digest != generation.snapshot.digest
        plan = None
    elif args.dry_run:
        plan = plan_environment(
            environment, store, allow_prerelease=args.prerelease
        )
        payload = _plan_payload(plan)
        payload.update({"profile": profile.name, "dry_run": True})
        if args.json:
            _emit_json(payload)
        else:
            print(f"would install into profile {profile.name!r}")
            _print_plan(plan)
        return 0
    else:
        result: InstallResult = install_environment(
            environment,
            store,
            profile,
            allow_prerelease=args.prerelease,
            ignore_host=args.ignore_host,
            force=args.force,
            verify_blobs=args.verify_blobs,
            write_lock=lockfile if args.lock else None,
        )
        generation = result.generation
        changed = result.changed
        plan = result.plan

    payload = {
        "profile": profile.name,
        "generation": generation.name,
        "snapshot": generation.snapshot.digest,
        "environment": generation.snapshot.environment,
        "changed": changed,
        "packages": [
            {"name": package.name, "version": str(package.version)}
            for package in generation.snapshot.packages
        ],
        "files": len(generation.snapshot.files()),
        "lockfile": str(lockfile) if args.lock or args.locked else None,
    }
    if args.json:
        _emit_json(payload)
    else:
        if changed:
            print(
                f"activated profile {profile.name!r} generation {generation.name}"
            )
        else:
            print(f"profile {profile.name!r} is already up to date")
        print(f"  snapshot: {generation.snapshot.digest}")
        print(
            f"  packages: {len(generation.snapshot.packages)}, "
            f"files: {len(generation.snapshot.files())}"
        )
        print(f"  path:     {profile.current_link}")
        if args.lock:
            print(f"  lockfile: {lockfile}")
    return 0


def cmd_list(args) -> int:
    store = _store(args)
    if args.store:
        entries = list(store.entries())
        payload = [
            {
                "name": entry.package.name,
                "version": str(entry.package.version),
                "digest": entry.content_digest,
                "files": len(entry.files),
            }
            for entry in entries
        ]
        if args.json:
            _emit_json(payload)
        elif not payload:
            print("the store contains no packages")
        else:
            print(
                _table(
                    ["NAME", "VERSION", "FILES", "DIGEST"],
                    [
                        (
                            item["name"],
                            item["version"],
                            item["files"],
                            short_digest(item["digest"]),
                        )
                        for item in payload
                    ],
                )
            )
        return 0

    profile = _profile(args)
    generation = profile.current_generation()
    if generation is None:
        if args.json:
            _emit_json({"profile": profile.name, "active": False, "packages": []})
        else:
            print(f"profile {profile.name!r} has no active generation")
        return 0
    payload = {
        "profile": profile.name,
        "active": True,
        "generation": generation.name,
        "snapshot": generation.snapshot.digest,
        "packages": [
            {
                "name": package.name,
                "version": str(package.version),
                "digest": package.digest,
                "files": len(package.files),
            }
            for package in generation.snapshot.packages
        ],
    }
    if args.json:
        _emit_json(payload)
    else:
        print(
            _table(
                ["NAME", "VERSION", "FILES", "DIGEST"],
                [
                    (
                        item["name"],
                        item["version"],
                        item["files"],
                        short_digest(item["digest"]),
                    )
                    for item in payload["packages"]
                ],
            )
        )
    return 0


def cmd_status(args) -> int:
    profile = _profile(args)
    status = profile.status()
    current = status.current
    payload = {
        "profile": profile.name,
        "path": str(profile.path),
        "active": current is not None,
        "generation": current.name if current else None,
        "snapshot": current.snapshot.digest if current else None,
        "environment": current.snapshot.environment if current else None,
        "packages": len(current.snapshot.packages) if current else 0,
        "files": len(current.snapshot.files()) if current else 0,
        "size": current.snapshot.total_size() if current else 0,
        "generations": status.generation_count,
    }
    if args.json:
        _emit_json(payload)
        return 0
    print(f"profile:    {profile.name}")
    print(f"path:       {profile.path}")
    if current is None:
        print("active:     none")
    else:
        print(f"active:     generation {current.name}")
        print(f"snapshot:   {current.snapshot.digest}")
        print(f"environment: {current.snapshot.environment}")
        print(f"packages:   {len(current.snapshot.packages)}")
        print(f"files:      {len(current.snapshot.files())}")
        print(f"size:       {format_bytes(current.snapshot.total_size())}")
    print(f"generations:{status.generation_count}")
    return 0


def cmd_history(args) -> int:
    profile = _profile(args)
    entries = profile.history()
    if args.json:
        _emit_json(entries)
        return 0
    if not entries:
        print(f"profile {profile.name!r} has no generations")
        return 0
    rows = [
        (
            entry.get("sequence", ""),
            entry.get("generation", ""),
            entry.get("action", ""),
            entry.get("environment", ""),
            short_digest(entry.get("snapshot_digest", "")),
            entry.get("created_at", ""),
        )
        for entry in reversed(entries)
    ]
    print(
        _table(
            ["SEQ", "GENERATION", "ACTION", "ENVIRONMENT", "SNAPSHOT", "CREATED"],
            rows,
        )
    )
    return 0


def cmd_rollback(args) -> int:
    profile = _profile(args)
    current = profile.current_generation()
    target = profile.resolve_reference(args.reference or "previous")
    if args.dry_run:
        payload = {
            "profile": profile.name,
            "current": current.name if current else None,
            "target": target.name,
            "dry_run": True,
        }
        if args.json:
            _emit_json(payload)
        else:
            print(f"would activate generation {target.name} (snapshot {target.snapshot.digest})")
        return 0
    generation = profile.rollback(args.reference)
    payload = {
        "profile": profile.name,
        "generation": generation.name,
        "snapshot": generation.snapshot.digest,
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"rolled back profile {profile.name!r} to generation {generation.name}")
        print(f"  snapshot: {generation.snapshot.digest}")
    return 0


def cmd_diff(args) -> int:
    store = _store(args)
    if args.right:
        before = _snapshot_from_reference(args, args.left, store=store)
        after = _snapshot_from_reference(args, args.right, store=store)
    else:
        path = Path(args.left)
        if not path.is_file() or path.suffix != ".toml":
            raise AxiomError(
                "diff with a single argument expects an environment .toml file; "
                "pass two snapshots or generations to compare them"
            )
        profile = _profile(args)
        current = profile.current_generation()
        if current is None:
            raise ProfileError(f"profile {profile.name!r} has no active generation")
        before = current.snapshot
        after = plan_environment(load_environment(path), store).snapshot

    diff = before.diff(after)
    payload = {
        "from": diff.from_digest,
        "to": diff.to_digest,
        "identical": diff.is_empty,
        "added_packages": list(diff.added_packages),
        "removed_packages": list(diff.removed_packages),
        "changed_packages": list(diff.changed_packages),
        "variables": list(diff.variables_changed),
        "files": [
            {"action": change.action, "target": change.target}
            for change in diff.file_changes
        ],
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"from {diff.from_digest}")
        print(f"to   {diff.to_digest}")
        print(diff.render())
    return 0


def cmd_verify(args) -> int:
    store = _store(args)
    if args.store:
        problems = store.verify(deep=not args.shallow)
        payload = {"target": "store", "ok": not problems, "problems": problems}
        if args.json:
            _emit_json(payload)
        else:
            if problems:
                print("store verification failed:", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
            else:
                print("store is consistent")
        return 0 if not problems else VerificationError.exit_code

    profile = _profile(args)
    report = profile.verify(
        deep=not args.shallow,
        reference=args.reference,
        strict=args.strict,
    )
    payload = {
        "target": f"profile:{profile.name}",
        "generation": report.generation,
        "ok": report.ok,
        "checked_files": report.checked_files,
        "checked_bytes": report.checked_bytes,
        "problems": list(report.problems),
    }
    if args.json:
        _emit_json(payload)
    else:
        if report.ok:
            print(
                f"profile {profile.name!r} generation {report.generation} is intact "
                f"({report.checked_files} files, "
                f"{format_bytes(report.checked_bytes)})"
            )
        else:
            print(
                f"profile {profile.name!r} generation {report.generation} "
                "does not match its manifest:",
                file=sys.stderr,
            )
            for problem in report.problems:
                print(f"  - {problem}", file=sys.stderr)
    return 0 if report.ok else VerificationError.exit_code


def cmd_gc(args) -> int:
    store = _store(args)
    profiles = Profile.list_profiles(store.root)
    keep = set()
    for name in profiles:
        for generation in Profile(store.root, name).generations():
            for record in generation.snapshot.files():
                keep.add(record.digest)
    report = store.gc(keep=keep, dry_run=args.dry_run)
    pruned = {}
    if args.prune_generations:
        for name in profiles:
            removed = Profile(store.root, name).prune(
                keep=args.prune_generations, dry_run=args.dry_run
            )
            if removed:
                pruned[name] = removed

    artifact_report = {"removed": [], "reclaimed": 0}
    if args.artifacts:
        referenced = {entry.artifact_digest for entry in store.entries()}
        for path in sorted(store.artifacts.glob("*.axp")):
            digest = path.stem
            if digest in referenced:
                continue
            artifact_report["removed"].append(digest)
            artifact_report["reclaimed"] += path.stat().st_size
            if not args.dry_run:
                path.unlink()

    payload = {
        "dry_run": args.dry_run,
        "blobs": {
            "removed": len(report.removed),
            "reclaimed": report.reclaimed,
            "kept": report.kept,
        },
        "profiles_pruned": pruned,
        "artifacts": artifact_report,
    }
    if args.json:
        _emit_json(payload)
    else:
        print(report.summary())
        for name, removed in pruned.items():
            verb = "would prune" if args.dry_run else "pruned"
            print(f"{verb} {len(removed)} generation(s) from profile {name!r}")
        if args.artifacts:
            verb = "would remove" if args.dry_run else "removed"
            print(
                f"{verb} {len(artifact_report['removed'])} cached artifact(s), "
                f"{format_bytes(artifact_report['reclaimed'])}"
            )
    return 0


def cmd_prune(args) -> int:
    profile = _profile(args)
    removed = profile.prune(keep=args.keep, dry_run=args.dry_run)
    payload = {
        "profile": profile.name,
        "removed": removed,
        "dry_run": args.dry_run,
    }
    if args.json:
        _emit_json(payload)
    elif not removed:
        print(f"nothing to prune in profile {profile.name!r}")
    else:
        verb = "would remove" if args.dry_run else "removed"
        print(f"{verb} {len(removed)} generation(s) from profile {profile.name!r}")
    return 0


def cmd_env_init(args) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        raise EnvironmentError(f"refusing to overwrite existing file: {path}")
    name = path.stem
    content = (
        f'name = "{name}"\n'
        f'description = "Describe this environment"\n'
        "\n"
        "# Dependencies are name = constraint pairs. Constraints use npm-style\n"
        "# ranges: \"^1.2.0\", \">=1.0.0,<2.0.0\", \"1.2.x\", or an exact version.\n"
        "[packages]\n"
        '# tool = "^1.0.0"\n'
        "\n"
        "[variables]\n"
        '# EDITOR = "vi"\n'
    )
    atomic_write_text(path, content)
    print(f"wrote environment scaffold {path}")
    return 0


def cmd_env_validate(args) -> int:
    environment = load_environment(args.path)
    problems: list = []
    if args.host:
        problems, _facts = check_constraints(environment.environment_constraints)
    payload = {
        "environment": environment.name,
        "packages": len(environment.requirements()),
        "constraints": len(environment.environment_constraints),
        "variables": len(environment.variables),
        "includes": list(environment.includes),
        "host_problems": list(problems),
        "valid": not problems,
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"{environment.name}: valid")
        print(f"  packages:    {payload['packages']}")
        print(f"  constraints: {payload['constraints']}")
        print(f"  variables:   {payload['variables']}")
        if problems:
            print("  host mismatches:", file=sys.stderr)
            for problem in problems:
                print(f"    - {problem}", file=sys.stderr)
    return 0 if not problems else 1


def cmd_env_show(args) -> int:
    environment = load_environment(args.path)
    payload = environment.to_dict()
    if args.json:
        _emit_json(payload)
    else:
        print(f"name: {environment.name}")
        if environment.description:
            print(f"description: {environment.description}")
        print("packages:")
        for requirement in environment.requirements():
            print(f"  {requirement}")
        if environment.variables:
            print("variables:")
            for key, value in sorted(environment.variables.items()):
                print(f"  {key}={value}")
        if environment.environment_constraints:
            print("constraints:")
            for constraint in environment.environment_constraints:
                print(f"  {constraint}")
    return 0


def cmd_run(args) -> int:
    profile = _profile(args)
    if profile.current_generation() is None:
        raise ProfileError(f"profile {profile.name!r} has no active generation")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise AxiomError("no command given; use: axiom run -- <command> [args...]")
    environment = os.environ.copy()
    environment.update(profile.environment())
    try:
        completed = subprocess.run(command, check=False, env=environment)
    except FileNotFoundError:
        raise AxiomError(f"command not found: {command[0]}") from None
    except PermissionError:
        raise AxiomError(f"command is not executable: {command[0]}") from None
    return completed.returncode


def cmd_shell(args) -> int:
    profile = _profile(args)
    if profile.current_generation() is None:
        raise ProfileError(f"profile {profile.name!r} has no active generation")
    snippet = profile.shell_snippet()
    if args.json:
        _emit_json({"profile": profile.name, "environment": profile.environment()})
    else:
        print(snippet)
    return 0


def cmd_export(args) -> int:
    profile = _profile(args)
    generation = profile.current_generation()
    if generation is None:
        raise ProfileError(f"profile {profile.name!r} has no active generation")
    if args.output:
        generation.snapshot.write_lockfile(args.output)
        print(f"wrote {args.output}")
    else:
        print(json.dumps(generation.snapshot.lockfile_dict(), indent=2, sort_keys=True))
    return 0


def cmd_profiles(args) -> int:
    store = _store(args)
    payload = []
    for name in Profile.list_profiles(store.root):
        status = Profile(store.root, name).status()
        payload.append(
            {
                "profile": name,
                "generation": status.current.name if status.current else None,
                "snapshot": status.current.snapshot.digest if status.current else None,
                "generations": status.generation_count,
            }
        )
    if args.json:
        _emit_json(payload)
    elif not payload:
        print("no profiles")
    else:
        print(
            _table(
                ["PROFILE", "GENERATION", "SNAPSHOT", "GENERATIONS"],
                [
                    (
                        item["profile"],
                        item["generation"] or "none",
                        short_digest(item["snapshot"]) if item["snapshot"] else "-",
                        item["generations"],
                    )
                    for item in payload
                ],
            )
        )
    return 0


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axiom",
        description="Immutable, atomic, user-space package manager.",
        parents=[_common()],
    )
    parser.set_defaults(
        root=None, profile=None, json=False, verbose=0, func=None
    )
    parser.add_argument(
        "--version", action="version", version=f"axiom {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, help_text, func, aliases=()):
        sub = subparsers.add_parser(
            name,
            help=help_text,
            description=help_text,
            parents=[_common()],
            aliases=list(aliases),
        )
        sub.set_defaults(func=func)
        return sub

    add("init", "initialise a store", cmd_init)

    sub = add("build", "build a deterministic .axp artifact", cmd_build)
    sub.add_argument("path", help="package directory containing package.toml")
    sub.add_argument("-o", "--output", help="artifact path (default: dist/<name>-<version>.axp)")
    sub.add_argument("--publish", action="store_true", help="register the artifact in the local store")

    sub = add("publish", "register an artifact or package directory", cmd_publish)
    sub.add_argument("targets", nargs="+", help=".axp file or package directory")
    sub.add_argument("--digest", help="require this artifact sha256 (for downloads)")

    sub = add("search", "search registered packages", cmd_search)
    sub.add_argument("query", nargs="?", default="", help="substring to match")

    sub = add("info", "show package metadata", cmd_info)
    sub.add_argument("package", help="name or name@range")

    sub = add("resolve", "resolve an environment without installing", cmd_resolve)
    sub.add_argument("environment", help="environment .toml file")
    sub.add_argument("--prerelease", action="store_true", help="allow pre-release versions")

    sub = add("lock", "write a lockfile for an environment", cmd_lock)
    sub.add_argument("environment", help="environment .toml file")
    sub.add_argument("-o", "--output", help=f"lockfile path (default: {LOCKFILE_NAME})")
    sub.add_argument("--prerelease", action="store_true", help="allow pre-release versions")

    sub = add(
        "install",
        "resolve and atomically activate an environment",
        cmd_install,
        aliases=("apply", "sync"),
    )
    sub.add_argument("environment", help="environment .toml file")
    sub.add_argument("--locked", action="store_true", help="install exactly the lockfile snapshot")
    sub.add_argument("--lock", action="store_true", help="write/update the lockfile")
    sub.add_argument("--lockfile", help=f"lockfile path (default: {LOCKFILE_NAME} next to the environment)")
    sub.add_argument("--dry-run", action="store_true", help="print the plan without changing anything")
    sub.add_argument("--force", action="store_true", help="create a generation even if the snapshot is unchanged")
    sub.add_argument("--verify-blobs", action="store_true", help="re-hash every blob while installing")
    sub.add_argument("--ignore-host", action="store_true", help="skip host constraint checks")
    sub.add_argument("--prerelease", action="store_true", help="allow pre-release versions")

    sub = add("list", "list packages in a profile or the store", cmd_list)
    sub.add_argument("--store", action="store_true", help="list the store instead of the active profile")

    sub = add("status", "show the active generation", cmd_status)

    sub = add("history", "show generation history", cmd_history)

    sub = add("rollback", "atomically activate an earlier generation", cmd_rollback)
    sub.add_argument("reference", nargs="?", help="generation sequence, name, digest prefix or 'previous'")
    sub.add_argument("--dry-run", action="store_true", help="show what would be activated")

    sub = add("diff", "compare snapshots, generations or an environment", cmd_diff)
    sub.add_argument("left", help="snapshot, generation reference, lockfile or environment")
    sub.add_argument("right", nargs="?", help="second snapshot or generation reference")

    sub = add("verify", "check installed files and the store against digests", cmd_verify)
    sub.add_argument("reference", nargs="?", help="generation reference (default: current)")
    sub.add_argument("--store", action="store_true", help="verify the content store instead")
    sub.add_argument("--shallow", action="store_true", help="skip re-hashing file contents")
    sub.add_argument(
        "--strict",
        action="store_true",
        help="treat Python bytecode caches as unexpected files",
    )

    sub = add("gc", "remove unreferenced blobs and old generations", cmd_gc)
    sub.add_argument("--dry-run", action="store_true", help="report without deleting")
    sub.add_argument("--prune-generations", type=int, metavar="N", help="keep at most N generations per profile")
    sub.add_argument("--artifacts", action="store_true", help="also remove unreferenced cached artifacts")

    sub = add("prune", "remove old generations from a profile", cmd_prune)
    sub.add_argument("--keep", type=int, default=5, help="generations to keep (default: 5)")
    sub.add_argument("--dry-run", action="store_true", help="report without deleting")

    sub = add("profiles", "list profiles", cmd_profiles)

    env_parser = subparsers.add_parser(
        "env", help="inspect and scaffold environment files", parents=[_common()]
    )
    env_parser.set_defaults(func=None)
    env_sub = env_parser.add_subparsers(dest="env_command", metavar="ACTION")

    env_init = env_sub.add_parser("init", help="write an environment scaffold", parents=[_common()])
    env_init.add_argument("path", help="environment .toml path")
    env_init.add_argument("--force", action="store_true", help="overwrite an existing file")
    env_init.set_defaults(func=cmd_env_init)

    env_validate = env_sub.add_parser("validate", help="validate an environment file", parents=[_common()])
    env_validate.add_argument("path", help="environment .toml path")
    env_validate.add_argument("--host", action="store_true", help="also check host constraints")
    env_validate.set_defaults(func=cmd_env_validate)

    env_show = env_sub.add_parser("show", help="show a merged environment", parents=[_common()])
    env_show.add_argument("path", help="environment .toml path")
    env_show.set_defaults(func=cmd_env_show)

    sub = add("run", "run a command inside a profile", cmd_run)
    sub.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments (after --)")

    sub = add("shell", "print shell exports for a profile", cmd_shell)

    sub = add("export", "export the active snapshot as a lockfile", cmd_export)
    sub.add_argument("-o", "--output", help="write to this path instead of stdout")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except AxiomError as exc:
        if getattr(args, "json", False):
            _emit_json(
                {
                    "error": exc.message,
                    "details": list(exc.details),
                    "kind": type(exc).__name__,
                }
            )
        print(f"axiom: {exc.render()}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("axiom: interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        # Filesystem failures (permissions, disk full, unwritable root) are
        # expected user-facing errors, not tracebacks.
        print(f"axiom: filesystem error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # pragma: no cover - piping to head
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
