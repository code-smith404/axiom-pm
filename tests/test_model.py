"""Core data model behaviour."""

from __future__ import annotations

import unittest

from axiom.model import (
    Dependency,
    Environment,
    EnvironmentConstraint,
    FileSpec,
    Package,
    PackageFile,
    Requirement,
    canonical_json,
    content_digest,
    normalize_mode,
)
from axiom.version import parse_constraint, parse_version
from tests import *


class PackageTests(unittest.TestCase):
    def make_package(self) -> Package:
        return Package.from_dict(
            {
                "name": "tool",
                "version": "1.2.3",
                "description": "a tool",
                "license": "MIT",
                "homepage": "https://example.invalid",
                "dependencies": [
                    {"name": "lib", "constraint": "^2.0.0"},
                    {"name": "opt", "constraint": ">=1", "optional": True},
                ],
                "provides": ["virtual-tool"],
                "conflicts": ["oldtool <1.0.0"],
                "files": [
                    {"source": "bin/tool", "target": "bin/tool", "mode": "0755"},
                    {"source": "share/doc", "target": "share/tool/doc"},
                ],
                "environment_constraints": [
                    {
                        "kind": "os",
                        "name": "id",
                        "operator": "==",
                        "value": "linux",
                    }
                ],
                "scripts": {"post": "echo hi"},
            }
        )

    def test_from_dict_and_roundtrip(self):
        package = self.make_package()
        self.assertEqual(package.identity, "tool@1.2.3")
        self.assertEqual(package.dependency("lib").constraint, parse_constraint("^2.0.0"))
        self.assertTrue(package.dependency("opt").optional)
        self.assertEqual(package.files[0].mode, "0755")
        self.assertEqual(package.files[1].mode, "0644")
        self.assertEqual(Package.from_dict(package.to_dict()), package)
        self.assertEqual(package.to_dict()["scripts"], {"post": "echo hi"})

    def test_required_fields(self):
        with self.assertRaises(ValueError):
            Package.from_dict({"version": "1.0.0"})
        with self.assertRaises(ValueError):
            Package.from_dict({"name": "x"})
        for bad_name in ("bad name", "..", ".", "a/b", "a\\b", "a\x00b"):
            with self.subTest(name=bad_name), self.assertRaises(ValueError):
                Package(name=bad_name, version=parse_version("1.0.0"))
        with self.assertRaises(ValueError):
            Package(name="x", version="not-a-version")

    def test_canonical_json_is_order_independent(self):
        left = {"b": 1, "a": [1, 2], "c": {"z": True, "y": None}}
        right = {"c": {"y": None, "z": True}, "a": [1, 2], "b": 1}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(content_digest(left), content_digest(right))
        self.assertTrue(content_digest(left).startswith("sha256:"))

    def test_version_normalised(self):
        package = Package(name="x", version="1")
        self.assertEqual(str(package.version), "1.0.0")


class FileAndModeTests(unittest.TestCase):
    def test_normalize_mode(self):
        self.assertEqual(normalize_mode("755"), "0755")
        self.assertEqual(normalize_mode("0755"), "0755")
        self.assertEqual(normalize_mode("0o644"), "0644")
        self.assertEqual(normalize_mode(0o755), "0755")
        self.assertEqual(FileSpec("a", "b", 0o600).mode, "0600")

    def test_invalid_mode(self):
        for value in ("", "9", "0999", -1, 0o10000, "4755", "rwx"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_mode(value)

    def test_package_file_records(self):
        record = PackageFile(target="bin/x", digest="a" * 64, mode="755", size="3")
        self.assertEqual(record.mode, "0755")
        self.assertEqual(record.size, 3)
        self.assertEqual(PackageFile.from_dict(record.to_dict()), record)


class EnvironmentTests(unittest.TestCase):
    def test_requirements_merge_constraints(self):
        environment = Environment(
            name="dev",
            packages=(
                Requirement("lib", parse_constraint("^1.0.0"), reason="direct"),
                Requirement("lib", parse_constraint("<1.5.0"), optional=True),
                Requirement("other", parse_constraint("2.0.0")),
            ),
        )
        merged = {requirement.name: requirement for requirement in environment.requirements()}
        self.assertEqual(list(merged), ["lib", "other"])
        self.assertTrue(merged["lib"].constraint.matches(parse_version("1.4.9")))
        self.assertFalse(merged["lib"].constraint.matches(parse_version("1.5.0")))
        self.assertFalse(merged["lib"].optional)
        self.assertIn("direct", merged["lib"].reason)

    def test_from_dict_accepts_strings_and_tables(self):
        environment = Environment.from_dict(
            {
                "name": "dev",
                "packages": ["hello ^1.0.0", {"name": "lib", "constraint": ">=2"}],
                "variables": {"A": 1},
                "environment_constraints": [
                    {"kind": "arch", "name": "machine", "value": "x86_64"}
                ],
            }
        )
        self.assertEqual(environment.packages[0].name, "hello")
        self.assertEqual(environment.packages[1].constraint, parse_constraint(">=2"))
        self.assertEqual(environment.variables, {"A": "1"})
        self.assertEqual(environment.environment_constraints[0].kind, "arch")

    def test_invalid_environment(self):
        with self.assertRaises(ValueError):
            Environment.from_dict({"packages": []})
        with self.assertRaises(ValueError):
            Environment.from_dict({"name": "x", "packages": [42]})

    def test_constraint_evaluation(self):
        facts = {
            "os": {"id": "linux", "version": "6"},
            "env": {"MODE": "dev"},
            "host": {"container": "false"},
        }
        cases = [
            (EnvironmentConstraint("os", "id", "==", "linux"), True),
            (EnvironmentConstraint("os", "id", "!=", "darwin"), True),
            (EnvironmentConstraint("os", "version", ">", "5"), True),
            (EnvironmentConstraint("os", "version", ">=", "6"), True),
            (EnvironmentConstraint("env", "MODE", "in", "dev,staging"), True),
            (EnvironmentConstraint("env", "MODE", "not-in", "prod"), True),
            (EnvironmentConstraint("env", "MODE", "contains", "dev"), True),
            (EnvironmentConstraint("host", "container", "==", "true"), False),
            (EnvironmentConstraint("os", "missing", "==", "x"), False),
        ]
        for constraint, expected in cases:
            with self.subTest(constraint=str(constraint)):
                satisfied, detail = constraint.evaluate(facts)
                self.assertIs(satisfied, expected, detail)
                self.assertTrue(detail)

    def test_numeric_comparison_requires_numbers(self):
        satisfied, detail = EnvironmentConstraint(
            "os", "id", ">", "5"
        ).evaluate({"os": {"id": "linux"}})
        self.assertFalse(satisfied)
        self.assertIn("cannot compare", detail)

    def test_unknown_operator(self):
        satisfied, detail = EnvironmentConstraint(
            "os", "id", "~=", "linux"
        ).evaluate({"os": {"id": "linux"}})
        self.assertFalse(satisfied)
        self.assertIn("unknown comparison", detail)

    def test_dependency_string(self):
        dependency = Dependency("lib", parse_constraint("^1.0.0"), optional=True)
        self.assertEqual(str(dependency), "lib >=1.0.0,<2.0.0 (optional)")


if __name__ == "__main__":
    unittest.main()
