"""The content-addressed store: blobs, artifacts and package metadata.

Layout under the store root::

    blobs/<ab>/<sha256>              file contents, mode 0444, named by digest
    packages/<name>/<version>/entry.json
    artifacts/<sha256>.axp           verified artifact cache
    profiles/<name>/                 managed by :mod:`axiom.profile`
    tmp/                             staging area for atomic writes
    store.lock                       advisory lock for writers

Two invariants make the store safe:

* A blob is written once, under the digest of its contents, and never
  modified.  Re-adding the same bytes is a no-op.
* A package version is written once.  Registering ``foo@1.0.0`` with different
  contents is an error, not an overwrite; changing a published version would
  break every snapshot that references it.
"""

from __future__ import annotations

import json
import os
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .artifact import (
    ARTIFACT_FORMAT,
    Artifact,
    build_artifact,
    read_artifact,
)
from .errors import StoreError
from .model import Package, PackageFile
from .util import (
    FileLock,
    atomic_write_bytes,
    atomic_write_json,
    default_root,
    ensure_dir,
    format_bytes,
    fsync_dir,
    hash_bytes,
    hash_file,
    now_iso,
    remove_tree,
    temp_name,
)

__all__ = ["GcReport", "Store", "StoreEntry"]

STORE_FORMAT = 1
BLOB_MODE = 0o444
_TEMP_MAX_AGE = 3600.0


@dataclass(frozen=True)
class StoreEntry:
    """A registered package: metadata plus content-addressed file records."""

    package: Package
    files: tuple
    content_digest: str
    artifact_digest: str
    artifact_size: int = 0
    imported_at: str = ""

    @property
    def identity(self) -> str:
        return self.package.identity

    def file_map(self) -> dict:
        return {item.target: item for item in self.files}

    def to_dict(self) -> dict:
        return {
            "format": STORE_FORMAT,
            "package": self.package.to_dict(),
            "files": [item.to_dict() for item in self.files],
            "content_digest": self.content_digest,
            "artifact": self.artifact_digest,
            "artifact_size": self.artifact_size,
            "imported_at": self.imported_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StoreEntry:
        return cls(
            package=Package.from_dict(data["package"]),
            files=tuple(
                PackageFile.from_dict(item) for item in data.get("files", ())
            ),
            content_digest=str(data.get("content_digest", "")),
            artifact_digest=str(data.get("artifact", "")),
            artifact_size=int(data.get("artifact_size", 0)),
            imported_at=str(data.get("imported_at", "")),
        )


@dataclass
class GcReport:
    removed: list
    reclaimed: int
    kept: int
    dry_run: bool

    def summary(self) -> str:
        verb = "would remove" if self.dry_run else "removed"
        return (
            f"{verb} {len(self.removed)} blobs, "
            f"{format_bytes(self.reclaimed)}; kept {self.kept} blobs"
        )


class Store:
    """A user-space package store rooted at ``root``."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root is not None else default_root()
        self.blobs = self.root / "blobs"
        self.packages = self.root / "packages"
        self.artifacts = self.root / "artifacts"
        self.profiles = self.root / "profiles"
        self.tmp = self.root / "tmp"
        self.snapshots = self.root / "snapshots"
        self.lock_path = self.root / "store.lock"

    # ------------------------------------------------------------------ setup

    def initialize(self) -> Store:
        for directory in (
            self.root,
            self.blobs,
            self.packages,
            self.artifacts,
            self.profiles,
            self.tmp,
            self.snapshots,
        ):
            ensure_dir(directory)
        version_file = self.root / "format.json"
        if not version_file.exists():
            atomic_write_json(version_file, {"format": STORE_FORMAT})
        else:
            try:
                data = json.loads(version_file.read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StoreError(f"corrupt store format file {version_file}: {exc}") from None
            if data.get("format") != STORE_FORMAT:
                raise StoreError(
                    f"store at {self.root} uses unsupported format "
                    f"{data.get('format')!r}"
                )
        return self

    def lock(self, timeout: float | None = 30.0) -> FileLock:
        ensure_dir(self.root)
        return FileLock(self.lock_path, timeout=timeout)

    # ------------------------------------------------------------------ blobs

    def blob_path(self, digest: str) -> Path:
        digest = _bare_digest(digest)
        return self.blobs / digest[:2] / digest

    def has_blob(self, digest: str) -> bool:
        return self.blob_path(digest).is_file()

    def put_blob(self, data: bytes) -> str:
        """Store ``data`` and return its hex digest."""
        digest = hash_bytes(data)
        target = self.blob_path(digest)
        if target.is_file():
            return digest
        ensure_dir(self.tmp)
        ensure_dir(target.parent)
        tmp = temp_name(self.tmp)
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, BLOB_MODE)
            os.replace(tmp, target)
            fsync_dir(target.parent)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
        return digest

    def put_blob_stream(self, handle, expected: str, size: int | None = None) -> str:
        """Stream a file-like object into the store, verifying its digest."""
        expected = _bare_digest(expected)
        ensure_dir(self.tmp)
        tmp = temp_name(self.tmp)
        import hashlib

        digest = hashlib.sha256()
        written = 0
        try:
            with open(tmp, "wb") as target:
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    written += len(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            actual = digest.hexdigest()
            if actual != expected:
                raise StoreError(
                    f"blob digest mismatch: expected {expected}, got {actual}"
                )
            if size is not None and written != size:
                raise StoreError(
                    f"blob size mismatch: expected {size}, got {written}"
                )
            destination = self.blob_path(expected)
            if destination.is_file():
                tmp.unlink()
                return expected
            ensure_dir(destination.parent)
            os.chmod(tmp, BLOB_MODE)
            os.replace(tmp, destination)
            fsync_dir(destination.parent)
            return expected
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise

    def read_blob(self, digest: str) -> bytes:
        path = self.blob_path(digest)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise StoreError(f"blob is missing from the store: {digest}") from None
        except OSError as exc:
            raise StoreError(f"cannot read blob {digest}: {exc}") from None

    # ------------------------------------------------------------- registration

    def ingest_artifact(
        self, path: Path | str, expected_digest: str | None = None
    ) -> StoreEntry:
        """Copy a verified artifact and its contents into the store."""
        path = Path(path)
        if not path.is_file():
            raise StoreError(f"artifact not found: {path}")
        artifact_digest = hash_file(path)
        if expected_digest is not None and artifact_digest != _bare_digest(expected_digest):
            raise StoreError(
                f"artifact digest mismatch for {path}: "
                f"expected {_bare_digest(expected_digest)}, got {artifact_digest}"
            )
        artifact = read_artifact(path)
        return self._register(artifact, artifact_digest, source=path)

    def ingest_directory(
        self,
        directory: Path | str,
        expected_digest: str | None = None,
    ) -> StoreEntry:
        """Build a package directory into a temporary artifact and register it."""
        staging = temp_name(self.tmp)
        ensure_dir(staging)
        try:
            artifact = build_artifact(directory, output=staging / "package.axp")
            assert artifact.path is not None
            digest = hash_file(artifact.path)
            if expected_digest is not None and digest != _bare_digest(expected_digest):
                raise StoreError(
                    f"artifact digest mismatch for {directory}: expected "
                    f"{_bare_digest(expected_digest)}, got {digest}"
                )
            return self._register(artifact, digest, source=artifact.path)
        finally:
            remove_tree(staging)

    def _register(self, artifact: Artifact, artifact_digest: str, source: Path) -> StoreEntry:
        artifact_digest = _bare_digest(artifact_digest)
        package = artifact.package
        existing = self.get_entry(package.name, package.version)
        if existing is not None:
            if existing.content_digest == artifact.content_digest:
                self._cache_artifact(source, artifact_digest)
                return existing
            raise StoreError(
                f"{package.identity} is already registered with different content; "
                "package versions are immutable - publish a new version instead"
            )

        ensure_dir(self.tmp)
        with self.lock():
            # Re-check under the lock: another process may have won the race.
            existing = self.get_entry(package.name, package.version)
            if existing is not None:
                if existing.content_digest == artifact.content_digest:
                    self._cache_artifact(source, artifact_digest)
                    return existing
                raise StoreError(
                    f"{package.identity} is already registered with different content"
                )
            self._extract_blobs(source, artifact)
            entry = StoreEntry(
                package=package,
                files=tuple(sorted(artifact.files, key=lambda f: f.target)),
                content_digest=artifact.content_digest,
                artifact_digest=artifact_digest,
                artifact_size=path_size(source),
                imported_at=now_iso(),
            )
            directory = self.packages / package.name / str(package.version)
            ensure_dir(directory)
            atomic_write_json(directory / "entry.json", entry.to_dict())
            self._cache_artifact(source, artifact_digest)
            return entry

    def _extract_blobs(self, source: Path, artifact: Artifact) -> None:
        """Verify and store every payload entry of the artifact."""
        expected = {item.target: item for item in artifact.files}
        seen = set()
        try:
            with tarfile.open(source, mode="r:*") as archive:
                for member in archive:
                    name = member.name
                    if not name.startswith("files/"):
                        continue
                    target = name[len("files/") :]
                    record = expected.get(target)
                    if record is None:
                        raise StoreError(
                            f"artifact {source} contains undeclared file {target!r}"
                        )
                    if target in seen:
                        raise StoreError(
                            f"artifact {source} contains duplicate file {target!r}"
                        )
                    seen.add(target)
                    handle = archive.extractfile(member)
                    if handle is None:  # pragma: no cover - defensive
                        raise StoreError(
                            f"artifact {source}: cannot read {target!r}"
                        )
                    self.put_blob_stream(handle, record.digest, record.size)
        except tarfile.TarError as exc:
            raise StoreError(f"cannot read artifact {source}: {exc}") from None
        missing = sorted(set(expected) - seen)
        if missing:
            raise StoreError(
                f"artifact {source} is missing payload entries: {', '.join(missing[:5])}"
            )

    def _cache_artifact(self, source: Path, digest: str) -> None:
        target = self.artifacts / f"{digest}.axp"
        try:
            if source.resolve() == target.resolve():
                return
        except OSError:
            pass
        if target.is_file():
            return
        ensure_dir(target.parent)
        data = source.read_bytes()
        if hash_bytes(data) != digest:  # pragma: no cover - defensive
            raise StoreError(f"artifact changed while importing: {source}")
        atomic_write_bytes(target, data, mode=0o444)

    # ----------------------------------------------------------------- lookup

    def entry_path(self, name: str, version) -> Path:
        return self.packages / name / str(version) / "entry.json"

    def get_entry(self, name: str, version) -> StoreEntry | None:
        path = self.entry_path(name, version)
        if not path.is_file():
            return None
        return _load_entry(path)

    def versions(self, name: str) -> list:
        directory = self.packages / name
        if not directory.is_dir():
            return []
        from .version import parse_version

        versions = []
        for child in directory.iterdir():
            if (child / "entry.json").is_file():
                versions.append(parse_version(child.name))
        return sorted(versions, reverse=True)

    def entries(self) -> Iterator[StoreEntry]:
        if not self.packages.is_dir():
            return
        for name_dir in sorted(self.packages.iterdir()):
            if not name_dir.is_dir():
                continue
            for version_dir in sorted(name_dir.iterdir()):
                path = version_dir / "entry.json"
                if path.is_file():
                    yield _load_entry(path)

    def candidates(self, name: str) -> list:
        """Return registered packages matching ``name`` by name or ``provides``."""
        matches = []
        for entry in self.entries():
            package = entry.package
            if package.name == name or name in package.provides:
                matches.append(entry)
        matches.sort(key=lambda entry: (entry.package.version, entry.package.name), reverse=True)
        return matches

    def all_packages(self) -> list:
        return [entry.package for entry in self.entries()]

    def stats(self) -> dict:
        blob_count = 0
        blob_bytes = 0
        if self.blobs.is_dir():
            for path in self.blobs.rglob("*"):
                if path.is_file():
                    blob_count += 1
                    blob_bytes += path.stat().st_size
        entries = list(self.entries())
        return {
            "root": str(self.root),
            "packages": len(entries),
            "names": len({entry.package.name for entry in entries}),
            "blobs": blob_count,
            "blob_bytes": blob_bytes,
            "artifacts": (
                len(list(self.artifacts.glob("*.axp")))
                if self.artifacts.is_dir()
                else 0
            ),
        }

    # -------------------------------------------------------------------- gc

    def gc(
        self,
        *,
        keep: Iterable[str] = (),
        dry_run: bool = False,
        remove_temporary: bool = True,
    ) -> GcReport:
        """Delete blobs that no package or supplied snapshot references."""
        with self.lock():
            referenced = {_bare_digest(digest) for digest in keep}
            for entry in self.entries():
                for record in entry.files:
                    referenced.add(_bare_digest(record.digest))

            removed: list = []
            reclaimed = 0
            kept = 0
            if self.blobs.is_dir():
                for path in sorted(self.blobs.rglob("*")):
                    if not path.is_file():
                        continue
                    digest = path.name
                    if digest in referenced:
                        kept += 1
                        continue
                    size = path.stat().st_size
                    removed.append(digest)
                    reclaimed += size
                    if not dry_run:
                        try:
                            path.unlink()
                        except OSError as exc:
                            raise StoreError(
                                f"cannot remove blob {path}: {exc}"
                            ) from None
            if remove_temporary and not dry_run:
                self._clean_temporary()
            return GcReport(
                removed=removed, reclaimed=reclaimed, kept=kept, dry_run=dry_run
            )

    def _clean_temporary(self) -> None:
        if not self.tmp.is_dir():
            return
        import time as _time

        cutoff = _time.time() - _TEMP_MAX_AGE
        for path in self.tmp.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    remove_tree(path)
            except OSError:
                continue

    # ---------------------------------------------------------------- verify

    def verify(self, *, deep: bool = False) -> list:
        """Return a list of problems found in the store (empty means healthy)."""
        problems: list = []
        for entry in self.entries():
            for record in entry.files:
                path = self.blob_path(record.digest)
                if not path.is_file():
                    problems.append(
                        f"{entry.identity}: missing blob for {record.target!r} "
                        f"({record.digest[:12]})"
                    )
                    continue
                size = path.stat().st_size
                if size != record.size:
                    problems.append(
                        f"{entry.identity}: size mismatch for {record.target!r}"
                    )
                if deep and hash_file(path) != _bare_digest(record.digest):
                    problems.append(
                        f"{entry.identity}: digest mismatch for {record.target!r}"
                    )
            if not entry.content_digest:
                problems.append(f"{entry.identity}: entry has no content digest")
            else:
                expected = _entry_content_digest(entry)
                if expected != entry.content_digest:
                    problems.append(
                        f"{entry.identity}: metadata digest does not match the "
                        "recorded content digest"
                    )
        return problems


def _entry_content_digest(entry: StoreEntry) -> str:
    from .model import content_digest

    return content_digest(
        {
            "format": ARTIFACT_FORMAT,
            "package": entry.package.to_dict(),
            "files": [
                record.to_dict()
                for record in sorted(entry.files, key=lambda item: item.target)
            ],
        }
    )


def _load_entry(path: Path) -> StoreEntry:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"corrupt package entry {path}: {exc}") from None
    try:
        return StoreEntry.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError(f"invalid package entry {path}: {exc}") from None


def _bare_digest(digest: str) -> str:
    text = str(digest).strip()
    text = text.removeprefix("sha256:")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise StoreError(f"invalid sha256 digest: {digest!r}")
    return text


def path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
