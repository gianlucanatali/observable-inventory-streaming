#!/usr/bin/env python3
"""Offline contract tests for the public demo launcher."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

OVERLAY = Path(__file__).resolve().parents[1]
DEMO = OVERLAY / "demo"

VALID = """stack: hybrid
aws_profile: demo-profile
aws_region: eu-west-1
presenter_cidr: 203.0.113.4/32
layers: releases,restock
datadog_site: datadoghq.eu
dd_api_key: dd_api_12345678
dd_app_key: dd_app_12345678
confluent_cloud_api_key: CK12345678
confluent_cloud_api_secret: CS12345678
jev_api_key: ""
"""


def copy_overlay(destination: Path) -> Path:
    root = destination / "public-demo"
    shutil.copytree(
        OVERLAY,
        root,
        ignore=shutil.ignore_patterns(
            ".state", ".env*", "demo.yaml", "vendor", ".venv", ".terraform", "node_modules", "__pycache__"
        ),
    )
    return root


def run_demo(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(root / "demo"), *args], cwd=root, text=True, capture_output=True, check=False, timeout=10
    )


class DemoCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = copy_overlay(Path(self.tmp.name))
        (self.root / "demo.yaml").write_text(VALID)
        os.chmod(self.root / "demo.yaml", 0o600)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_validates_and_maps_every_command_without_writing_files(self) -> None:
        expected = {
            "create": ("stack-preflight", "stack-up", "CONFIRM=yes"),
            "destroy": ("stack-down", "CONFIRM=yes"),
            "status": ("stack-status",),
            "links": ("links",),
            "reset": ("reset",),
        }
        for command, snippets in expected.items():
            with self.subTest(command=command):
                result = run_demo(self.root, command, "--dry-run", "--yes")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("MODE=cloud", result.stdout)
                self.assertIn("ENV_DIR=", result.stdout)
                for snippet in snippets:
                    self.assertIn(snippet, result.stdout)
                self.assertNotIn("dd_api_12345678", result.stdout + result.stderr)
                self.assertNotIn("CS12345678", result.stdout + result.stderr)
                self.assertFalse((self.root / ".env").exists())

    def test_refuses_unknown_placeholder_invalid_value_and_insecure_config(self) -> None:
        cases = {
            "unknown": VALID + "unexpected: value\n",
            "placeholder": VALID.replace("dd_api_12345678", "REPLACE_ME"),
            "region": VALID.replace("eu-west-1", "moon-1"),
            "layers": VALID.replace("releases,restock", "kafka"),
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                (self.root / "demo.yaml").write_text(content)
                result = run_demo(self.root, "status", "--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("dd_api_12345678", result.stdout + result.stderr)
        (self.root / "demo.yaml").write_text(VALID)
        os.chmod(self.root / "demo.yaml", 0o644)
        result = run_demo(self.root, "status", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("chmod 600 demo.yaml", result.stderr)

    def test_create_writes_private_env_atomically_and_uses_existing_secrets_target(self) -> None:
        mock = self.root / "make"
        mock.write_text("#!/usr/bin/env python3\nimport sys\nprint('MAKE ' + ' '.join(sys.argv[1:]))\n")
        mock.chmod(0o755)
        result = subprocess.run(
            [str(self.root / "demo"), "create", "--yes"], cwd=self.root, text=True, capture_output=True,
            env={**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}"}, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        env_file = self.root / ".env"
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertIn("DD_API_KEY=dd_api_12345678", env_file.read_text())
        self.assertIn("secrets", result.stdout)
        self.assertNotIn("dd_api_12345678", result.stdout + result.stderr)

    def test_empty_presenter_cidr_is_optional_and_not_forwarded_to_make(self) -> None:
        for value in ("", "\"\"", "''"):
            with self.subTest(value=value):
                (self.root / "demo.yaml").write_text(VALID.replace("203.0.113.4/32", value))
                result = run_demo(self.root, "status", "--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("TF_VAR_presenter_cidr=", result.stdout)

    def test_create_prepares_then_preflights_before_cost_confirmation_and_stack_up(self) -> None:
        loader = __import__("importlib.machinery").machinery.SourceFileLoader("demo_under_test", str(self.root / "demo"))
        spec = __import__("importlib.util").util.spec_from_loader(loader.name, loader)
        module = __import__("importlib.util").util.module_from_spec(spec)
        loader.exec_module(module)
        events: list[tuple[str, object]] = []
        with (
            mock.patch.object(module, "write_env", side_effect=lambda values: events.append(("env", values))),
            mock.patch.object(module, "invoke", side_effect=lambda command, dry_run: events.append(("make", command[-1]))),
            mock.patch.object(module, "prompt", side_effect=lambda message, yes: events.append(("prompt", message))),
            mock.patch.object(sys, "argv", ["demo", "create"]),
        ):
            self.assertEqual(module.main(), 0)
        self.assertEqual([event[0] for event in events], ["env", "make", "make", "prompt", "make"])
        self.assertEqual([event[1] for event in events if event[0] == "make"], ["secrets", "stack-preflight", "CONFIRM=yes"])
        self.assertIn("~US$1.70/hour", str(events[3][1]))
        self.assertIn("remaining trial credit", str(events[3][1]).lower())

    def test_every_make_action_declares_hybrid_topology(self) -> None:
        for action in ("create", "destroy", "status", "links", "reset"):
            with self.subTest(action=action):
                result = run_demo(self.root, action, "--dry-run", "--yes")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("TOPOLOGY=hybrid", result.stdout)

    def test_public_root_files_and_submodule_contract_are_present(self) -> None:
        ignored = (self.root / ".gitignore").read_text()
        for entry in (".env*", "demo.yaml", ".state/", "account-down_override.tf.json", ".terraform/", "*.tfstate*", "*.tfplan", "node_modules/", "logs/"):
            self.assertIn(entry, ignored)
        modules = (self.root / ".gitmodules").read_text()
        self.assertIn("vendor/jr", modules)
        self.assertIn("https://github.com/gianlucanatali/jr.git", modules)
        self.assertIn("branch = sql-producer", modules)

    def test_jr_dockerfile_is_resolved_from_the_jr_build_context(self) -> None:
        compose = (self.root / "compose" / "compose.yaml").read_text()
        makefile = (self.root / "Makefile").read_text()
        self.assertIn("dockerfile: ${JR_DOCKERFILE:-../../jr/Dockerfile}", compose)
        self.assertIn("JR_DOCKERFILE ?= $(if $(wildcard $(CURDIR)/vendor/jr),../../jr/Dockerfile,../../overlay/jr/Dockerfile)", makefile)


if __name__ == "__main__":
    unittest.main()
