"""Deterministic artifact building and defensive reading."""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from axiom.artifact import (
    ARTIFACT_FORMAT,
    META_NAME,
    PAYLOAD_DIR,
    build_artifact,
    default_artifact_name,
    read_artifact,
    verify_artifact,
)
from axiom.errors import ArtifactError
from tests import *
from tests.helpers import write_package


def tar_entries(path: Path) -> list:
    entries = []
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            data = archive.extractfile(member).read() if member.isfile() else b""
            entries.append((member.name, member, data))
    return entries


def rewrite_artifact(source: Path, destination: Path, *, replace=None, drop=()):
    """Rewrite an artifact, replacing or dropping payload entries."""
    replace = replace or {}
    with tarfile.open(source, "r:*") as source_archive, tarfile.open(
        destination, "w", format=tarfile.PAX_FORMAT
    ) as destination_archive:
        for member in source_archive:
            if member.name in drop:
                continue
            data = source_archive.extractfile(member).read() if member.isfile() else b""
            if member.name in replace:
                data = replace[member.name]
            member.size = len(data)
            destination_archive.addfile(member, io.BytesIO(data))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_build_is_byte_for_byte_reproducible(self):
        package_dir = write_package(
            self.base / "pkg",
            "hello",
            "1.0.0",
            files={
                "bin/hello": ("#!/bin/sh\n", "0755"),
                "share/message.txt": "hi\n",
            },
        )
        first = build_artifact(package_dir, self.base / "one.axp")
        second = build_artifact(package_dir, self.base / "two.axp")
        self.assertEqual(first.artifact_digest, second.artifact_digest)
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual(
            (self.base / "one.axp").read_bytes(),
            (self.base / "two.axp").read_bytes(),
        )
        self.assertEqual(
            [record.target for record in first.files],
            ["bin/hello", "share/message.txt"],
        )
        self.assertEqual(first.files[0].mode, "0755")
        self.assertEqual(default_artifact_name(first.package), "hello-1.0.0.axp")

    def test_archive_metadata_is_normalised(self):
        package_dir = write_package(
            self.base / "pkg", "hello", "1.0.0", files={"a.txt": "a"}
        )
        build_artifact(package_dir, self.base / "one.axp")
        entries = tar_entries(self.base / "one.axp")
        names = [name for name, _member, _data in entries]
        self.assertEqual(names[0], META_NAME)
        self.assertIn(f"{PAYLOAD_DIR}/a.txt", names)
        for name, member, _data in entries:
            self.assertEqual(member.mtime, 0, name)
            self.assertEqual(member.uid, 0, name)
            self.assertEqual(member.gid, 0, name)
            self.assertEqual(member.uname, "", name)
            self.assertEqual(member.gname, "", name)
        metadata = json.loads(entries[0][2])
        self.assertEqual(metadata["format"], ARTIFACT_FORMAT)
        self.assertEqual(metadata["package"]["name"], "hello")
        self.assertNotIn("created_at", metadata)

    def test_read_verifies_and_returns_records(self):
        package_dir = write_package(
            self.base / "pkg",
            "hello",
            "1.0.0",
            files={"bin/hello": ("#!/bin/sh\necho hi\n", "0755")},
        )
        built = build_artifact(package_dir, self.base / "one.axp")
        artifact = read_artifact(self.base / "one.axp")
        self.assertEqual(artifact.package.identity, "hello@1.0.0")
        self.assertEqual(artifact.content_digest, built.content_digest)
        self.assertEqual(artifact.artifact_digest, built.artifact_digest)
        self.assertEqual(artifact.total_size, len("#!/bin/sh\necho hi\n"))
        self.assertEqual(verify_artifact(self.base / "one.axp").package, artifact.package)

    def test_auto_discovers_payload_and_modes(self):
        package_dir = write_package(
            self.base / "pkg",
            "auto",
            "0.1.0",
            files={
                "lib/data.txt": "data",
                "bin/run": ("#!/bin/sh\n", "0755"),
            },
            auto_files=True,
        )
        artifact = build_artifact(package_dir, self.base / "one.axp")
        modes = {record.target: record.mode for record in artifact.files}
        self.assertEqual(modes["lib/data.txt"], "0644")
        self.assertEqual(modes["bin/run"], "0755")

    def test_renamed_install_target(self):
        package_dir = write_package(
            self.base / "pkg",
            "renamed",
            "1.0.0",
            files={"payload.txt": "content"},
            explicit_files=False,
            auto_files=True,
        )
        # Declare a target that differs from the payload path.
        (package_dir / "package.toml").write_text(
            "\n".join(
                [
                    'name = "renamed"',
                    'version = "1.0.0"',
                    "[[files]]",
                    'source = "payload.txt"',
                    'target = "share/different.txt"',
                ]
            )
            + "\n",
            "utf-8",
        )
        artifact = build_artifact(package_dir, self.base / "one.axp")
        self.assertEqual(artifact.files[0].target, "share/different.txt")
        self.assertIn(
            f"{PAYLOAD_DIR}/share/different.txt",
            [name for name, _m, _d in tar_entries(self.base / "one.axp")],
        )

    def test_missing_declared_file(self):
        package_dir = write_package(self.base / "pkg", "broken", "1.0.0")
        (package_dir / "files" / "share" / "broken.txt").unlink()
        with self.assertRaises(ArtifactError):
            build_artifact(package_dir, self.base / "bad.axp")

    def test_symlink_in_payload_is_rejected(self):
        package_dir = write_package(
            self.base / "pkg", "linked", "1.0.0", auto_files=True
        )
        target = package_dir / "files" / "share" / "link"
        target.symlink_to("broken.txt")
        with self.assertRaises(ArtifactError):
            build_artifact(package_dir, self.base / "bad.axp")

    def test_empty_payload_is_rejected(self):
        package_dir = write_package(
            self.base / "pkg", "empty", "1.0.0", files={}, auto_files=True
        )
        with self.assertRaises(ArtifactError):
            build_artifact(package_dir, self.base / "bad.axp")

    def test_duplicate_targets_are_rejected(self):
        package_dir = write_package(self.base / "pkg", "dup", "1.0.0")
        (package_dir / "package.toml").write_text(
            "\n".join(
                [
                    'name = "dup"',
                    'version = "1.0.0"',
                    "[[files]]",
                    'source = "share/dup.txt"',
                    'target = "same.txt"',
                    "[[files]]",
                    'source = "share/dup.txt"',
                    'target = "same.txt"',
                ]
            )
            + "\n",
            "utf-8",
        )
        with self.assertRaises(ArtifactError):
            build_artifact(package_dir, self.base / "bad.axp")


class ReadDefenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.package_dir = write_package(
            self.base / "pkg",
            "hello",
            "1.0.0",
            files={"bin/hello": ("#!/bin/sh\necho hi\n", "0755")},
        )
        self.artifact = build_artifact(self.package_dir, self.base / "good.axp")

    def test_payload_tampering_is_detected(self):
        rewrite_artifact(
            self.base / "good.axp",
            self.base / "bad.axp",
            replace={f"{PAYLOAD_DIR}/bin/hello": b"#!/bin/sh\necho HO\n"},
        )
        with self.assertRaises(ArtifactError) as caught:
            read_artifact(self.base / "bad.axp")
        self.assertIn("digest mismatch", str(caught.exception))

    def test_missing_payload_entry_is_detected(self):
        rewrite_artifact(
            self.base / "good.axp",
            self.base / "bad.axp",
            drop={f"{PAYLOAD_DIR}/bin/hello"},
        )
        with self.assertRaises(ArtifactError) as caught:
            read_artifact(self.base / "bad.axp")
        self.assertIn("missing payload", str(caught.exception))

    def test_undeclared_payload_entry_is_detected(self):
        with tarfile.open(self.base / "good.axp", "r:*") as archive, tarfile.open(
            self.base / "bad.axp", "w", format=tarfile.PAX_FORMAT
        ) as destination:
            for member in archive:
                data = archive.extractfile(member).read() if member.isfile() else b""
                destination.addfile(member, io.BytesIO(data))
            extra = tarfile.TarInfo(f"{PAYLOAD_DIR}/extra.txt")
            extra.size = 3
            destination.addfile(extra, io.BytesIO(b"hey"))
        with self.assertRaises(ArtifactError) as caught:
            read_artifact(self.base / "bad.axp")
        self.assertIn("undeclared", str(caught.exception))

    def test_metadata_pointing_at_wrong_digest_is_detected(self):
        with tarfile.open(self.base / "good.axp", "r:*") as archive:
            metadata = json.loads(
                archive.extractfile(META_NAME).read().decode("utf-8")
            )
        metadata["files"][0]["digest"] = "0" * 64
        rewrite_artifact(
            self.base / "good.axp",
            self.base / "bad.axp",
            replace={META_NAME: json.dumps(metadata).encode("utf-8")},
        )
        with self.assertRaises(ArtifactError) as caught:
            read_artifact(self.base / "bad.axp")
        self.assertIn("digest mismatch", str(caught.exception))

    def test_missing_metadata_is_detected(self):
        rewrite_artifact(
            self.base / "good.axp", self.base / "bad.axp", drop={META_NAME}
        )
        with self.assertRaises(ArtifactError) as caught:
            read_artifact(self.base / "bad.axp")
        self.assertIn("missing", str(caught.exception))

    def test_path_traversal_is_rejected(self):
        path = self.base / "evil.axp"
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo("../evil.txt")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(ArtifactError):
            read_artifact(path)

    def test_absolute_path_is_rejected(self):
        path = self.base / "evil.axp"
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo("/etc/passwd")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(ArtifactError):
            read_artifact(path)

    def test_symlink_member_is_rejected(self):
        path = self.base / "evil.axp"
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo(f"{PAYLOAD_DIR}/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with self.assertRaises(ArtifactError):
            read_artifact(path)

    def test_corrupt_archive_is_rejected(self):
        path = self.base / "corrupt.axp"
        path.write_bytes(b"this is not a tar archive")
        with self.assertRaises(ArtifactError):
            read_artifact(path)

    def test_missing_artifact(self):
        with self.assertRaises(ArtifactError):
            read_artifact(self.base / "nope.axp")


if __name__ == "__main__":
    unittest.main()
