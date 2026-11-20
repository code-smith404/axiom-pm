"""Atomic activation, rollback, verification and pruning."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from axiom.errors import AxiomError, ProfileError
from axiom.installer import install_environment, resolve_environment
from axiom.manifest import load_environment
from axiom.profile import Profile
from axiom.snapshot import Snapshot, SnapshotFile, SnapshotPackage
from axiom.store import Store
from axiom.version import parse_version
from tests import *
from tests.helpers import publish, write_environment, write_package


class ProfileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / "root").initialize()
        self.profile = Profile(self.store.root, "test").initialize()

    def publish_version(self, version: str, *, package="state", content=None, env_name="test"):
        directory = write_package(
            self.base / "packages" / f"{package}-{version}",
            package,
            version,
            files={
                f"share/{package}.txt": (
                    content if content is not None else f"{version}\n",
                    "0644",
                ),
                f"bin/{package}": ("#!/bin/sh\n", "0755"),
            },
        )
        entry = publish(self.store, directory)
        env_path = write_environment(
            self.base / f"env-{package}-{version}.toml",
            env_name,
            {package: f"={version}"},
        )
        return entry, env_path

    def install_version(self, version: str, **kwargs):
        entry, env_path = self.publish_version(version, **kwargs)
        result = install_environment(
            load_environment(env_path), self.store, self.profile
        )
        return result, entry, env_path

    def snapshot_for(self, env_path) -> Snapshot:
        return resolve_environment(load_environment(env_path), self.store).snapshot

    def assert_current_content(self, expected: str):
        current = self.profile.current_generation()
        self.assertIsNotNone(current)
        data = (current.tree / "share" / "state.txt").read_text()
        self.assertEqual(data, expected)
        return current


class InstallationTests(ProfileTestCase):
    def test_install_activates_a_complete_generation(self):
        result, entry, _env = self.install_version("1.0.0")
        self.assertTrue(result.changed)
        self.assertEqual(result.generation.sequence, 1)
        self.assertTrue(self.profile.current_link.is_symlink())
        self.assertEqual(
            os.readlink(self.profile.current_link),
            f"generations/{result.generation.name}",
        )
        current = self.assert_current_content("1.0.0\n")
        self.assertEqual(current.digest, result.snapshot.digest)
        installed = current.tree / "bin" / "state"
        self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
        self.assertEqual(installed.read_text(), "#!/bin/sh\n")
        self.assertEqual(self.profile.status().generation_count, 1)
        # The store blob is independent of the installed copy.
        blob = self.store.blob_path(entry.files[0].digest)
        self.assertEqual(blob.stat().st_mode & 0o777, 0o444)

    def test_reinstalling_same_snapshot_is_a_noop(self):
        first, _entry, env_path = self.install_version("1.0.0")
        second = install_environment(
            load_environment(env_path), self.store, self.profile
        )
        self.assertFalse(second.changed)
        self.assertEqual(first.generation.name, second.generation.name)
        self.assertEqual(self.profile.status().generation_count, 1)
        self.assertEqual(len(self.profile.history()), 1)

    def test_upgrade_then_rollback(self):
        first, _entry, _env = self.install_version("1.0.0")
        second, _entry2, _env2 = self.install_version("2.0.0")
        self.assertEqual(self.assert_current_content("2.0.0\n").name, second.generation.name)
        self.assertEqual(second.generation.sequence, 2)

        rolled = self.profile.rollback()
        self.assertEqual(rolled.name, first.generation.name)
        self.assertEqual(self.assert_current_content("1.0.0\n").name, first.generation.name)
        self.assertEqual(
            os.readlink(self.profile.previous_link),
            f"generations/{second.generation.name}",
        )
        actions = [entry["action"] for entry in self.profile.history()]
        self.assertEqual(actions, ["install", "install", "rollback"])
        # The old generation was not rewritten: it still exists intact.
        self.assertTrue((first.generation.path / "files" / "share" / "state.txt").is_file())

    def test_rollback_to_explicit_generation(self):
        first, _e, _env = self.install_version("1.0.0")
        self.install_version("2.0.0")
        self.install_version("3.0.0")
        rolled = self.profile.rollback(str(first.generation.sequence))
        self.assertEqual(rolled.name, first.generation.name)
        self.assert_current_content("1.0.0\n")

    def test_rollback_without_history(self):
        self.install_version("1.0.0")
        with self.assertRaises(ProfileError):
            self.profile.rollback()

    def test_rollback_to_current_is_refused(self):
        self.install_version("1.0.0")
        with self.assertRaises(ProfileError):
            self.profile.rollback("current")

    def test_new_install_after_rollback_gets_a_new_sequence(self):
        first, _e, _env = self.install_version("1.0.0")
        second, _e2, _env2 = self.install_version("2.0.0")
        self.profile.rollback()
        third, _e3, _env3 = self.install_version("3.0.0")
        self.assertEqual(third.generation.sequence, 3)
        self.assertEqual(third.generation.parent, first.snapshot.digest)
        self.assertNotEqual(third.generation.name, second.generation.name)
        self.assert_current_content("3.0.0\n")


class FailureSafetyTests(ProfileTestCase):
    def test_missing_blob_aborts_without_activation(self):
        first, _entry, _env = self.install_version("1.0.0")
        bad = Snapshot(
            environment="bad",
            packages=(
                SnapshotPackage(
                    name="ghost",
                    version=parse_version("1.0.0"),
                    digest="",
                    files=(
                        SnapshotFile(
                            target="share/ghost.txt",
                            digest="0" * 64,
                            size=1,
                            package="ghost@1.0.0",
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaises(ProfileError):
            self.profile.install(bad, self.store)
        self.assertEqual(self.profile.current_generation().name, first.generation.name)
        self.assert_current_content("1.0.0\n")
        staging = [
            item.name
            for item in self.profile.generations_dir.iterdir()
            if item.name.startswith(".staging-")
        ]
        self.assertEqual(staging, [])
        self.assertEqual(self.profile.status().generation_count, 1)

    def test_activation_failure_leaves_current_untouched(self):
        first, _entry, _env_path = self.install_version("1.0.0")
        # A different snapshot is needed for the install to attempt activation.
        _second_entry, second_env = self.publish_version("2.0.0")
        snapshot_two = self.snapshot_for(second_env)
        with mock.patch.object(
            Profile, "_activate", side_effect=ProfileError("simulated crash")
        ), self.assertRaises(ProfileError):
            self.profile.install(snapshot_two, self.store)
        self.assertEqual(self.profile.current_generation().name, first.generation.name)
        self.assert_current_content("1.0.0\n")
        # The new generation was fully materialised before activation was
        # attempted; it remains on disk but is not active.
        self.assertEqual(self.profile.status().generation_count, 2)
        self.assertNotEqual(self.profile.current_generation().digest, snapshot_two.digest)

    def test_staging_is_removed_after_materialisation_error(self):
        self.install_version("1.0.0")
        _entry, env_path = self.publish_version("2.0.0")
        snapshot = self.snapshot_for(env_path)
        from axiom.profile import Profile as ProfileClass

        def explode(self, staging, snapshot_, store, *, verify_blobs):
            raise ProfileError("simulated materialisation failure")

        with mock.patch.object(ProfileClass, "_materialize", explode), self.assertRaises(
            ProfileError
        ):
            self.profile.install(snapshot, self.store)
        self.assertEqual(
            [
                item.name
                for item in self.profile.generations_dir.iterdir()
                if item.name.startswith(".staging-")
            ],
            [],
        )

    def test_exclusive_profile_lock(self):
        lock = self.profile.lock()
        lock.acquire()
        try:
            with self.assertRaises(AxiomError):
                self.profile.lock(timeout=0.1).acquire()
        finally:
            lock.release()
        # Lock is reacquirable after release.
        self.profile.lock(timeout=0.1).release()


class VerificationTests(ProfileTestCase):
    def test_healthy_generation(self):
        self.install_version("1.0.0")
        report = self.profile.verify(deep=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.checked_files, 2)
        self.assertGreater(report.checked_bytes, 0)

    def test_detects_modified_content(self):
        self.install_version("1.0.0")
        path = self.profile.current_generation().tree / "share" / "state.txt"
        data = path.read_bytes()
        os.chmod(path, 0o644)
        path.write_bytes(data[:-1] + b"X")
        report = self.profile.verify(deep=True)
        self.assertFalse(report.ok)
        self.assertTrue(any("digest mismatch" in item for item in report.problems))

    def test_detects_mode_change(self):
        self.install_version("1.0.0")
        path = self.profile.current_generation().tree / "bin" / "state"
        os.chmod(path, 0o700)
        report = self.profile.verify(deep=True)
        self.assertFalse(report.ok)
        self.assertTrue(any("mode mismatch" in item for item in report.problems))

    def test_detects_extra_file(self):
        self.install_version("1.0.0")
        tree = self.profile.current_generation().tree
        (tree / "share" / "extra.txt").write_text("unexpected")
        report = self.profile.verify(deep=False)
        self.assertFalse(report.ok)
        self.assertTrue(any("unexpected file" in item for item in report.problems))

    def test_ignores_runtime_bytecode_caches(self):
        self.install_version("1.0.0")
        tree = self.profile.current_generation().tree
        cache = tree / "lib" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "module.cpython-311.pyc").write_bytes(b"bytecode")
        self.assertTrue(self.profile.verify(deep=False).ok)
        strict = self.profile.verify(deep=False, strict=True)
        self.assertFalse(strict.ok)
        self.assertTrue(any("__pycache__" in item for item in strict.problems))

    def test_detects_missing_file(self):
        self.install_version("1.0.0")
        tree = self.profile.current_generation().tree
        (tree / "share" / "state.txt").unlink()
        report = self.profile.verify(deep=False)
        self.assertFalse(report.ok)
        self.assertTrue(any("missing file" in item for item in report.problems))

    def test_shallow_verification_skips_hashing(self):
        self.install_version("1.0.0")
        path = self.profile.current_generation().tree / "share" / "state.txt"
        data = path.read_bytes()
        os.chmod(path, 0o644)
        path.write_bytes(data[:-1] + b"X")
        self.assertTrue(self.profile.verify(deep=False).ok)

    def test_verify_another_generation(self):
        first, _e, _env = self.install_version("1.0.0")
        self.install_version("2.0.0")
        report = self.profile.verify(reference=str(first.generation.sequence))
        self.assertTrue(report.ok)
        self.assertEqual(report.generation, first.generation.name)


class GenerationAdministrationTests(ProfileTestCase):
    def test_prune_keeps_current_and_recent(self):
        installed = []
        for version in ("1.0.0", "2.0.0", "3.0.0"):
            result, _entry, _env = self.install_version(version)
            installed.append(result.generation)
        removed = self.profile.prune(keep=2)
        self.assertEqual(removed, [installed[0].name])
        self.assertEqual(
            [generation.name for generation in self.profile.generations()],
            [installed[1].name, installed[2].name],
        )
        self.assertEqual(
            self.profile.current_generation().name, installed[2].name
        )
        # History no longer mentions the pruned generation.
        self.assertNotIn(
            installed[0].name,
            [entry["generation"] for entry in self.profile.history()],
        )

    def test_prune_dry_run(self):
        for version in ("1.0.0", "2.0.0", "3.0.0"):
            self.install_version(version)
        removed = self.profile.prune(keep=2, dry_run=True)
        self.assertEqual(len(removed), 1)
        self.assertEqual(self.profile.status().generation_count, 3)

    def test_prune_rejects_tiny_keep(self):
        with self.assertRaises(ProfileError):
            self.profile.prune(keep=1)

    def test_corrupt_generation_metadata_is_reported(self):
        result, _entry, _env = self.install_version("1.0.0")
        metadata = result.generation.path / "GENERATION.json"
        data = json.loads(metadata.read_text())
        data["snapshot_digest"] = "sha256:" + "0" * 64
        metadata.write_text(json.dumps(data))
        with self.assertRaises(ProfileError):
            self.profile.generations()

    def test_resolve_references(self):
        first, _e, _env = self.install_version("1.0.0")
        second, _e2, _env2 = self.install_version("2.0.0")
        self.assertEqual(self.profile.resolve_reference("current").name, second.generation.name)
        self.assertEqual(self.profile.resolve_reference("previous").name, first.generation.name)
        self.assertEqual(self.profile.resolve_reference("1").name, first.generation.name)
        self.assertEqual(
            self.profile.resolve_reference(first.generation.name).name,
            first.generation.name,
        )
        prefix = first.snapshot.digest.split(":", 1)[1][:6]
        self.assertEqual(self.profile.resolve_reference(prefix).name, first.generation.name)
        with self.assertRaises(ProfileError):
            self.profile.resolve_reference("999")
        with self.assertRaises(ProfileError):
            self.profile.resolve_reference("zzzz")

    def test_environment_variables(self):
        _entry, env_path = self.publish_version("1.0.0")
        env_path = write_environment(
            env_path,
            "test",
            {"state": "=1.0.0"},
            variables={"FOO": "bar"},
        )
        install_environment(load_environment(env_path), self.store, self.profile)
        variables = self.profile.environment()
        self.assertEqual(variables["FOO"], "bar")
        self.assertEqual(variables["AXIOM_PROFILE"], "test")
        self.assertTrue(variables["AXIOM_GENERATION"].startswith("000001-"))
        self.assertTrue(variables["PATH"].startswith(str(self.profile.current_link / "files" / "bin")))
        snippet = self.profile.shell_snippet()
        self.assertIn("export FOO='bar'", snippet)
        self.assertIn("export AXIOM_PROFILE='test'", snippet)


class ConcurrentVisibilityTests(ProfileTestCase):
    def test_readers_never_see_a_partial_generation(self):
        first_entry, first_env = self.publish_version("1.0.0")
        _second_entry, second_env = self.publish_version("2.0.0")
        install_environment(load_environment(first_env), self.store, self.profile)
        snapshots = [
            self.snapshot_for(first_env),
            self.snapshot_for(second_env),
        ]

        failures = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    link = self.profile.current_link
                    content = (link / "files" / "share" / "state.txt").read_text()
                    if content not in ("1.0.0\n", "2.0.0\n"):
                        failures.append(f"unexpected content: {content!r}")
                except FileNotFoundError as exc:
                    failures.append(f"missing current generation: {exc}")
                except OSError as exc:  # pragma: no cover - race reporting
                    failures.append(f"reader error: {exc}")

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for index in range(12):
                self.profile.install(snapshots[index % 2], self.store)
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
