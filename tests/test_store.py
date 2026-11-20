"""Content-addressed store behaviour."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path

from axiom.errors import StoreError
from axiom.store import Store
from axiom.version import parse_version
from tests import *
from tests.helpers import publish, write_package


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / "root").initialize()

    def package(self, name="hello", version="1.0.0", **kwargs):
        directory = write_package(
            self.base / "packages" / f"{name}-{version}", name, version, **kwargs
        )
        return publish(self.store, directory)


class BlobTests(StoreTestCase):
    def test_put_blob_is_content_addressed_and_read_only(self):
        digest = self.store.put_blob(b"hello world")
        self.assertEqual(len(digest), 64)
        path = self.store.blob_path(digest)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"hello world")
        self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertEqual(self.store.put_blob(b"hello world"), digest)
        self.assertEqual(len(list(self.store.blobs.rglob("*"))), 2)  # shard dir + blob

    def test_blob_sharding(self):
        digest = self.store.put_blob(b"x")
        self.assertEqual(self.store.blob_path(digest).parent.name, digest[:2])

    def test_stream_verifies_digest_and_size(self):
        data = b"streamed content"
        from axiom.util import hash_bytes

        digest = hash_bytes(data)
        result = self.store.put_blob_stream(io.BytesIO(data), digest, len(data))
        self.assertEqual(result, digest)
        self.assertTrue(self.store.has_blob(digest))
        with self.assertRaises(StoreError):
            self.store.put_blob_stream(io.BytesIO(data), "0" * 64, len(data))
        with self.assertRaises(StoreError):
            self.store.put_blob_stream(io.BytesIO(data), digest, len(data) + 1)

    def test_missing_blob(self):
        with self.assertRaises(StoreError):
            self.store.read_blob("a" * 64)

    def test_invalid_digest(self):
        with self.assertRaises(StoreError):
            self.store.blob_path("not-a-digest")


class RegistrationTests(StoreTestCase):
    def test_ingest_package_and_versions(self):
        entry = self.package()
        self.assertEqual(entry.package.identity, "hello@1.0.0")
        self.assertEqual(len(entry.files), 1)
        self.assertTrue(self.store.has_blob(entry.files[0].digest))
        self.assertEqual(self.store.versions("hello"), [parse_version("1.0.0")])
        self.assertIsNotNone(self.store.get_entry("hello", "1.0.0"))
        self.assertIsNone(self.store.get_entry("hello", "9.9.9"))
        self.assertEqual(len(list(self.store.entries())), 1)

    def test_candidates_and_provides(self):
        self.package("hello", "1.0.0")
        self.package("hello", "2.0.0")
        self.package("libmath", "1.2.0", provides=["math"])
        versions = [str(entry.package.version) for entry in self.store.candidates("hello")]
        self.assertEqual(versions, ["2.0.0", "1.0.0"])
        virtual = self.store.candidates("math")
        self.assertEqual([entry.package.name for entry in virtual], ["libmath"])

    def test_ingest_is_idempotent_for_identical_content(self):
        first = self.package()
        second = self.store.ingest_directory(
            self.base / "packages" / "hello-1.0.0"
        )
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual(len(list(self.store.entries())), 1)

    def test_republishing_a_version_with_different_content_is_refused(self):
        self.package()
        changed = write_package(
            self.base / "packages" / "hello-changed",
            "hello",
            "1.0.0",
            files={"share/hello.txt": "different content\n"},
        )
        with self.assertRaises(StoreError) as caught:
            publish(self.store, changed)
        self.assertIn("immutable", str(caught.exception))
        # The original entry is untouched.
        entry = self.store.get_entry("hello", "1.0.0")
        self.assertIn("hello 1.0.0", self.store.read_blob(entry.files[0].digest).decode())

    def test_artifact_digest_must_match_when_given(self):
        self.package()
        artifact = next(self.store.artifacts.glob("*.axp"))
        with self.assertRaises(StoreError):
            self.store.ingest_artifact(artifact, expected_digest="0" * 64)

    def test_artifact_is_cached(self):
        entry = self.package()
        cached = self.store.artifacts / f"{entry.artifact_digest}.axp"
        self.assertTrue(cached.is_file())
        self.assertEqual(cached.stat().st_mode & 0o777, 0o444)

    def test_stats(self):
        self.package()
        stats = self.store.stats()
        self.assertEqual(stats["packages"], 1)
        self.assertEqual(stats["names"], 1)
        self.assertEqual(stats["blobs"], 1)
        self.assertGreater(stats["blob_bytes"], 0)


class VerifyTests(StoreTestCase):
    def test_healthy_store(self):
        self.package()
        self.assertEqual(self.store.verify(deep=True), [])

    def test_detects_corrupt_blob(self):
        entry = self.package()
        blob = self.store.blob_path(entry.files[0].digest)
        os.chmod(blob, 0o644)
        data = blob.read_bytes()
        blob.write_bytes(b"X" + data[1:])
        problems = self.store.verify(deep=True)
        self.assertTrue(any("digest mismatch" in problem for problem in problems))

    def test_detects_metadata_digest_mismatch(self):
        self.package()
        path = self.store.entry_path("hello", "1.0.0")
        import json

        data = json.loads(path.read_text())
        data["content_digest"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(data))
        problems = self.store.verify()
        self.assertTrue(
            any("content digest" in problem for problem in problems)
        )

    def test_detects_missing_blob(self):
        entry = self.package()
        self.store.blob_path(entry.files[0].digest).unlink()
        problems = self.store.verify()
        self.assertTrue(any("missing blob" in problem for problem in problems))


class GarbageCollectionTests(StoreTestCase):
    def test_keeps_referenced_blobs_and_removes_orphans(self):
        entry = self.package()
        referenced = entry.files[0].digest
        orphan = self.store.put_blob(b"orphan")
        report = self.store.gc()
        self.assertIn(orphan, report.removed)
        self.assertEqual(report.kept, 1)
        self.assertFalse(self.store.has_blob(orphan))
        self.assertTrue(self.store.has_blob(referenced))
        self.assertGreater(report.reclaimed, 0)

    def test_dry_run_changes_nothing(self):
        self.package()
        orphan = self.store.put_blob(b"orphan")
        report = self.store.gc(dry_run=True)
        self.assertIn(orphan, report.removed)
        self.assertTrue(self.store.has_blob(orphan))
        self.assertIn("would remove", report.summary())

    def test_extra_keep_digests_are_honoured(self):
        extra = self.store.put_blob(b"kept by snapshot")
        self.store.gc(keep={f"sha256:{extra}"})
        self.assertTrue(self.store.has_blob(extra))

    def test_gc_never_removes_registered_content(self):
        self.package("hello", "1.0.0")
        self.package("hello", "2.0.0")
        self.store.gc()
        for entry in self.store.entries():
            for record in entry.files:
                self.assertTrue(self.store.has_blob(record.digest))


if __name__ == "__main__":
    unittest.main()
