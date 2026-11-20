"""End-to-end scenarios: reproducibility, conflicts and upgrades."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from axiom.errors import ResolutionError
from axiom.installer import (
    install_environment,
    install_snapshot,
    plan_environment,
    resolve_environment,
)
from axiom.manifest import load_environment
from axiom.profile import Profile
from axiom.snapshot import Snapshot
from axiom.store import Store
from tests import *
from tests.helpers import publish, write_environment, write_package


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.packages = self.base / "packages"
        self._write_ecosystem()

    def _write_ecosystem(self):
        write_package(
            self.packages / "hello-1.0.0",
            "hello",
            "1.0.0",
            files={"bin/hello": ("#!/bin/sh\necho hello\n", "0755")},
        )
        write_package(
            self.packages / "libmath-1.2.0",
            "libmath",
            "1.2.0",
            files={"lib/libmath.py": "VALUE = 1\n"},
            provides=["math"],
        )
        write_package(
            self.packages / "libmath-1.3.0",
            "libmath",
            "1.3.0",
            files={"lib/libmath.py": "VALUE = 2\n"},
            provides=["math"],
        )
        write_package(
            self.packages / "libmath-2.0.0",
            "libmath",
            "2.0.0",
            files={"lib/libmath.py": "VALUE = 3\n"},
            provides=["math"],
        )
        write_package(
            self.packages / "app-2.0.0",
            "app",
            "2.0.0",
            dependencies=["hello ^1.0.0", "libmath ^1.0.0"],
            files={"bin/app": ("#!/bin/sh\n", "0755")},
        )
        write_package(
            self.packages / "legacy-0.9.0",
            "legacy",
            "0.9.0",
            conflicts=["app >=2.0.0"],
            files={"share/legacy.txt": "legacy\n"},
        )

    def make_store(self, name: str) -> Store:
        store = Store(self.base / name).initialize()
        for directory in sorted(self.packages.iterdir()):
            publish(store, directory)
        return store

    def environment(self, name="dev", packages=None, path=None, **kwargs):
        return write_environment(
            path or self.base / f"{name}.toml",
            name,
            packages or {"app": "^2.0.0"},
            **kwargs,
        )

    def test_transitive_resolution_selects_one_version_per_package(self):
        store = self.make_store("store")
        env_path = self.environment()
        plan = plan_environment(load_environment(env_path), store)
        selected = {package.name: str(package.version) for package in plan.snapshot.packages}
        self.assertEqual(
            selected,
            {"app": "2.0.0", "hello": "1.0.0", "libmath": "1.3.0"},
        )
        targets = {record.target for record in plan.snapshot.files()}
        self.assertEqual(targets, {"bin/app", "bin/hello", "lib/libmath.py"})

    def test_same_environment_is_reproducible_in_another_store(self):
        store_a = self.make_store("store-a")
        store_b = self.make_store("store-b")
        env_path = self.environment(packages={"app": "^2.0.0", "hello": "^1.0.0"})
        environment = load_environment(env_path)

        profile_a = Profile(store_a.root, "dev").initialize()
        result = install_environment(environment, store_a, profile_a)
        lockfile = self.base / "axiom.lock.json"
        result.plan.snapshot.write_lockfile(lockfile)
        pinned = Snapshot.read_lockfile(lockfile)

        profile_b = Profile(store_b.root, "dev").initialize()
        install_snapshot(
            pinned,
            store_b,
            profile_b,
            environment=load_environment(env_path),
        )

        self.assertEqual(
            profile_a.current_generation().digest,
            profile_b.current_generation().digest,
        )
        self.assertEqual(
            profile_a.current_generation().snapshot.to_dict(),
            profile_b.current_generation().snapshot.to_dict(),
        )
        for record in pinned.files():
            left = (profile_a.current_generation().tree / record.target).read_bytes()
            right = (profile_b.current_generation().tree / record.target).read_bytes()
            self.assertEqual(left, right)

        # Re-resolving in either store gives the same digest.
        self.assertEqual(
            resolve_environment(environment, store_b).snapshot.digest,
            result.plan.snapshot.digest,
        )

    def test_virtual_package_requirement_is_satisfied_by_provider(self):
        store = self.make_store("store")
        env_path = self.environment(
            "virtual", {"math": "^1.0.0", "app": "^2.0.0"}
        )
        plan = plan_environment(load_environment(env_path), store)
        names = {package.name: str(package.version) for package in plan.snapshot.packages}
        self.assertEqual(names["libmath"], "1.3.0")

    def test_conflicting_environment_fails_with_an_explanation(self):
        store = self.make_store("store")
        env_path = self.environment(
            "conflict", {"app": "^2.0.0", "legacy": "^0.9.0"}
        )
        with self.assertRaises(ResolutionError) as caught:
            plan_environment(load_environment(env_path), store)
        text = caught.exception.render()
        self.assertIn("legacy", text.lower())
        self.assertIn("conflict", text.lower())

    def test_upgrade_changes_the_snapshot_and_rollback_restores_it(self):
        store = self.make_store("store")
        profile = Profile(store.root, "dev").initialize()
        environment = load_environment(
            self.environment("dev", {"app": "^2.0.0", "libmath": "=1.2.0"})
        )
        before = install_environment(environment, store, profile)
        old_digest = before.snapshot.digest
        old_content = (
            profile.current_generation().tree / "lib" / "libmath.py"
        ).read_text()

        upgraded_env = load_environment(
            write_environment(
                self.base / "upgraded.toml",
                "dev",
                {"app": "^2.0.0", "libmath": "=1.3.0"},
            )
        )
        after = install_environment(upgraded_env, store, profile)
        self.assertNotEqual(after.snapshot.digest, old_digest)
        self.assertEqual(
            (profile.current_generation().tree / "lib" / "libmath.py").read_text(),
            "VALUE = 2\n",
        )

        profile.rollback()
        self.assertEqual(profile.current_generation().digest, old_digest)
        self.assertEqual(
            (profile.current_generation().tree / "lib" / "libmath.py").read_text(),
            old_content,
        )
        self.assertEqual(
            (profile.current_generation().tree / "bin" / "app").stat().st_mode & 0o777,
            0o755,
        )

    def test_gc_preserves_every_active_generation(self):
        store = self.make_store("store")
        environment = load_environment(self.environment())
        profile = Profile(store.root, "dev").initialize()
        install_environment(environment, store, profile)
        keep = {
            record.digest
            for generation in profile.generations()
            for record in generation.snapshot.files()
        }
        store.gc(keep=keep)
        for record in profile.current_generation().snapshot.files():
            self.assertTrue(store.has_blob(record.digest))


if __name__ == "__main__":
    unittest.main()
