"""Profiles, generations and atomic activation.

A profile is a directory that owns one isolated tree of installed files::

    profiles/dev/current -> generations/000003-1a2b3c4d5e6f
    profiles/dev/generations/000003-1a2b3c4d5e6f/
        GENERATION.json
        files/bin/tool
        files/lib/...

Installing never edits the active tree.  A new generation is materialised
beside the old one from immutable blobs, flushed to disk, and only then is the
``current`` symlink replaced with :func:`os.replace`.  Symlink replacement is a
single atomic operation, so a reader either sees the complete old generation or
the complete new one.  A crash at any point leaves ``current`` untouched; the
half-built staging directory is invisible and is removed on the next run.

Rollback is the same operation in reverse and needs no copying: older
generations are immutable and still on disk.

Every generation records the snapshot it was built from.  The snapshot digest
is therefore the identity of the active state; two installs of an unchanged
environment produce no new generation at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ProfileError
from .snapshot import Snapshot
from .store import Store
from .util import (
    FileLock,
    atomic_symlink,
    atomic_write_json,
    ensure_dir,
    fsync_dir,
    hash_file,
    now_iso,
    remove_tree,
    safe_join,
    short_digest,
)

__all__ = ["Generation", "Profile", "ProfileStatus", "VerifyReport"]

GENERATION_FORMAT = 1
HISTORY_FORMAT = 1
_GENERATION_RE = re.compile(r"^(\d{6})-([0-9a-f]{12})$")
_LOCK_TIMEOUT = 30.0


@dataclass(frozen=True)
class Generation:
    """One immutable, materialised snapshot inside a profile."""

    sequence: int
    name: str
    path: Path
    snapshot: Snapshot
    action: str = "install"
    created_at: str = ""
    parent: str = ""
    facts: Mapping = field(default_factory=dict)
    axiom_version: str = ""

    @property
    def tree(self) -> Path:
        return self.path / "files"

    @property
    def digest(self) -> str:
        return self.snapshot.digest

    def to_dict(self) -> dict:
        return {
            "format": GENERATION_FORMAT,
            "sequence": self.sequence,
            "name": self.name,
            "snapshot": self.snapshot.to_dict(),
            "snapshot_digest": self.snapshot.digest,
            "action": self.action,
            "created_at": self.created_at,
            "parent": self.parent,
            "facts": dict(self.facts),
            "axiom_version": self.axiom_version,
        }

    @classmethod
    def load(cls, path: Path | str) -> Generation:
        path = Path(path)
        metadata_path = path / "GENERATION.json"
        try:
            data = json.loads(metadata_path.read_text("utf-8"))
        except FileNotFoundError:
            raise ProfileError(f"generation is missing metadata: {path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"cannot read generation metadata {metadata_path}: {exc}") from None
        if data.get("format") != GENERATION_FORMAT:
            raise ProfileError(
                f"generation {path.name} uses unsupported format {data.get('format')!r}"
            )
        try:
            snapshot = Snapshot.from_dict(data["snapshot"], verify_digest=False)
        except (KeyError, ValueError) as exc:
            raise ProfileError(f"invalid generation metadata in {path}: {exc}") from None
        recorded = str(data.get("snapshot_digest", ""))
        if recorded and recorded != snapshot.digest:
            raise ProfileError(
                f"generation {path.name} is corrupt: its snapshot digest does "
                "not match its metadata"
            )
        return cls(
            sequence=int(data.get("sequence", 0)),
            name=str(data.get("name", path.name)),
            path=path,
            snapshot=snapshot,
            action=str(data.get("action", "install")),
            created_at=str(data.get("created_at", "")),
            parent=str(data.get("parent", "")),
            facts=dict(data.get("facts", {})),
            axiom_version=str(data.get("axiom_version", "")),
        )


@dataclass(frozen=True)
class ProfileStatus:
    name: str
    path: Path
    current: Generation | None
    generation_count: int
    variables: Mapping = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.current is not None


@dataclass(frozen=True)
class VerifyReport:
    profile: str
    generation: str
    problems: tuple
    checked_files: int
    checked_bytes: int

    @property
    def ok(self) -> bool:
        return not self.problems


class Profile:
    """A named, isolated set of generations."""

    def __init__(self, root: Path | str, name: str):
        if not name or "/" in name or name.startswith("."):
            raise ProfileError(f"invalid profile name: {name!r}")
        self.root = Path(root)
        self.name = name
        self.path = self.root / "profiles" / name
        self.generations_dir = self.path / "generations"
        self.current_link = self.path / "current"
        self.previous_link = self.path / "previous"
        self.history_path = self.path / "history.json"
        self.lock_path = self.path / "profile.lock"

    # ------------------------------------------------------------------ setup

    def initialize(self) -> Profile:
        ensure_dir(self.generations_dir)
        return self

    def exists(self) -> bool:
        return self.generations_dir.is_dir()

    def lock(self, timeout: float | None = _LOCK_TIMEOUT) -> FileLock:
        ensure_dir(self.path)
        return FileLock(self.lock_path, timeout=timeout)

    def _clean_staging(self, *, max_age: float = 3600.0) -> None:
        """Remove abandoned staging directories (must hold the profile lock).

        A staging directory belonging to a live process is never touched; this
        keeps a second invocation from deleting a concurrent install's work.
        """
        if not self.generations_dir.is_dir():
            return
        cutoff = time.time() - max_age
        for entry in self.generations_dir.iterdir():
            if not (
                entry.name.startswith(".staging-") or entry.name.startswith(".tmp-")
            ):
                continue
            try:
                age = entry.stat().st_mtime
            except OSError:
                continue
            if _pid_alive(entry.name):
                continue
            if age > cutoff:
                with contextlib.suppress(OSError):
                    remove_tree(entry)

    # -------------------------------------------------------------- generations

    def generations(self) -> list:
        if not self.generations_dir.is_dir():
            return []
        found = []
        for entry in self.generations_dir.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not _GENERATION_RE.match(entry.name):
                continue
            if not (entry / "GENERATION.json").is_file():
                continue
            found.append(Generation.load(entry))
        found.sort(key=lambda item: item.sequence)
        return found

    def current_generation(self) -> Generation | None:
        if not self.current_link.is_symlink():
            return None
        target = os.readlink(self.current_link)
        resolved = (self.path / target).resolve()
        generations = self.generations_dir.resolve()
        try:
            resolved.relative_to(generations)
        except ValueError:
            raise ProfileError(
                f"profile {self.name!r} has a current link outside its "
                f"generation directory: {target!r}"
            ) from None
        if not resolved.is_dir():
            raise ProfileError(
                f"profile {self.name!r} points at a missing generation: {target!r}"
            )
        return Generation.load(resolved)

    def resolve_reference(self, reference: str) -> Generation:
        """Resolve ``current``, a sequence number, a name or a digest prefix."""
        reference = str(reference).strip()
        if reference in ("current", "active", ""):
            current = self.current_generation()
            if current is None:
                raise ProfileError(f"profile {self.name!r} has no active generation")
            return current
        if reference == "previous":
            return self._previous_generation()
        generations = self.generations()
        if reference.isdigit():
            sequence = int(reference)
            for generation in generations:
                if generation.sequence == sequence:
                    return generation
            raise ProfileError(f"no generation {sequence} in profile {self.name!r}")
        for generation in generations:
            if generation.name == reference:
                return generation
        needle = reference.split(":", 1)[-1].lower()
        matches = [
            generation
            for generation in generations
            if generation.snapshot.digest.split(":", 1)[-1].startswith(needle)
            and len(needle) >= 4
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ProfileError(
                f"digest prefix {reference!r} matches several generations"
            )
        raise ProfileError(f"generation not found: {reference!r}")

    def _previous_generation(self) -> Generation:
        current = self.current_generation()
        if current is None:
            raise ProfileError(f"profile {self.name!r} has no active generation")
        candidates = [
            generation
            for generation in self.generations()
            if generation.sequence < current.sequence
        ]
        if not candidates:
            raise ProfileError(
                f"profile {self.name!r} has no earlier generation to roll back to"
            )
        return candidates[-1]

    # ---------------------------------------------------------------- install

    def install(
        self,
        snapshot: Snapshot,
        store: Store,
        *,
        facts: Mapping | None = None,
        force: bool = False,
        verify_blobs: bool = False,
    ) -> tuple:
        """Materialise and activate ``snapshot``; return ``(generation, changed)``.

        The return value's second element is ``False`` when the active snapshot
        already has this digest, in which case nothing is written.
        """
        self.initialize()
        with self.lock():
            self._clean_staging()
            current = self.current_generation()
            if (
                current is not None
                and current.snapshot.digest == snapshot.digest
                and not force
            ):
                return current, False

            sequence = self._next_sequence()
            name = f"{sequence:06d}-{snapshot.digest.split(':', 1)[-1][:12]}"
            final = self.generations_dir / name
            staging = self.generations_dir / f".staging-{os.getpid()}-{os.urandom(4).hex()}"
            if final.exists():  # pragma: no cover - sequence collision guard
                raise ProfileError(f"generation already exists: {final}")
            ensure_dir(staging)
            try:
                self._materialize(staging, snapshot, store, verify_blobs=verify_blobs)
                generation = Generation(
                    sequence=sequence,
                    name=name,
                    path=final,
                    snapshot=snapshot,
                    action="install",
                    created_at=now_iso(),
                    parent=current.snapshot.digest if current else "",
                    facts=dict(facts or {}),
                    axiom_version=_axiom_version(),
                )
                self._write_metadata(staging, generation)
                _fsync_tree(staging)
                os.rename(staging, final)
                fsync_dir(self.generations_dir)
            except BaseException:
                with contextlib.suppress(OSError):
                    remove_tree(staging)
                raise

            self._activate(generation, previous=current)
            self._append_history(
                {
                    "sequence": generation.sequence,
                    "generation": generation.name,
                    "snapshot_digest": generation.snapshot.digest,
                    "environment": generation.snapshot.environment,
                    "action": "install",
                    "created_at": generation.created_at,
                    "parent": generation.parent,
                }
            )
            return generation, True

    def _materialize(
        self,
        staging: Path,
        snapshot: Snapshot,
        store: Store,
        *,
        verify_blobs: bool,
    ) -> None:
        tree = staging / "files"
        ensure_dir(tree)
        for record in snapshot.files():
            destination = safe_join(tree, record.target)
            blob = store.blob_path(record.digest)
            if not blob.is_file():
                raise ProfileError(
                    f"cannot install {record.target!r}: blob "
                    f"{short_digest(record.digest)} is missing from the store"
                )
            size = blob.stat().st_size
            if size != record.size:
                raise ProfileError(
                    f"cannot install {record.target!r}: blob size mismatch "
                    f"(expected {record.size}, found {size})"
                )
            if verify_blobs and hash_file(blob) != record.digest:
                raise ProfileError(
                    f"cannot install {record.target!r}: blob digest mismatch"
                )
            ensure_dir(destination.parent)
            mode = int(record.mode, 8)
            # Always copy: hardlinking would let a chmod or an accidental write
            # inside the profile mutate the store's immutable blob.
            with open(blob, "rb") as source, open(destination, "wb") as target:
                shutil.copyfileobj(source, target, length=1 << 20)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(destination, mode)

    def _write_metadata(self, staging: Path, generation: Generation) -> None:
        path = staging / "GENERATION.json"
        atomic_write_json(path, generation.to_dict())

    def _next_sequence(self) -> int:
        sequences = [generation.sequence for generation in self.generations()]
        return (max(sequences) + 1) if sequences else 1

    def _activate(self, generation: Generation, *, previous: Generation | None) -> None:
        relative = f"generations/{generation.name}"
        if previous is not None:
            atomic_symlink(
                f"generations/{previous.name}",
                self.previous_link,
            )
        atomic_symlink(relative, self.current_link)

    # --------------------------------------------------------------- rollback

    def rollback(self, reference: str | None = None) -> Generation:
        """Activate an earlier generation without copying anything."""
        self.initialize()
        with self.lock():
            self._clean_staging()
            current = self.current_generation()
            if current is None:
                raise ProfileError(f"profile {self.name!r} has no active generation")
            if reference is None:
                target = self._previous_generation()
            else:
                target = self.resolve_reference(reference)
            if target.snapshot.digest == current.snapshot.digest:
                raise ProfileError(
                    f"profile {self.name!r} is already at {target.name}"
                )
            self._activate(target, previous=current)
            self._append_history(
                {
                    "sequence": target.sequence,
                    "generation": target.name,
                    "snapshot_digest": target.snapshot.digest,
                    "environment": target.snapshot.environment,
                    "action": "rollback",
                    "created_at": now_iso(),
                    "parent": current.snapshot.digest,
                }
            )
            return target

    # ----------------------------------------------------------------- status

    def status(self) -> ProfileStatus:
        generations = self.generations()
        return ProfileStatus(
            name=self.name,
            path=self.path,
            current=self.current_generation(),
            generation_count=len(generations),
        )

    def environment(self, snapshot: Snapshot | None = None) -> dict:
        """Environment variables for running commands inside this profile."""
        generation = self.current_generation()
        if snapshot is None and generation is not None:
            snapshot = generation.snapshot
        variables = dict(snapshot.variables) if snapshot else {}
        if self.current_link.exists():
            bin_dir = self.current_link / "files" / "bin"
            variables["PATH"] = str(bin_dir) + os.pathsep + variables.get(
                "PATH", os.environ.get("PATH", "")
            )
        if generation is not None:
            variables["AXIOM_PROFILE"] = self.name
            variables["AXIOM_GENERATION"] = generation.name
            variables["AXIOM_SNAPSHOT"] = generation.snapshot.digest
        return variables

    def shell_snippet(self) -> str:
        variables = self.environment()
        lines = []
        for key in sorted(variables):
            value = variables[key].replace("'", "'\\''")
            lines.append(f"export {key}='{value}'")
        return "\n".join(lines)

    # ----------------------------------------------------------------- verify

    def verify(
        self,
        *,
        deep: bool = True,
        reference: str | None = None,
        strict: bool = False,
    ) -> VerifyReport:
        generation = (
            self.resolve_reference(reference)
            if reference is not None
            else self.current_generation()
        )
        if generation is None:
            raise ProfileError(f"profile {self.name!r} has no active generation")
        expected = {record.target: record for record in generation.snapshot.files()}
        problems = []
        checked = 0
        checked_bytes = 0
        tree = generation.tree
        if not tree.is_dir():
            raise ProfileError(
                f"generation {generation.name} is missing its file tree"
            )
        seen = set()
        for target, record in sorted(expected.items()):
            path = safe_join(tree, target)
            if not path.is_file() or path.is_symlink():
                problems.append(f"missing file: {target}")
                continue
            seen.add(target)
            checked += 1
            checked_bytes += record.size
            actual_mode = f"{stat.S_IMODE(path.stat().st_mode):04o}"
            if actual_mode != record.mode:
                problems.append(
                    f"mode mismatch: {target} (expected {record.mode}, found {actual_mode})"
                )
            if path.stat().st_size != record.size:
                problems.append(f"size mismatch: {target}")
                continue
            if deep and hash_file(path) != record.digest:
                problems.append(f"digest mismatch: {target}")
        for path in sorted(tree.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(tree).as_posix()
            if relative in seen:
                continue
            if not strict and _is_bytecode_cache(relative):
                # Running Python code from a profile creates __pycache__
                # directories. They are runtime state, not managed content.
                continue
            problems.append(f"unexpected file: {relative}")
        return VerifyReport(
            profile=self.name,
            generation=generation.name,
            problems=tuple(problems),
            checked_files=checked,
            checked_bytes=checked_bytes,
        )

    # ---------------------------------------------------------------- pruning

    def prune(self, keep: int = 5, *, dry_run: bool = False) -> list:
        """Remove old generations, never the active or previous one."""
        if keep < 2:
            raise ProfileError("keep must be at least 2 (current and previous)")
        with self.lock():
            self._clean_staging()
            current = self.current_generation()
            generations = self.generations()
            if len(generations) <= keep:
                return []
            removable = generations[:-keep]
            removed = []
            for generation in removable:
                if current is not None and generation.sequence == current.sequence:
                    continue
                removed.append(generation.name)
                if not dry_run:
                    remove_tree(generation.path)
            if not dry_run and removed:
                self._rewrite_history()
            return removed

    # ---------------------------------------------------------------- history

    def history(self) -> list:
        entries = self._read_history()
        if entries:
            return entries
        return [
            {
                "sequence": generation.sequence,
                "generation": generation.name,
                "snapshot_digest": generation.snapshot.digest,
                "environment": generation.snapshot.environment,
                "action": generation.action,
                "created_at": generation.created_at,
                "parent": generation.parent,
            }
            for generation in self.generations()
        ]

    def _read_history(self) -> list:
        try:
            data = json.loads(self.history_path.read_text("utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            return []
        if data.get("format") != HISTORY_FORMAT:
            return []
        return list(data.get("entries", ()))

    def _append_history(self, entry: dict) -> None:
        entries = self._read_history()
        entries.append(entry)
        ensure_dir(self.history_path.parent)
        atomic_write_json(
            self.history_path,
            {"format": HISTORY_FORMAT, "entries": entries[-200:]},
        )

    def _rewrite_history(self) -> None:
        existing = {
            generation.name: generation for generation in self.generations()
        }
        entries = [
            entry
            for entry in self._read_history()
            if entry.get("generation") in existing
        ]
        atomic_write_json(
            self.history_path,
            {"format": HISTORY_FORMAT, "entries": entries[-200:]},
        )

    # ------------------------------------------------------------------- misc

    @staticmethod
    def list_profiles(root: Path | str) -> list:
        directory = Path(root) / "profiles"
        if not directory.is_dir():
            return []
        return sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Profile({self.name!r} at {self.path})"


def _axiom_version() -> str:
    from . import __version__

    return __version__


def _fsync_tree(root: Path) -> None:
    """Flush every file and directory below ``root`` (best effort)."""
    for directory, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = Path(directory) / filename
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                continue
            try:
                os.fsync(fd)
            except OSError:  # pragma: no cover - platform dependent
                pass
            finally:
                os.close(fd)
        fsync_dir(directory)
    fsync_dir(root)


def _is_bytecode_cache(relative: str) -> bool:
    parts = relative.split("/")
    return "__pycache__" in parts or relative.endswith((".pyc", ".pyo"))


def _pid_alive(staging_name: str) -> bool:
    """Best-effort check whether the process that created staging is alive."""
    match = re.match(r"^\.(?:staging|tmp)-(\d+)-", staging_name)
    if not match:
        return False
    pid = int(match.group(1))
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
