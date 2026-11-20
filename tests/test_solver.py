"""Deterministic backtracking resolution."""

from __future__ import annotations

import unittest

from axiom.errors import ResolutionError
from axiom.model import Dependency, Package, Requirement
from axiom.solver import Resolver
from axiom.version import parse_constraint, parse_version
from tests import *


def make_package(
    name,
    version,
    *,
    dependencies=(),
    provides=(),
    conflicts=(),
) -> Package:
    return Package(
        name=name,
        version=parse_version(version),
        dependencies=tuple(
            Dependency(dep_name, parse_constraint(constraint), optional=optional)
            for dep_name, constraint, optional in dependencies
        ),
        provides=tuple(provides),
        conflicts=tuple(
            Dependency(dep_name, parse_constraint(constraint))
            for dep_name, constraint in conflicts
        ),
    )


class FakeSource:
    def __init__(self, packages):
        self.packages = list(packages)

    def candidates(self, name):
        return [
            package
            for package in self.packages
            if package.name == name or name in package.provides
        ]


def requirement(name, constraint=""):
    return Requirement(name, parse_constraint(constraint or None))


class ResolutionTests(unittest.TestCase):
    def solve(self, packages, requirements, **kwargs):
        return Resolver(FakeSource(packages), **kwargs).solve(requirements)

    def test_resolves_transitively(self):
        packages = [
            make_package("app", "1.0.0", dependencies=[("lib", "^1.0.0", False)]),
            make_package("lib", "1.4.0", dependencies=[("core", ">=2.0.0", False)]),
            make_package("core", "2.3.0"),
            make_package("core", "1.9.0"),
        ]
        solution = self.solve(packages, [requirement("app")])
        self.assertEqual(
            [package.identity for package in solution.packages],
            ["app@1.0.0", "core@2.3.0", "lib@1.4.0"],
        )

    def test_prefers_highest_compatible_version(self):
        packages = [
            make_package("lib", "1.0.0"),
            make_package("lib", "1.5.0"),
            make_package("lib", "2.0.0"),
        ]
        solution = self.solve(packages, [requirement("lib", "^1.0.0")])
        self.assertEqual(solution.identities(), ("lib@1.5.0",))

    def test_backtracks_to_a_compatible_version(self):
        packages = [
            make_package("app", "2.0.0", dependencies=[("lib", ">=2.0.0", False)]),
            make_package("app", "1.0.0", dependencies=[("lib", "<2.0.0", False)]),
            make_package("lib", "1.0.0"),
        ]
        solution = self.solve(packages, [requirement("app")])
        self.assertEqual(solution.by_name()["app"].version, parse_version("1.0.0"))
        self.assertEqual(solution.by_name()["lib"].version, parse_version("1.0.0"))

    def test_detects_unsatisfiable_constraints(self):
        packages = [
            make_package("a", "1.0.0", dependencies=[("c", ">=2.0.0", False)]),
            make_package("b", "1.0.0", dependencies=[("c", "<2.0.0", False)]),
            make_package("c", "1.0.0"),
            make_package("c", "2.0.0"),
        ]
        with self.assertRaises(ResolutionError) as caught:
            self.solve(packages, [requirement("a"), requirement("b")])
        details = "\n".join(caught.exception.details)
        self.assertIn("c", details)

    def test_missing_package_has_clear_failure(self):
        with self.assertRaises(ResolutionError) as caught:
            self.solve([], [requirement("ghost")])
        self.assertIn("ghost", caught.exception.message)
        self.assertTrue(any("registered" in item for item in caught.exception.details))

    def test_conflicts_are_respected(self):
        packages = [
            make_package("app", "1.0.0", conflicts=[("old", ">=2.0.0")]),
            make_package("old", "2.0.0"),
            make_package("old", "1.0.0"),
        ]
        solution = self.solve(packages, [requirement("app"), requirement("old")])
        self.assertEqual(solution.by_name()["old"].version, parse_version("1.0.0"))

    def test_conflict_with_no_alternative_fails(self):
        packages = [
            make_package("a", "1.0.0", conflicts=[("b", "*")]),
            make_package("b", "1.0.0"),
        ]
        with self.assertRaises(ResolutionError):
            self.solve(packages, [requirement("a"), requirement("b")])

    def test_optional_dependencies_are_inactive_by_default(self):
        packages = [
            make_package("app", "1.0.0", dependencies=[("extra", "^1.0.0", True)]),
            make_package("extra", "1.0.0"),
        ]
        solution = self.solve(packages, [requirement("app")])
        self.assertEqual(solution.identities(), ("app@1.0.0",))

    def test_optional_dependencies_activate_when_requested_elsewhere(self):
        packages = [
            make_package("app", "1.0.0", dependencies=[("extra", "^1.0.0", True)]),
            make_package("extra", "1.0.0"),
        ]
        solution = self.solve(
            packages, [requirement("app"), requirement("extra")]
        )
        self.assertEqual(solution.identities(), ("app@1.0.0", "extra@1.0.0"))

    def test_prereleases_are_excluded_unless_requested(self):
        packages = [
            make_package("lib", "1.0.0"),
            make_package("lib", "1.1.0-rc.1"),
        ]
        solution = self.solve(packages, [requirement("lib", ">=1.0.0")])
        self.assertEqual(solution.identities(), ("lib@1.0.0",))
        solution = self.solve(packages, [requirement("lib", ">=1.1.0-rc.1")])
        self.assertEqual(solution.identities(), ("lib@1.1.0-rc.1",))
        solution = self.solve(
            packages, [requirement("lib", ">=1.0.0")], allow_prerelease=True
        )
        self.assertEqual(solution.identities(), ("lib@1.1.0-rc.1",))

    def test_virtual_packages_resolve_to_providers(self):
        packages = [
            make_package("libmath", "1.2.0", provides=["math"]),
            make_package("libmath", "2.0.0", provides=["math"]),
        ]
        solution = self.solve(packages, [requirement("math")])
        self.assertEqual(solution.identities(), ("libmath@2.0.0",))

    def test_virtual_requirement_and_real_constraint_choose_a_consistent_provider(self):
        packages = [
            make_package("libmath", "1.0.0", provides=["math"]),
            make_package("libmath", "2.0.0", provides=["math"]),
        ]
        solution = self.solve(
            packages, [requirement("math"), requirement("libmath", "1.0.0")]
        )
        self.assertEqual(solution.identities(), ("libmath@1.0.0",))

    def test_dependency_features_are_recorded_for_the_dependency(self):
        packages = [
            Package(
                name="app",
                version=parse_version("1.0.0"),
                dependencies=(
                    Dependency(
                        "lib",
                        parse_constraint("^1.0.0"),
                        features=("tls", "http2"),
                    ),
                ),
            ),
            make_package("lib", "1.0.0"),
        ]
        solution = self.solve(packages, [requirement("app")])
        self.assertEqual(solution.features, {"lib": ("http2", "tls")})

    def test_features_follow_a_virtual_provider(self):
        packages = [
            Package(
                name="app",
                version=parse_version("1.0.0"),
                dependencies=(
                    Dependency(
                        "math",
                        parse_constraint("^1.0.0"),
                        features=("fast",),
                    ),
                ),
            ),
            make_package("libmath", "1.0.0", provides=["math"]),
        ]
        solution = self.solve(packages, [requirement("app")])
        self.assertEqual(solution.features, {"libmath": ("fast",)})

    def test_cycles_terminate(self):
        packages = [
            make_package("a", "1.0.0", dependencies=[("b", "^1", False)]),
            make_package("b", "1.0.0", dependencies=[("a", "^1", False)]),
        ]
        solution = self.solve(packages, [requirement("a")])
        self.assertEqual(solution.identities(), ("a@1.0.0", "b@1.0.0"))

    def test_self_dependency(self):
        packages = [
            make_package("a", "1.0.0", dependencies=[("a", "^1", False)]),
            make_package("a", "2.0.0", dependencies=[("a", ">=3", False)]),
        ]
        solution = self.solve(packages, [requirement("a")])
        self.assertEqual(solution.identities(), ("a@1.0.0",))

    def test_deterministic_results(self):
        packages = [
            make_package("app", "1.0.0", dependencies=[("lib", "^1", False)]),
            make_package("lib", "1.0.0"),
            make_package("lib", "1.1.0"),
            make_package("lib", "1.2.0"),
        ]
        results = {
            Resolver(FakeSource(packages)).solve([requirement("app")]).identities()
            for _ in range(5)
        }
        self.assertEqual(len(results), 1)

    def test_step_budget_fails_loudly(self):
        packages = [
            make_package("a", "1.0.0", dependencies=[("b", "*", False)]),
            make_package("b", "1.0.0", dependencies=[("c", "*", False)]),
            make_package("c", "1.0.0"),
        ]
        with self.assertRaises(ResolutionError) as caught:
            Resolver(FakeSource(packages), max_steps=1).solve([requirement("a")])
        self.assertIn("budget", caught.exception.message)

    def test_empty_requirements(self):
        solution = self.solve([], [])
        self.assertEqual(solution.packages, ())
        self.assertEqual(solution.steps, 0)


if __name__ == "__main__":
    unittest.main()
