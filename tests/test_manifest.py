"""TOML loading: includes, table forms and error reporting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from axiom.errors import EnvironmentError
from axiom.manifest import (
    load_environment,
    load_package,
    package_from_dict,
)
from axiom.version import parse_constraint
from tests import *


class EnvironmentLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def write(self, name, text):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return path

    def test_includes_merge_packages_and_variables(self):
        self.write(
            "base.toml",
            "\n".join(
                [
                    'name = "base"',
                    "[packages]",
                    'lib = "^1.0.0"',
                    "[variables]",
                    'A = "base"',
                    'B = "base"',
                ]
            )
            + "\n",
        )
        dev = self.write(
            "dev.toml",
            "\n".join(
                [
                    'name = "dev"',
                    'includes = ["base.toml"]',
                    "[packages]",
                    'app = "^2.0.0"',
                    "[variables]",
                    'B = "dev"',
                ]
            )
            + "\n",
        )
        environment = load_environment(dev)
        self.assertEqual(environment.name, "dev")
        names = [requirement.name for requirement in environment.requirements()]
        self.assertEqual(names, ["app", "lib"])
        self.assertEqual(environment.variables, {"A": "base", "B": "dev"})
        self.assertEqual(
            {requirement.name: str(requirement.constraint) for requirement in environment.requirements()},
            {"app": ">=2.0.0,<3.0.0", "lib": ">=1.0.0,<2.0.0"},
        )

    def test_duplicate_constraints_are_merged_by_requirements(self):
        path = self.write(
            "env.toml",
            "\n".join(
                [
                    'name = "dev"',
                    'includes = ["left.toml", "right.toml"]',
                ]
            )
            + "\n",
        )
        for side, constraint in (("left", ">=1"), ("right", "<2")):
            self.write(
                f"{side}.toml",
                "\n".join(
                    [
                        f'name = "{side}"',
                        "[packages]",
                        f'lib = "{constraint}"',
                    ]
                )
                + "\n",
            )
        environment = load_environment(path)
        requirements = environment.requirements()
        self.assertEqual(len(requirements), 1)
        merged = requirements[0].constraint
        self.assertTrue(merged.matches(parse_constraint("1.5.0").exact))
        self.assertFalse(merged.matches(parse_constraint("2.0.0").exact))

    def test_include_cycle_is_rejected(self):
        self.write("a.toml", 'name = "a"\nincludes = ["b.toml"]\n')
        self.write("b.toml", 'name = "b"\nincludes = ["a.toml"]\n')
        with self.assertRaises(EnvironmentError) as caught:
            load_environment(self.base / "a.toml")
        self.assertIn("cycle", str(caught.exception))

    def test_missing_include_is_rejected(self):
        path = self.write("env.toml", 'name = "x"\nincludes = ["missing.toml"]\n')
        with self.assertRaises(EnvironmentError) as caught:
            load_environment(path)
        self.assertIn("not found", str(caught.exception))

    def test_missing_file_and_invalid_toml(self):
        with self.assertRaises(EnvironmentError):
            load_environment(self.base / "nope.toml")
        bad = self.write("bad.toml", "name = [unterminated\n")
        with self.assertRaises(EnvironmentError) as caught:
            load_environment(bad)
        self.assertIn("invalid TOML", str(caught.exception))

    def test_invalid_package_entry(self):
        path = self.write("bad.toml", 'name = "x"\npackages = 42\n')
        with self.assertRaises(EnvironmentError):
            load_environment(path)

    def test_environment_name_falls_back_to_file_stem(self):
        path = self.write("stem.toml", "[packages]\nlib = '*'\n")
        self.assertEqual(load_environment(path).name, "stem")


class PackageLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_table_forms(self):
        package = package_from_dict(
            {
                "name": "tool",
                "version": "1.0.0",
                "dependencies": {"lib": "^1.0.0"},
                "conflicts": {"old": "<2.0.0"},
                "provides": "virtual",
                "files": [{"source": "bin/tool", "mode": "0755"}],
            }
        )
        self.assertEqual(package.dependencies[0].name, "lib")
        self.assertEqual(package.conflicts[0].constraint, parse_constraint("<2.0.0"))
        self.assertEqual(package.provides, ("virtual",))
        self.assertEqual(package.files[0].target, "bin/tool")
        self.assertEqual(package.files[0].mode, "0755")

    def test_invalid_package_definition(self):
        with self.assertRaises(EnvironmentError):
            package_from_dict({"name": "x"})
        with self.assertRaises(EnvironmentError):
            package_from_dict({"version": "1.0.0"})
        with self.assertRaises(EnvironmentError):
            package_from_dict({"name": "x", "version": "1.0.0", "scripts": []})

    def test_load_package_file(self):
        path = self.base / "package.toml"
        path.write_text(
            "\n".join(
                [
                    'name = "hello"',
                    'version = "1.0.0"',
                    "[[files]]",
                    'source = "bin/hello"',
                    'target = "bin/hello"',
                ]
            )
            + "\n",
            "utf-8",
        )
        package = load_package(path)
        self.assertEqual(package.identity, "hello@1.0.0")
        self.assertEqual(package.files[0].mode, "0644")

    def test_missing_package_file(self):
        with self.assertRaises(EnvironmentError):
            load_package(self.base / "package.toml")


if __name__ == "__main__":
    unittest.main()
