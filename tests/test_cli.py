"""End-to-end CLI behaviour."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from axiom.store import Store
from tests import *
from tests.helpers import run_cli, write_environment, write_package


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.store = Store(self.root).initialize()
        self.work = self.base / "work"
        self.work.mkdir()

    # ------------------------------------------------------------- fixtures

    def build_and_publish(self, name, version, **kwargs) -> Path:
        directory = write_package(
            self.work / f"{name}-{version}", name, version, **kwargs
        )
        artifact = self.base / f"{name}-{version}.axp"
        code, out, err = run_cli(["build", directory, "-o", artifact], self.root)
        self.assertEqual(code, 0, err)
        code, out, err = run_cli(["publish", artifact], self.root)
        self.assertEqual(code, 0, err)
        return artifact

    def environment(self, name="dev", packages=None, variables=None, **kwargs) -> Path:
        return write_environment(
            self.work / f"{name}.toml",
            name,
            packages or {"hello": "^1.0.0"},
            variables=variables,
            **kwargs,
        )

    def json_cli(self, *argv, expect=0):
        code, out, err = run_cli(argv, self.root)
        self.assertEqual(code, expect, f"stdout={out}\nstderr={err}")
        return json.loads(out)


class BuildAndInspectTests(CliTestCase):
    def test_build_publish_search_info(self):
        self.build_and_publish(
            "hello",
            "1.0.0",
            description="greeting tool",
            files={"bin/hello": ("#!/bin/sh\necho hi\n", "0755")},
        )
        self.build_and_publish("hello", "1.2.0", description="greeting tool")
        self.build_and_publish(
            "libmath", "2.0.0", provides=["math"], description="math library"
        )

        search = self.json_cli("search", "hello", "--json")
        self.assertEqual(search[0]["name"], "hello")
        self.assertEqual(search[0]["versions"], ["1.2.0", "1.0.0"])

        info = self.json_cli("info", "hello@^1.0.0", "--json")
        self.assertEqual(info["version"], "1.2.0")
        self.assertEqual(info["description"], "greeting tool")
        self.assertTrue(info["content_digest"].startswith("sha256:"))

        packages = self.json_cli("list", "--store", "--json")
        self.assertEqual(len(packages), 3)
        self.assertIn("libmath", [item["name"] for item in packages])

        code, out, err = run_cli(["search", "nothing-matches"], self.root)
        self.assertEqual(code, 0)
        self.assertIn("no matching packages", out)

        code, out, err = run_cli(["info", "ghost"], self.root)
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_build_without_publish_does_not_create_store(self):
        directory = write_package(self.work / "pkg", "hello", "1.0.0")
        root = self.base / "untouched"
        code, out, err = run_cli(["build", directory, "-o", self.base / "x.axp"], root)
        self.assertEqual(code, 0, err)
        self.assertFalse(root.exists())


class InstallTests(CliTestCase):
    def test_install_flow(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment(variables={"GREETING": "hi"})

        plan = self.json_cli("resolve", environment, "--json")
        self.assertEqual(plan["environment"], "dev")
        self.assertEqual(len(plan["packages"]), 1)
        self.assertEqual(plan["packages"][0]["name"], "hello")

        result = self.json_cli("install", environment, "--lock", "--json")
        self.assertEqual(result["profile"], "dev")
        self.assertTrue(result["changed"])
        self.assertEqual(result["generation"], "000001-" + result["snapshot"].split(":")[1][:12])
        self.assertTrue((self.work / "axiom.lock.json").is_file())

        status = self.json_cli("-p", "dev", "status", "--json")
        self.assertEqual(status["generation"], result["generation"])
        self.assertEqual(status["packages"], 1)
        self.assertEqual(status["generations"], 1)

        listing = self.json_cli("-p", "dev", "list", "--json")
        self.assertEqual(listing["packages"][0]["name"], "hello")

        code, out, err = run_cli(["-p", "dev", "verify"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("intact", out)

        exported = self.json_cli("-p", "dev", "export", "--json")
        self.assertEqual(exported["snapshot"]["environment"], "dev")

    def test_install_is_idempotent(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment()
        first = self.json_cli("install", environment, "--json")
        second = self.json_cli("install", environment, "--json")
        self.assertFalse(second["changed"])
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(second["packages"], first["packages"])

    def test_dry_run_does_not_activate(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment()
        code, out, err = run_cli(["install", environment, "--dry-run"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("would install", out)
        status = self.json_cli("-p", "dev", "status", "--json")
        self.assertFalse(status["active"])
        self.assertFalse((self.work / "axiom.lock.json").exists())

    def test_locked_install_rejects_a_stale_lockfile(self):
        self.build_and_publish("hello", "1.0.0")
        self.build_and_publish("hello", "2.0.0")
        environment = self.environment(packages={"hello": "=2.0.0"})
        self.json_cli("install", environment, "--lock", "--json")

        write_environment(environment, "dev", {"hello": "=1.0.0"})
        code, out, err = run_cli(
            ["install", environment, "--locked"], self.root
        )
        self.assertEqual(code, 1)
        self.assertIn("out of date", err)

        # Updating the lockfile catches up and a locked install succeeds.
        self.json_cli("lock", environment, "--json")
        result = self.json_cli("install", environment, "--locked", "--json")
        self.assertEqual(result["packages"][0]["version"], "1.0.0")

    def test_locked_install_without_lockfile_fails(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment()
        code, out, err = run_cli(
            ["install", environment, "--locked"], self.root
        )
        self.assertEqual(code, 1)
        self.assertIn("no lockfile", err)

    def test_unsatisfiable_environment_exit_code(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment(packages={"hello": ">=2.0.0"})
        code, out, err = run_cli(["resolve", environment], self.root)
        self.assertEqual(code, 3)
        self.assertIn("cannot resolve", err)
        self.assertIn("hello", err)

    def test_host_constraint_failure(self):
        self.build_and_publish(
            "hello",
            "1.0.0",
            environment_constraints=[
                {
                    "kind": "os",
                    "name": "id",
                    "operator": "==",
                    "value": "definitely-not-this-os",
                }
            ],
        )
        environment = self.environment()
        code, out, err = run_cli(["install", environment], self.root)
        self.assertEqual(code, 1)
        self.assertIn("does not match this host", err)
        code, out, err = run_cli(
            ["install", environment, "--ignore-host"], self.root
        )
        self.assertEqual(code, 0, err)

    def test_axiom_profile_environment_variable_is_honoured(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment()
        code, out, err = run_cli(
            ["install", environment, "--json"],
            self.root,
            env={"AXIOM_PROFILE": "from-env"},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["profile"], "from-env")
        status = self.json_cli("-p", "from-env", "status", "--json")
        self.assertTrue(status["active"])

    def test_install_is_isolated_per_profile(self):
        self.build_and_publish("hello", "1.0.0")
        self.build_and_publish("hello", "2.0.0")
        env_one = self.environment(
            "one", {"hello": "=1.0.0"}
        )
        env_two = self.environment(
            "two", {"hello": "=2.0.0"}
        )
        one = self.json_cli("install", env_one, "-p", "one", "--json")
        two = self.json_cli("install", env_two, "-p", "two", "--json")
        self.assertNotEqual(one["generation"], two["generation"])
        profiles = self.json_cli("profiles", "--json")
        self.assertEqual(
            {item["profile"]: item["snapshot"] for item in profiles},
            {"one": one["snapshot"], "two": two["snapshot"]},
        )
        path = self.root / "profiles" / "one" / "current" / "files" / "share" / "hello.txt"
        self.assertIn("hello 1.0.0", path.read_text())


class DiffAndRollbackTests(CliTestCase):
    def test_rollback_through_cli(self):
        self.build_and_publish("hello", "1.0.0")
        self.build_and_publish("hello", "2.0.0")
        env_one = self.environment("first", {"hello": "=1.0.0"})
        env_two = self.environment("second", {"hello": "=2.0.0"})
        first = self.json_cli("install", env_one, "-p", "demo", "--json")
        second = self.json_cli("install", env_two, "-p", "demo", "--json")
        self.assertNotEqual(first["snapshot"], second["snapshot"])

        dry = self.json_cli("-p", "demo", "rollback", "--dry-run", "--json")
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["target"], first["generation"])

        rolled = self.json_cli("-p", "demo", "rollback", "--json")
        self.assertEqual(rolled["snapshot"], first["snapshot"])
        history = self.json_cli("-p", "demo", "history", "--json")
        self.assertEqual(
            [item["action"] for item in history], ["install", "install", "rollback"]
        )

    def test_diff_environment_against_active_profile(self):
        self.build_and_publish("hello", "1.0.0")
        self.build_and_publish("hello", "2.0.0")
        env_one = write_environment(
            self.work / "one.toml", "dev", {"hello": "=1.0.0"}
        )
        env_two = write_environment(
            self.work / "two.toml", "dev", {"hello": "=2.0.0"}
        )
        self.json_cli("install", env_one, "-p", "dev", "--json")
        diff = self.json_cli("-p", "dev", "diff", env_two, "--json")
        self.assertEqual(diff["changed_packages"], ["hello"])
        self.assertEqual(diff["added_packages"], [])
        self.assertEqual(diff["removed_packages"], [])
        self.assertFalse(diff["identical"])

        identical = self.json_cli("-p", "dev", "diff", env_one, "--json")
        self.assertTrue(identical["identical"])

    def test_diff_between_generations(self):
        self.build_and_publish("hello", "1.0.0")
        self.build_and_publish("hello", "2.0.0")
        self.json_cli(
            "install", self.environment("a", {"hello": "=1.0.0"}), "-p", "dev", "--json"
        )
        self.json_cli(
            "install", self.environment("b", {"hello": "=2.0.0"}), "-p", "dev", "--json"
        )
        diff = self.json_cli("-p", "dev", "diff", "1", "2", "--json")
        self.assertFalse(diff["identical"])


class MaintenanceTests(CliTestCase):
    def test_env_scaffold_validate_show(self):
        path = self.work / "new.toml"
        code, out, err = run_cli(["env", "init", path], self.root)
        self.assertEqual(code, 0, err)
        self.assertTrue(path.is_file())
        code, out, err = run_cli(["env", "validate", path, "--json"], self.root)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertTrue(data["valid"])

        code, out, err = run_cli(["env", "init", path], self.root)
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)
        code, out, err = run_cli(["env", "init", path, "--force"], self.root)
        self.assertEqual(code, 0, err)

        show = self.json_cli("env", "show", path, "--json")
        self.assertEqual(show["name"], "new")

    def test_gc_and_prune(self):
        self.build_and_publish("hello", "1.0.0")
        self.build_and_publish("hello", "2.0.0")
        for version in ("1.0.0", "2.0.0"):
            self.json_cli(
                "install",
                self.environment("dev", {"hello": f"={version}"}),
                "-p", "dev",
                "--json",
            )
        report = self.json_cli(
            "gc",
            "--dry-run",
            "--prune-generations", "2",
            "--artifacts",
            "--json",
        )
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["blobs"]["removed"], 0)
        status = self.json_cli("-p", "dev", "status", "--json")
        self.assertEqual(status["generations"], 2)

        code, out, err = run_cli(["gc", "--prune-generations", "2"], self.root)
        self.assertEqual(code, 0, err)
        status = self.json_cli("-p", "dev", "status", "--json")
        self.assertEqual(status["generations"], 2)

    def test_verify_store(self):
        self.build_and_publish("hello", "1.0.0")
        result = self.json_cli("verify", "--store", "--json")
        self.assertTrue(result["ok"])
        self.assertEqual(result["problems"], [])

    def test_run_sets_profile_environment(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment(variables={"AXIOM_TEST_VALUE": "42"})
        self.json_cli("install", environment, "-p", "dev", "--json")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "axiom",
                "--root",
                str(self.root),
                "-p",
                "dev",
                "run",
                "--",
                sys.executable,
                "-c",
                (
                    "import os; print(os.environ['AXIOM_TEST_VALUE']); "
                    "print(os.environ['AXIOM_PROFILE'])"
                ),
            ],
            capture_output=True,
            check=False,
            text=True,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), ["42", "dev"])

    def test_shell_prints_exports(self):
        self.build_and_publish("hello", "1.0.0")
        environment = self.environment(variables={"FOO": "bar"})
        self.json_cli("install", environment, "-p", "dev", "--json")
        code, out, err = run_cli(["-p", "dev", "shell"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("export FOO='bar'", out)
        self.assertIn("export AXIOM_PROFILE='dev'", out)

    def test_json_error_output(self):
        environment = self.environment(packages={"missing": "*"})
        code, out, err = run_cli(["resolve", environment, "--json"], self.root)
        self.assertEqual(code, 3)
        data = json.loads(out)
        self.assertEqual(data["kind"], "ResolutionError")
        self.assertIn("missing", data["error"])


if __name__ == "__main__":
    unittest.main()
