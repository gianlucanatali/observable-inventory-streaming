#!/usr/bin/env python3
"""Public-root regression: supported static/unit suites need no umbrella checkout."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

OVERLAY = Path(__file__).resolve().parents[1]


class StandaloneOverlayTest(unittest.TestCase):
    def test_local_config_rendering_does_not_require_the_lima_vm(self) -> None:
        result = subprocess.run(
            ["make", "-n", "MODE=dev", "config"],
            cwd=OVERLAY,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("docker compose", result.stdout)
        self.assertNotIn("limactl shell", result.stdout)

    def test_private_tool_assets_are_absent_from_the_public_overlay(self) -> None:
        public_paths = (
            OVERLAY / "scripts" / "backup_recorder.py",
            OVERLAY / "scripts" / "build_reel.sh",
            OVERLAY / "demo-evidence" / "reel.json",
            OVERLAY / "smoke" / "tests" / "test_backup_recorder.py",
            OVERLAY / "smoke" / "tests" / "test_reel.py",
            OVERLAY / "workshop" / "IMAGES.md",
            OVERLAY / "inventory-api" / "bench.py",
        )
        self.assertTrue(all(not path.exists() for path in public_paths), public_paths)

    def test_copied_overlay_with_in_root_jr_fixture_runs_static_and_unit_suites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "public-demo"
            shutil.copytree(
                OVERLAY,
                root,
                ignore=shutil.ignore_patterns(
                    ".state", ".env*", "demo.yaml", "vendor", ".venv", ".terraform", "node_modules", "__pycache__"
                ),
            )
            jr_context = root / "vendor" / "jr"
            jr_context.mkdir(parents=True)
            jr_dockerfile = root / "jr" / "Dockerfile"
            jr_dockerfile.parent.mkdir(exist_ok=True)
            jr_dockerfile.write_text("FROM scratch\n")
            self.assertFalse((jr_context / "Dockerfile").exists())
            self.assertEqual((jr_context / "../../jr/Dockerfile").resolve(), jr_dockerfile.resolve())
            commands = [
                [sys.executable, "-m", "unittest", "tests/test_demo_cli.py"],
            ]
            commands.extend([["bash", str(path.relative_to(root))] for path in sorted((root / "compose/scripts/tests").glob("test-*.sh"))])
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pytest_check = subprocess.run(
                [sys.executable, "-c", "import pytest"], text=True, capture_output=True, check=False, timeout=10
            )
            self.assertEqual(
                pytest_check.returncode,
                0,
                "standalone acceptance requires pytest; install the locked smoke test dependency (cd smoke && uv sync --group dev)",
            )
            for static_guard in ("terraform/aws/tests/test_static_guards.py", "terraform/datadog/tests/test_static_guards.py"):
                with self.subTest(static_guard=static_guard):
                    result = subprocess.run(
                        [sys.executable, static_guard], cwd=root, text=True, capture_output=True, check=False, timeout=60
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for suite in ("compose/tests", "flink/tests"):
                with self.subTest(suite=suite):
                    result = subprocess.run(
                        [sys.executable, "-m", "pytest", "-q", suite],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            uv_check = subprocess.run(["uv", "--version"], text=True, capture_output=True, check=False, timeout=10)
            self.assertEqual(
                uv_check.returncode,
                0,
                "standalone acceptance requires uv to run each component's locked unit suite",
            )
            component_suites = (
                "inventory-api",
                "storefront/backend",
                "stock-projector",
                "offer-worker",
                "sellable-dev",
                "cost-meter",
                "scenario",
                "watchdog",
                "supplier-sim",
                "demo-control",
            )
            for component in component_suites:
                with self.subTest(component=component):
                    result = subprocess.run(
                        ["uv", "run", "pytest", "-q"],
                        cwd=root / component,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=180,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with self.subTest(component="smoke-unit"):
                result = subprocess.run(
                    ["uv", "run", "pytest", "-q", "tests/test_checks.py"],
                    cwd=root / "smoke",
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=180,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
