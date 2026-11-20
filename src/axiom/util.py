"""Filesystem and encoding helpers.

The two properties everything else depends on live here:

* **Atomicity** - every state transition is written to a temporary name in the
  destination directory and then moved into place with :func:`os.replace`.  A
  reader therefore sees either the old state or the new state, never a partial
  one, even across a crash.
* **Determinism** - JSON written for hashing always uses sorted keys, compact
  separators and UTF-8, so equal values always produce equal digests.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import posixpath
import secrets
import shutil
import stat
import time
from pathlib import Path, PurePosixPath

from .errors import AxiomError

__all__ = [
    "FileLock",
    "atomic_symlink",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "canonical_json",
    "default_root",
    "ensure_dir",
    "format_bytes",
    "fsync_dir",
    "hash_bytes",
    "hash_file",
    "now_iso",
    "remove_tree",
    "safe_join",
    "short_digest",
    "temp_name",
]


def default_root() -> Path:
    """Return the store root, honouring ``AXIOM_ROOT``."""
    override = os.environ.get("AXIOM_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".axiom"


def ensure_dir(path: Path | str, mode: int = 0o755) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    return path


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def temp_name(directory: Path | str, prefix: str = ".tmp-") -> Path:
    """Return an unused temporary path inside ``directory``."""
    directory = Path(directory)
    while True:
        candidate = directory / f"{prefix}{os.getpid()}-{secrets.token_hex(6)}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate


def fsync_dir(path: Path | str) -> None:
    """Flush a directory entry so a rename survives a crash.

    Some filesystems (and some platforms) do not support fsync on directories;
    that is not fatal because :func:`os.replace` is still atomic there.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # pragma: no cover - platform dependent
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.ENOTSUP):
            raise
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path | str, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` atomically."""
    path = Path(path)
    ensure_dir(path.parent)
    tmp = temp_name(path.parent)
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_text(
    path: Path | str, text: str, mode: int = 0o644, *, newline: bool = False
) -> None:
    if newline and not text.endswith("\n"):
        text += "\n"
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path | str, value: object, mode: int = 0o644) -> None:
    atomic_write_text(path, canonical_json(value) + "\n", mode=mode)


def atomic_symlink(target: str, link: Path | str) -> None:
    """Atomically make ``link`` point at ``target``.

    The new symlink is created under a temporary name in the link's directory
    and moved over the old one, so ``link`` never disappears.
    """
    link = Path(link)
    ensure_dir(link.parent)
    tmp = temp_name(link.parent, prefix=".symlink-")
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link)
        fsync_dir(link.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path | str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def safe_join(root: Path | str, relative: str) -> Path:
    """Join ``relative`` onto ``root``, refusing escapes.

    Absolute paths, ``..`` components and empty components are rejected.  The
    check is purely lexical; callers must still avoid following symlinks inside
    the destination tree while materialising files.
    """
    if not relative:
        raise AxiomError("empty path is not allowed")
    if relative.startswith("/"):
        raise AxiomError(f"absolute path is not allowed: {relative!r}")
    if "\\" in relative:
        raise AxiomError(f"backslashes are not allowed in paths: {relative!r}")
    if any(ord(char) < 32 for char in relative):
        raise AxiomError(f"control characters are not allowed in paths: {relative!r}")
    normalised = posixpath.normpath(relative)
    if normalised in ("", ".", "..") or normalised.startswith("../"):
        raise AxiomError(f"path escapes its root: {relative!r}")
    parts = PurePosixPath(normalised).parts
    if any(part in ("..", "") for part in parts):
        raise AxiomError(f"path escapes its root: {relative!r}")
    return Path(root).joinpath(*parts)


def remove_tree(path: Path | str) -> None:
    """Remove a tree that may contain read-only files."""

    def _onerror(func, target, exc_info):  # pragma: no cover - defensive
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IRWXU)
            func(target)

    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path, onerror=_onerror)


def short_digest(digest: str, length: int = 12) -> str:
    """Shorten ``sha256:<hex>`` or a bare hex digest for display."""
    text = digest.split(":", 1)[-1]
    return text[:length]


def format_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{count} B"  # pragma: no cover - unreachable


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FileLock:
    """An advisory exclusive lock on a file, held for the life of the context.

    Locks are per-profile (and one for the store) so that two ``axiom install``
    invocations cannot interleave their writes.  The lock is released by the
    kernel if the process dies, which is what makes crash recovery safe.
    """

    def __init__(self, path: Path | str, timeout: float | None = 30.0):
        self.path = Path(path)
        self.timeout = timeout
        self._handle = None

    def acquire(self) -> FileLock:
        ensure_dir(self.path.parent)
        handle = open(self.path, "a+")  # noqa: SIM115 - held for the lock's lifetime
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    handle.close()
                    raise AxiomError(
                        f"another axiom process holds the lock at {self.path}"
                    ) from None
                time.sleep(0.05)
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
