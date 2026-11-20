"""Snapshots: identity, diffs and lockfile round-trips."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from axiom.errors import AxiomError, EnvironmentError
from axiom.model import Environment, Package, PackageFile, Requirement
from axiom.snapshot import (
    LOCKFILE_FORMAT,
    Snapshot,
    SnapshotFile,
    SnapshotPackage,
    snapshot_from_solution,
)
from axiom.solver import Solution
from axiom.store import StoreEntry
from axiom.util import hash_bytes
from axiom.version import parse_version
from tests import *


def package(name, version, **kwargs) -> Package:
    return Package(name=name, version=parse_version(version), **kwargs)


def entry(pkg: Package, files: dict | None = None) -> StoreEntry:
    files = files or {
        f"share/{pkg.name}.txt": (f"{pkg.identity}\n".encode(), "0644")
    }
    records = []
    for target, spec in files.items():
        data, mode = spec if isinstance(spec, tuple) else (spec, "0644")
        records.append(
            PackageFile(
                target=target,
                digest=hash_bytes(data),
                mode=mode,
                size=len(data),
            )
        )
    records = tuple(records)
    return StoreEntry(
        package=pkg,
        files=records,
        content_digest="sha256:" + hash_bytes(pkg.identity.encode()),
        artifact_digest=hash_bytes(b"artifact:" + pkg.identity.encode()),
    )


def build_snapshot(
    packages,
    *,
    environment="dev",
    variables=None,
    files=None,
) -> Snapshot:
    files = files or {}
    entries = {}
    selected = []
    for pkg in packages:
        pkg_files = files.get(pkg.name)
        entries[pkg.name] = entry(pkg, pkg_files)
        selected.append(pkg)
    env = Environment(
        name=environment,
        packages=tuple(Requirement(pkg.name) for pkg in packages),
        variables=variables or {},
    )
    solution = Solution(packages=tuple(selected))
    return snapshot_from_solution(env, solution, lambda item: entries[item.name])


class SnapshotTests(unittest.TestCase):
    def test_snapshot_records_packages_and_files(self):
        snapshot = build_snapshot(
            [package("hello", "1.0.0")],
            variables={"A": "1"},
        )
        self.assertEqual(snapshot.environment, "dev")
        self.assertEqual(len(snapshot.packages), 1)
        self.assertEqual(snapshot.variables, {"A": "1"})
        self.assertEqual(len(snapshot.files()), 1)
        self.assertEqual(snapshot.total_size(), len("hello@1.0.0\n"))
        self.assertTrue(snapshot.digest.startswith("sha256:"))
        self.assertEqual(len(snapshot.short_digest), 12)

    def test_digest_is_order_independent(self):
        one = package("a", "1.0.0")
        two = package("b", "1.0.0")
        forward = build_snapshot([one, two])
        backward = build_snapshot([two, one])
        self.assertEqual(forward.digest, backward.digest)
        self.assertEqual(forward.to_dict(), backward.to_dict())

    def test_digest_changes_when_content_changes(self):
        first = build_snapshot([package("a", "1.0.0")])
        second = build_snapshot([package("a", "1.0.1")])
        self.assertNotEqual(first.digest, second.digest)
        third = build_snapshot(
            [package("a", "1.0.0")],
            files={"a": {"share/a.txt": (b"different", "0644")}},
        )
        self.assertNotEqual(first.digest, third.digest)

    def test_digest_changes_when_variables_change(self):
        first = build_snapshot([package("a", "1.0.0")], variables={"X": "1"})
        second = build_snapshot([package("a", "1.0.0")], variables={"X": "2"})
        self.assertNotEqual(first.digest, second.digest)

    def test_duplicate_install_target_is_rejected(self):
        one = package("a", "1.0.0")
        two = package("b", "1.0.0")
        files = {
            "a": {"bin/tool": (b"a", "0755")},
            "b": {"bin/tool": (b"b", "0755")},
        }
        with self.assertRaises(AxiomError) as caught:
            build_snapshot([one, two], files=files)
        self.assertIn("conflict", str(caught.exception))

    def test_file_and_directory_collision_is_rejected(self):
        one = package("a", "1.0.0")
        two = package("b", "1.0.0")
        files = {
            "a": {"bin/tool": (b"file", "0755")},
            "b": {"bin/tool/extra": (b"nested", "0644")},
        }
        with self.assertRaises(AxiomError) as caught:
            build_snapshot([one, two], files=files)
        self.assertIn("install conflict", str(caught.exception))

    def test_diff_reports_add_remove_change(self):
        before = build_snapshot(
            [package("a", "1.0.0"), package("b", "1.0.0")],
            variables={"X": "1"},
        )
        after = build_snapshot(
            [package("a", "1.0.1"), package("c", "2.0.0")],
            variables={"X": "2"},
        )
        diff = before.diff(after)
        self.assertFalse(diff.is_empty)
        self.assertEqual(diff.added_packages, ("c",))
        self.assertEqual(diff.removed_packages, ("b",))
        self.assertEqual(diff.changed_packages, ("a",))
        self.assertEqual(diff.variables_changed, ("X",))
        self.assertTrue(any(change.target == "share/b.txt" for change in diff.file_changes))
        self.assertIn("+ package c", diff.render())

    def test_identical_diff(self):
        snapshot = build_snapshot([package("a", "1.0.0")])
        diff = snapshot.diff(snapshot)
        self.assertTrue(diff.is_empty)
        self.assertIn("identical", diff.render())

    def test_file_change_detects_mode_only(self):
        before = build_snapshot(
            [package("a", "1.0.0")],
            files={"a": {"bin/x": (b"same", "0644")}},
        )
        after = build_snapshot(
            [package("a", "1.0.0")],
            files={"a": {"bin/x": (b"same", "0755")}},
        )
        changes = before.diff(after).file_changes
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].action, "change")

    def test_lockfile_roundtrip(self):
        snapshot = build_snapshot([package("a", "1.0.0")], variables={"X": "1"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "axiom.lock.json"
            snapshot.write_lockfile(path)
            data = json.loads(path.read_text())
            self.assertEqual(data["lockfile_version"], LOCKFILE_FORMAT)
            self.assertEqual(data["digest"], snapshot.digest)
            loaded = Snapshot.read_lockfile(path)
            self.assertEqual(loaded.digest, snapshot.digest)
            self.assertEqual(loaded.to_dict(), snapshot.to_dict())

    def test_lockfile_digest_tampering_is_detected(self):
        snapshot = build_snapshot([package("a", "1.0.0")])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "axiom.lock.json"
            snapshot.write_lockfile(path)
            data = json.loads(path.read_text())
            data["snapshot"]["variables"]["injected"] = "yes"
            path.write_text(json.dumps(data))
            with self.assertRaises(EnvironmentError):
                Snapshot.read_lockfile(path)

    def test_missing_lockfile(self):
        with self.assertRaises(EnvironmentError):
            Snapshot.read_lockfile("/nonexistent/axiom.lock.json")


class SnapshotValidationTests(unittest.TestCase):
    def test_file_target_validation(self):
        for target in ("/etc/passwd", "../escape", "a/../../b", "bad\x00name"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                SnapshotFile(target=target, digest="a" * 64)

    def test_digest_and_mode_normalisation(self):
        record = SnapshotFile(target="a", digest="b" * 64, mode="755", size="2")
        self.assertEqual(record.mode, "0755")
        self.assertEqual(record.size, 2)
        with self.assertRaises(ValueError):
            SnapshotFile(target="a", digest="short")

    def test_snapshot_rejects_duplicate_packages(self):
        package_one = SnapshotPackage(name="a", version=parse_version("1.0.0"), digest="")
        with self.assertRaises(ValueError):
            Snapshot(environment="dev", packages=(package_one, package_one))

    def test_snapshot_rejects_duplicate_targets(self):
        file_one = SnapshotFile(target="x", digest="a" * 64)
        package_one = SnapshotPackage(
            name="a", version=parse_version("1.0.0"), digest="", files=(file_one,)
        )
        package_two = SnapshotPackage(
            name="b", version=parse_version("1.0.0"), digest="", files=(file_one,)
        )
        with self.assertRaises(ValueError):
            Snapshot(environment="dev", packages=(package_one, package_two))

    def test_invalid_snapshot_dict(self):
        with self.assertRaises(EnvironmentError):
            Snapshot.from_dict({"environment": "dev", "packages": [{"name": "a"}]})
        with self.assertRaises(EnvironmentError):
            Snapshot.from_dict({"packages": []})


if __name__ == "__main__":
    unittest.main()
