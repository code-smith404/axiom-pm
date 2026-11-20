"""The ``.axp`` package artifact: a deterministic, self-describing tarball.

An artifact contains exactly two kinds of entries:

``axiom-artifact.json``
    Canonical JSON describing the package, its files and the digest of that
    description.

``files/<target>``
    File contents, named by the path they occupy inside a profile.

The bytes of the artifact are reproducible: entries are sorted, timestamps are
zero, owners are empty and modes are normalised.  Building the same directory
twice therefore produces byte-identical output, and the artifact digest is a
stable name for the package.

Reading an artifact is defensive.  Absolute paths, ``..`` components, symlinks,
hard links, device nodes and duplicate entries are rejected, and every file is
checked against the digest recorded in the metadata before it can enter the
content store.
"""

from __future__ import annotations

import io
import json
import posixpath
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ArtifactError
from .manifest import load_package, package_from_dict
from .model import Package, PackageFile, content_digest, normalize_mode
from .util import (
    atomic_write_bytes,
    ensure_dir,
    hash_bytes,
    hash_file,
    safe_join,
)

__all__ = [
    "ARTIFACT_FORMAT",
    "META_NAME",
    "PAYLOAD_DIR",
    "Artifact",
    "build_artifact",
    "default_artifact_name",
    "read_artifact",
    "verify_artifact",
]

ARTIFACT_FORMAT = 1
META_NAME = "axiom-artifact.json"
PAYLOAD_DIR = "files"
_MAX_META_BYTES = 4 << 20


@dataclass(frozen=True)
class Artifact:
    """A verified artifact: metadata plus content-addressed file records."""

    package: Package
    files: tuple
    content_digest: str
    artifact_digest: str
    path: Path | None = None

    @property
    def identity(self) -> str:
        return self.package.identity

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    def file_map(self) -> dict:
        return {item.target: item for item in self.files}


def default_artifact_name(package: Package) -> str:
    return f"{package.name}-{package.version}.axp"


def build_artifact(
    source_dir: Path | str,
    output: Path | str | None = None,
    *,
    deterministic: bool = True,
) -> Artifact:
    """Build an artifact from a package directory.

    The directory must contain ``package.toml``.  Payload files are read from
    ``files/`` (or ``payload/`` as a fallback).  When ``package.toml`` does not
    declare ``files``, every regular file under the payload directory is
    installed at the same relative path.
    """
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise ArtifactError(f"package directory not found: {source_dir}")
    definition = source_dir / "package.toml"
    if not definition.is_file():
        raise ArtifactError(f"missing package.toml in {source_dir}")

    package = load_package(definition)
    payload_root = source_dir / PAYLOAD_DIR
    if not payload_root.is_dir():
        fallback = source_dir / "payload"
        payload_root = fallback if fallback.is_dir() else payload_root

    files, contents = _collect_files(package, payload_root)
    if output is None:
        output = source_dir / "dist" / default_artifact_name(package)
    output = Path(output)
    data = _pack(package, files, contents)
    ensure_dir(output.parent)
    atomic_write_bytes(output, data, mode=0o644)
    return Artifact(
        package=package,
        files=tuple(files),
        content_digest=_content_digest(package, files),
        artifact_digest=hash_bytes(data),
        path=output,
    )


def _collect_files(package: Package, payload_root: Path) -> tuple:
    """Resolve declared files, or auto-discover the payload tree.

    Returns ``(records, contents)`` where ``contents`` maps each install target
    to its bytes so the archive writer never has to touch the filesystem again.
    """
    if package.files:
        records = []
        contents = {}
        seen = set()
        for spec in package.files:
            source = safe_join(payload_root, spec.source)
            if not source.is_file():
                raise ArtifactError(
                    f"{package.identity}: declared file is missing: {spec.source!r}"
                )
            if source.is_symlink():
                raise ArtifactError(
                    f"{package.identity}: symlinks are not allowed in payloads: "
                    f"{spec.source!r}"
                )
            target = _normalise_member(spec.target)
            if target in seen:
                raise ArtifactError(
                    f"{package.identity}: duplicate install target {target!r}"
                )
            seen.add(target)
            data = source.read_bytes()
            contents[target] = data
            records.append(_record(target, data, spec.mode))
        return sorted(records, key=lambda item: item.target), contents

    if not payload_root.is_dir():
        raise ArtifactError(
            f"{package.identity}: no files declared and no payload directory "
            f"at {payload_root}"
        )

    records = []
    contents = {}
    for path in sorted(payload_root.rglob("*")):
        relative = path.relative_to(payload_root).as_posix()
        if path.is_symlink():
            raise ArtifactError(
                f"{package.identity}: symlinks are not allowed in payloads: "
                f"{relative!r}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ArtifactError(
                f"{package.identity}: unsupported file type in payload: {relative!r}"
            )
        mode = "0755" if path.stat().st_mode & 0o111 else "0644"
        target = _normalise_member(relative)
        data = path.read_bytes()
        contents[target] = data
        records.append(_record(target, data, mode))
    if not records:
        raise ArtifactError(
            f"{package.identity}: payload directory {payload_root} is empty"
        )
    return sorted(records, key=lambda item: item.target), contents


def _record(target: str, data: bytes, mode: str | int) -> PackageFile:
    return PackageFile(
        target=target,
        digest=hash_bytes(data),
        mode=normalize_mode(mode),
        size=len(data),
    )


def _normalise_member(name: str) -> str:
    """Validate an archive/target path and return its POSIX form."""
    if not name or name.startswith("/") or "\\" in name:
        raise ArtifactError(f"invalid file path in package: {name!r}")
    if any(ord(char) < 32 for char in name):
        raise ArtifactError(f"control characters in file path: {name!r}")
    normalised = posixpath.normpath(name)
    if normalised in ("", ".", "..") or normalised.startswith("../"):
        raise ArtifactError(f"file path escapes the package root: {name!r}")
    parts = PurePosixPath(normalised).parts
    if any(part in ("..", "") for part in parts):
        raise ArtifactError(f"file path escapes the package root: {name!r}")
    return normalised


def _metadata(package: Package, files: Iterable[PackageFile]) -> dict:
    return {
        "format": ARTIFACT_FORMAT,
        "package": package.to_dict(),
        "files": [item.to_dict() for item in sorted(files, key=lambda f: f.target)],
    }


def _content_digest(package: Package, files: Iterable[PackageFile]) -> str:
    return content_digest(_metadata(package, files))


def _pack(package: Package, files: Iterable[PackageFile], contents: dict) -> bytes:
    files = sorted(files, key=lambda item: item.target)
    metadata = _metadata(package, files)
    meta_bytes = json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    entries = [(META_NAME, meta_bytes, 0o644)]
    for record in files:
        entries.append(
            (
                f"{PAYLOAD_DIR}/{record.target}",
                contents[record.target],
                int(record.mode, 8),
            )
        )
    return _write_tar(entries)


def _tarinfo(name: str, size: int, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    info.pax_headers = {}
    return info


def _write_tar(entries: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT
    ) as archive:
        for name, data, mode in entries:
            info = _tarinfo(name, len(data), mode)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def read_artifact(path: Path | str, *, verify: bool = True) -> Artifact:
    """Read and validate an artifact from disk."""
    path = Path(path)
    if not path.is_file():
        raise ArtifactError(f"artifact not found: {path}")
    if verify:
        return _read_verified(path)
    return _read_verified(path)


def _read_verified(path: Path) -> Artifact:
    meta_bytes: bytes | None = None
    payload: dict = {}
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive:
                name = _normalise_member(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ArtifactError(
                        f"artifact {path} contains an unsupported entry: "
                        f"{member.name!r} (only regular files are allowed)"
                    )
                if name in payload:
                    raise ArtifactError(
                        f"artifact {path} contains duplicate entry {name!r}"
                    )
                handle = archive.extractfile(member)
                if handle is None:  # pragma: no cover - defensive
                    raise ArtifactError(
                        f"artifact {path} entry {name!r} has no contents"
                    )
                data = handle.read()
                if name == META_NAME:
                    if len(data) > _MAX_META_BYTES:
                        raise ArtifactError(
                            f"artifact {path} metadata is unreasonably large"
                        )
                    meta_bytes = data
                elif name.startswith(PAYLOAD_DIR + "/"):
                    payload[name] = data
                else:
                    raise ArtifactError(
                        f"artifact {path} contains an unexpected entry: {name!r}"
                    )
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ArtifactError(f"cannot read artifact {path}: {exc}") from None

    if meta_bytes is None:
        raise ArtifactError(f"artifact {path} is missing {META_NAME}")
    try:
        metadata = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"artifact {path} has invalid metadata: {exc}") from None

    if metadata.get("format") != ARTIFACT_FORMAT:
        raise ArtifactError(
            f"artifact {path} uses unsupported format {metadata.get('format')!r}"
        )
    try:
        package = package_from_dict(metadata["package"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(f"artifact {path} has invalid metadata: {exc}") from None

    records = []
    declared = {}
    for item in metadata.get("files", ()):
        try:
            record = PackageFile.from_dict(item)
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactError(
                f"artifact {path} has an invalid file record: {exc}"
            ) from None
        target = _normalise_member(record.target)
        if target in declared:
            raise ArtifactError(
                f"artifact {path} declares duplicate file {target!r}"
            )
        declared[target] = record
        records.append(record)

    payload_names = {name[len(PAYLOAD_DIR) + 1 :] for name in payload}
    missing = sorted(set(declared) - payload_names)
    extra = sorted(payload_names - set(declared))
    if missing:
        raise ArtifactError(
            f"artifact {path} is missing payload files: {', '.join(missing[:5])}"
        )
    if extra:
        raise ArtifactError(
            f"artifact {path} contains undeclared files: {', '.join(extra[:5])}"
        )

    for target, record in declared.items():
        data = payload[PAYLOAD_DIR + "/" + target]
        if len(data) != record.size:
            raise ArtifactError(
                f"artifact {path}: size mismatch for {target!r} "
                f"(metadata {record.size}, actual {len(data)})"
            )
        actual = hash_bytes(data)
        if actual != record.digest:
            raise ArtifactError(
                f"artifact {path}: digest mismatch for {target!r}"
            )

    return Artifact(
        package=package,
        files=tuple(sorted(records, key=lambda item: item.target)),
        content_digest=_content_digest(package, records),
        artifact_digest=hash_file(path),
        path=path,
    )


def verify_artifact(path: Path | str) -> Artifact:
    return read_artifact(path, verify=True)
