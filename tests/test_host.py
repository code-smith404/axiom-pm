"""Host fact collection and constraint checking."""

from __future__ import annotations

import unittest

from axiom.host import (
    check_constraints,
    collect_facts,
    facts_used_by,
    normalise_arch,
)
from axiom.model import EnvironmentConstraint
from tests import *


class HostFactTests(unittest.TestCase):
    def test_collects_expected_namespaces(self):
        facts = collect_facts()
        for namespace in ("host", "os", "arch", "kernel", "python", "axiom"):
            self.assertIn(namespace, facts)
        self.assertIn("machine", facts["arch"])
        self.assertIn("version", facts["python"])
        self.assertEqual(facts["host"]["arch"], facts["arch"]["machine"])
        self.assertIn("PATH", facts["env"])

    def test_environment_can_be_excluded(self):
        facts = collect_facts(include_env=False)
        self.assertNotIn("env", facts)

    def test_arch_normalisation(self):
        cases = {
            "amd64": "x86_64",
            "x86_64": "x86_64",
            "arm64": "aarch64",
            "aarch64": "aarch64",
            "i686": "x86",
            "riscv64": "riscv64",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalise_arch(raw), expected)

    def test_check_constraints(self):
        facts = {
            "os": {"system": "linux", "version": "6"},
            "host": {"container": "false"},
        }
        problems, returned = check_constraints(
            [
                EnvironmentConstraint("os", "system", "==", "linux"),
                EnvironmentConstraint("os", "version", ">=", "6"),
            ],
            facts,
        )
        self.assertEqual(problems, [])
        self.assertIs(returned, facts)

        problems, _ = check_constraints(
            [
                EnvironmentConstraint("os", "system", "==", "darwin"),
                EnvironmentConstraint("arch", "machine", "==", "x86_64"),
            ],
            facts,
        )
        self.assertEqual(len(problems), 2)
        self.assertIn("darwin", problems[0])
        self.assertIn("not available", problems[1])

    def test_facts_used_by_only_referenced_values(self):
        facts = {
            "os": {"system": "linux", "version": "6"},
            "env": {"SECRET": "do-not-record"},
        }
        used = facts_used_by(
            [EnvironmentConstraint("os", "system", "==", "linux")], facts
        )
        self.assertEqual(used, {"os": {"system": "linux"}})


if __name__ == "__main__":
    unittest.main()
