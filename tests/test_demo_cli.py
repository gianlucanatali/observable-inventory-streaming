#!/usr/bin/env python3
"""Offline contract tests for the public demo launcher."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

OVERLAY = Path(__file__).resolve().parents[1]
DEMO = OVERLAY / "demo"

VALID = """stack: hybrid
aws_profile: demo-profile
aws_region: eu-west-1
allowed_cidr: 203.0.113.4/32
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

    def test_empty_allowed_cidr_is_optional_and_not_forwarded_to_make(self) -> None:
        for value in ("", "\"\"", "''"):
            with self.subTest(value=value):
                (self.root / "demo.yaml").write_text(VALID.replace("203.0.113.4/32", value))
                result = run_demo(self.root, "status", "--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("TF_VAR_presenter_cidr=", result.stdout)

    def test_owner_is_optional_forwarded_to_make_and_an_environment_value_wins(self) -> None:
        result = run_demo(self.root, "status", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("OWNER=", result.stdout)
        (self.root / "demo.yaml").write_text(VALID + "owner: jane.doe@example.com\n")
        result = run_demo(self.root, "status", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OWNER=jane.doe@example.com", result.stdout)
        with mock.patch.dict(os.environ, {"OWNER": "envowner"}):
            result = run_demo(self.root, "status", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("OWNER=jane.doe@example.com", result.stdout)

    def test_owner_must_fit_the_aws_tag_value_rules(self) -> None:
        for value in ("jane doe", "jane,doe", "a" * 257, '""'):
            with self.subTest(value=value[:12]):
                (self.root / "demo.yaml").write_text(VALID + f"owner: {value}\n")
                result = run_demo(self.root, "status", "--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("owner must be 1 to 256 characters", result.stderr)

    def test_allowed_cidr_is_forwarded_as_presenter_cidr_and_legacy_key_is_accepted(self) -> None:
        result = run_demo(self.root, "status", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TF_VAR_presenter_cidr=203.0.113.4/32", result.stdout)
        self.assertIn("PRESENTER_CIDR=203.0.113.4/32", result.stdout)
        (self.root / "demo.yaml").write_text(VALID.replace("allowed_cidr", "presenter_cidr"))
        result = run_demo(self.root, "status", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TF_VAR_presenter_cidr=203.0.113.4/32", result.stdout)

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
        self.assertIn("about $1.50 to $2.50 per hour", str(events[3][1]))
        self.assertIn("estimate, not a bill", str(events[3][1]).lower())

    def _load_module(self):
        loader = __import__("importlib.machinery").machinery.SourceFileLoader("demo_cidr_under_test", str(self.root / "demo"))
        spec = __import__("importlib.util").util.spec_from_loader(loader.name, loader)
        module = __import__("importlib.util").util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_create_with_empty_allowed_cidr_detects_ip_once_and_forwards_it(self) -> None:
        (self.root / "demo.yaml").write_text(VALID.replace("203.0.113.4/32", ""))
        module = self._load_module()
        commands: list[list[str]] = []
        with (
            mock.patch.object(module, "detect_public_ipv4", return_value="198.51.100.9") as detect,
            mock.patch.object(module, "write_env"),
            mock.patch.object(module, "invoke", side_effect=lambda command, dry_run: commands.append(command)),
            mock.patch.object(module, "prompt"),
            mock.patch.object(sys, "argv", ["demo", "create"]),
            mock.patch("builtins.print") as printed,
        ):
            self.assertEqual(module.main(), 0)
        detect.assert_called_once()
        self.assertEqual(len(commands), 3)
        for command in commands:
            self.assertIn("PRESENTER_CIDR=198.51.100.9/32", command)
            self.assertIn("TF_VAR_presenter_cidr=198.51.100.9/32", command)
        self.assertIn("allowed_cidr not set; using your current IP 198.51.100.9/32", str(printed.call_args_list))

    def test_create_fails_before_anything_runs_when_ip_detection_fails(self) -> None:
        (self.root / "demo.yaml").write_text(VALID.replace("203.0.113.4/32", ""))
        module = self._load_module()
        with (
            mock.patch.object(module.urllib.request, "urlopen", side_effect=OSError("offline")),
            mock.patch.object(module, "write_env") as write_env,
            mock.patch.object(module, "invoke") as invoke,
            mock.patch.object(sys, "argv", ["demo", "create", "--yes"]),
            mock.patch("sys.stderr") as stderr,
        ):
            self.assertEqual(module.main(), 2)
        write_env.assert_not_called()
        invoke.assert_not_called()
        self.assertIn("set allowed_cidr in demo.yaml", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_detect_public_ipv4_rejects_non_ipv4_answers(self) -> None:
        module = self._load_module()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>captive portal</html>"
        with mock.patch.object(module.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(ValueError):
                module.detect_public_ipv4()
        response.__enter__.return_value.read.return_value = b"198.51.100.9\n"
        with mock.patch.object(module.urllib.request, "urlopen", return_value=response):
            self.assertEqual(module.detect_public_ipv4(), "198.51.100.9")

    def test_dry_run_create_with_empty_cidr_stays_offline(self) -> None:
        (self.root / "demo.yaml").write_text(VALID.replace("203.0.113.4/32", ""))
        result = run_demo(self.root, "create", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("allowed_cidr not set", result.stderr)

    def test_allowed_cidr_must_be_a_single_ipv4_slash_32(self) -> None:
        for bad in ("203.0.113.4", "203.0.113.0/24", "2001:db8::1/128", "not-a-cidr"):
            with self.subTest(value=bad):
                (self.root / "demo.yaml").write_text(VALID.replace("203.0.113.4/32", bad))
                result = run_demo(self.root, "status", "--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("/32", result.stderr)

    def test_every_make_action_declares_hybrid_topology(self) -> None:
        for action in ("create", "destroy", "status", "links", "reset"):
            with self.subTest(action=action):
                result = run_demo(self.root, action, "--dry-run", "--yes")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("TOPOLOGY=hybrid", result.stdout)

    # --- mode: local ---------------------------------------------------------------------------------
    LOCAL = "mode: local\n" + VALID.replace("layers: releases,restock", "layers: releases,dd-synthetics")

    def _write(self, content: str) -> None:
        (self.root / "demo.yaml").write_text(content)
        os.chmod(self.root / "demo.yaml", 0o600)

    def test_mode_defaults_to_cloud_and_rejects_unknown_values(self) -> None:
        result = run_demo(self.root, "status", "--dry-run")
        self.assertIn("MODE=cloud", result.stdout)
        self._write("mode: laptop\n" + VALID)
        result = run_demo(self.root, "status", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mode must be local or cloud", result.stderr)

    def test_local_dry_run_maps_every_command_to_the_local_targets(self) -> None:
        self._write(self.LOCAL)
        expected = {
            "create": ["local-up LOCAL_LAYERS=core,releases"],
            "destroy": ["local-down"],
            "status": [" status", " links"],
            "links": [" links"],
            "reset": [" reset"],
        }
        for command, snippets in expected.items():
            with self.subTest(command=command):
                result = run_demo(self.root, command, "--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("MODE=dev STACK=dev", result.stdout)
                for forbidden in ("MODE=cloud", "TOPOLOGY=hybrid", "stack-", "CONFIRM=yes", "AWS_PROFILE", "PRESENTER_CIDR"):
                    self.assertNotIn(forbidden, result.stdout)
                for snippet in snippets:
                    self.assertIn(snippet, result.stdout)
                self.assertNotIn("dd_api_12345678", result.stdout + result.stderr)
                self.assertFalse((self.root / ".env").exists())
        create = run_demo(self.root, "create", "--dry-run")
        self.assertIn("layer dd-synthetics is cloud-only; skipped locally", create.stderr)
        self.assertNotIn("PURGE", run_demo(self.root, "destroy", "--dry-run").stdout)
        self.assertIn("local-down PURGE=1", run_demo(self.root, "destroy", "--dry-run", "--purge").stdout)

    def test_local_layers_all_and_core(self) -> None:
        module = self._load_module()
        self.assertEqual(module.local_layers("all"), (["releases", "restock", "offers"], []))
        self.assertEqual(module.local_layers("core"), ([], []))
        self.assertEqual(module.local_layers("core,offers,control-center,dd-streams"), (["offers"], ["control-center", "dd-streams"]))

    def test_local_needs_only_datadog_and_ignores_cloud_placeholders(self) -> None:
        self._write("mode: local\ndatadog_site: datadoghq.eu\ndd_api_key: dd_api_12345678\n")
        self.assertEqual(run_demo(self.root, "create", "--dry-run").returncode, 0)
        self._write(self.LOCAL.replace("CS12345678", "REPLACE_WITH_CONFLUENT_API_SECRET"))  # the one-line flip
        self.assertEqual(run_demo(self.root, "create", "--dry-run").returncode, 0)
        self._write("mode: local\ndatadog_site: datadoghq.eu\ndd_api_key: REPLACE_ME\n")
        result = run_demo(self.root, "create", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("'dd_api_key' still contains a placeholder", result.stderr)
        self._write("mode: local\ndatadog_site: datadoghq.eu\n")
        self.assertIn("missing required key(s): dd_api_key", run_demo(self.root, "status", "--dry-run").stderr)
        self._write(self.LOCAL + "typo_key: x\n")
        self.assertIn("unknown key(s): typo_key", run_demo(self.root, "status", "--dry-run").stderr)

    def test_local_create_keeps_the_cloud_keys_already_in_env_and_warns_about_a_live_cloud_stack(self) -> None:
        self._write(self.LOCAL.replace("dd_api_12345678", "dd_api_local0001"))
        env_file = self.root / ".env"
        env_file.write_text("# Generated by ./demo from demo.yaml; mode 0600. Do not edit secrets here.\nDD_SITE=datadoghq.eu\n"
                            "DD_API_KEY=dd_api_cloud0001\nCONFLUENT_CLOUD_API_KEY=CKcloud0001\nDD_HOSTNAME=laptop\n")
        (self.root / ".env.cloud-hybrid").write_text("X=1\n")
        mock_make = self.root / "make"
        mock_make.write_text("#!/usr/bin/env python3\nimport sys\nprint('MAKE ' + ' '.join(sys.argv[1:]))\n")
        mock_make.chmod(0o755)
        result = subprocess.run([str(self.root / "demo"), "create"], cwd=self.root, text=True, capture_output=True,
                                env={**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}"}, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MAKE --no-print-directory -C", result.stdout)
        self.assertIn("MODE=dev STACK=dev", result.stdout)
        self.assertIn("local-up LOCAL_LAYERS=core,releases", result.stdout)
        self.assertIn("cloud stack(s) hybrid may still be running and billing", result.stderr)
        text = env_file.read_text()
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertIn("DD_API_KEY=dd_api_local0001", text)
        self.assertNotIn("dd_api_cloud0001", text)
        self.assertIn("CONFLUENT_CLOUD_API_KEY=CKcloud0001", text)  # a later cloud ./demo destroy still has it
        self.assertIn("DD_HOSTNAME=laptop", text)
        self.assertEqual(text.count("# Generated by ./demo"), 1)
        self.assertNotIn("dd_api_local0001", result.stdout + result.stderr)

    def test_local_destroy_keeps_volumes_unless_asked(self) -> None:
        self._write(self.LOCAL)
        module = self._load_module()
        commands: list[list[str]] = []
        for argv, tty, answer, purge in ((["demo", "destroy"], False, "", False), (["demo", "destroy", "--yes"], True, "y", False),
                                          (["demo", "destroy"], True, "y", True), (["demo", "destroy"], True, "", False),
                                          (["demo", "destroy", "--purge"], False, "", True)):
            with self.subTest(argv=argv, tty=tty, answer=answer):
                commands.clear()
                with (
                    mock.patch.object(module, "invoke", side_effect=lambda command, dry_run: commands.append(command)),
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(module.sys.stdin, "isatty", return_value=tty),
                    mock.patch("builtins.input", return_value=answer),
                    mock.patch("builtins.print"),
                ):
                    self.assertEqual(module.main(), 0)
                self.assertEqual(commands[0][-2 if purge else -1], "local-down")
                self.assertEqual("PURGE=1" in commands[0], purge)

    def test_purge_is_refused_in_cloud_mode(self) -> None:
        result = run_demo(self.root, "destroy", "--dry-run", "--purge")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--purge is for mode: local only", result.stderr)

    def test_public_root_files_and_submodule_contract_are_present(self) -> None:
        ignored = (self.root / ".gitignore").read_text()
        for entry in (".env*", "demo.yaml", ".state/", "account-down_override.tf.json", ".terraform/", "*.tfstate*", "*.tfplan", "node_modules/", "logs/"):
            self.assertIn(entry, ignored)
        modules = (self.root / ".gitmodules").read_text()
        self.assertIn("vendor/jr", modules)
        self.assertIn("https://github.com/gianlucanatali/jr.git", modules)
        self.assertIn("branch = sql-producer", modules)

    def _fake_stack_make(self, outcome: str, code: int) -> None:
        """A make on PATH that plays stack.sh for stack-up/stack-down: writes .state/stack-<stack>.outcome, then exits."""
        fake = self.root / "make"
        fake.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import os, sys
            from pathlib import Path
            args = sys.argv[1:]
            if not any(a in ("stack-up", "stack-down") for a in args):
                sys.exit(0)
            assert os.environ.get("DEMO_BANNER") == "1", "./demo must tell stack.sh it prints the banner itself"
            stack = next(a.split("=", 1)[1] for a in args if a.startswith("STACK="))
            outcome = {outcome!r}
            if outcome:
                state = Path({str(self.root)!r}) / ".state"
                state.mkdir(exist_ok=True)
                (state / f"stack-{{stack}}.outcome").write_text(outcome)
            print("stack.sh output", flush=True)
            if {code}:
                print("make: *** [stack-down] Error 1", file=sys.stderr)
            sys.exit({code})
            """))
        fake.chmod(0o755)

    def _run_with_fake_make(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.root / "demo"), *args], cwd=self.root, text=True, capture_output=True,
                              env={**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}"}, check=False, timeout=10)

    RULE = "#" * 60

    def test_failed_destroy_exits_with_stack_sh_code_and_prints_the_banner_last(self) -> None:
        banner = [self.RULE, "DESTROY INCOMPLETE: stack hybrid is STILL BILLING (estimate: AWS about $0.50/h)",
                  "Left: AWS ec2 instance/i-0abc", "Cause: aws layer failed: Error: no such host",
                  "Fix: ./demo destroy --yes   (it resumes)", self.RULE]
        self._fake_stack_make("exit=1\n" + "\n".join(banner) + "\n", 2)
        result = self._run_with_fake_make("destroy", "--yes")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr.strip().splitlines()[-len(banner):], banner)  # nothing after the banner
        self.assertLess(result.stderr.index("make: ***"), result.stderr.index("DESTROY INCOMPLETE"))
        self.assertNotIn("returned non-zero exit status", result.stderr)

    def test_destroy_that_fails_before_stack_sh_reports_still_ends_with_a_banner(self) -> None:
        stale = self.root / ".state" / "stack-hybrid.outcome"
        stale.parent.mkdir(exist_ok=True)
        stale.write_text("exit=0\n#\nDESTROY COMPLETE: nothing left billing for stack hybrid\n#\n")  # an older run's result
        self._fake_stack_make("", 2)
        result = self._run_with_fake_make("destroy", "--yes")
        self.assertEqual(result.returncode, 2)
        lines = result.stderr.strip().splitlines()
        self.assertEqual(lines[-1], self.RULE)
        self.assertIn("DESTROY INCOMPLETE: stack hybrid may be STILL BILLING", result.stderr)
        self.assertIn("make stack-down exited 2", result.stderr)
        self.assertNotIn("DESTROY COMPLETE", result.stderr)

    def test_clean_destroy_prints_destroy_complete_and_exits_zero(self) -> None:
        self._fake_stack_make(f"exit=0\n{self.RULE}\nDESTROY COMPLETE: nothing left billing for stack hybrid\n{self.RULE}\n", 0)
        result = self._run_with_fake_make("destroy", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.strip().splitlines()[-2], "DESTROY COMPLETE: nothing left billing for stack hybrid")

    def test_failed_create_exits_non_zero_with_the_create_failed_banner_last(self) -> None:
        banner = [self.RULE, "CREATE FAILED: stack hybrid is half-built and STILL BILLING (estimate: AWS about $0.50/h)",
                  "Half-built (Terraform state): cloud(12 resources) aws(40 resources)", "Cause: terraform apply failed in terraform/aws",
                  "Fix: fix the cause, then ./demo create --yes (it resumes), or ./demo destroy --yes to stop the billing", self.RULE]
        self._fake_stack_make("exit=1\n" + "\n".join(banner) + "\n", 2)
        result = self._run_with_fake_make("create", "--yes")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr.strip().splitlines()[-len(banner):], banner)

    def _stderr_on_a_terminal(self, *args: str, no_color: bool = False) -> tuple[int, str]:
        """Run ./demo with stderr on a pseudo-terminal (colour applies) and return (exit code, what the terminal got)."""
        import pty
        master, slave = pty.openpty()
        env = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}"}
        env.pop("NO_COLOR", None)
        if no_color:
            env["NO_COLOR"] = "1"
        process = subprocess.Popen([str(self.root / "demo"), *args], cwd=self.root, stdout=subprocess.DEVNULL,
                                   stderr=slave, env=env)
        os.close(slave)
        chunks = []
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # the terminal is closed once ./demo has exited
                break
            if not chunk:
                break
            chunks.append(chunk)
        os.close(master)
        return process.wait(timeout=10), b"".join(chunks).decode()

    def test_banner_is_bold_red_or_green_on_a_terminal_and_plain_with_no_color(self) -> None:
        red, green, reset = "\033[1;31m", "\033[1;32m", "\033[0m"
        self._fake_stack_make(f"exit=1\n{self.RULE}\nDESTROY INCOMPLETE: stack hybrid is STILL BILLING\n{self.RULE}\n", 2)
        code, screen = self._stderr_on_a_terminal("destroy", "--yes")
        self.assertEqual(code, 1, screen)
        self.assertIn(f"{red}DESTROY INCOMPLETE: stack hybrid is STILL BILLING{reset}", screen)
        self.assertIn(f"{red}{self.RULE}{reset}", screen)
        code, screen = self._stderr_on_a_terminal("destroy", "--yes", no_color=True)
        self.assertEqual(code, 1, screen)
        self.assertNotIn("\033[", screen)
        self.assertIn(f"{self.RULE}\r\nDESTROY INCOMPLETE: stack hybrid is STILL BILLING\r\n{self.RULE}", screen)
        self._fake_stack_make(f"exit=0\n{self.RULE}\nDESTROY COMPLETE: nothing left billing for stack hybrid\n{self.RULE}\n", 0)
        code, screen = self._stderr_on_a_terminal("destroy", "--yes")
        self.assertEqual(code, 0, screen)
        self.assertIn(f"{green}DESTROY COMPLETE: nothing left billing for stack hybrid{reset}", screen)
        result = self._run_with_fake_make("destroy", "--yes")  # a pipe, not a terminal: plain text
        self.assertNotIn("\033[", result.stderr)
        self.assertIn("DESTROY COMPLETE: nothing left billing for stack hybrid", result.stderr)

    def _fake_failing_preflight(self, log_lines: list[str], code: int = 2, name: str = "hybrid-preflight-20261008-101421.log") -> str:
        """A make that fails stack-preflight after writing a stack.sh style log; returns the log path."""
        logs = self.root / ".state" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / name
        text = "\n".join([f"== log started: {log}", *log_lines, f"== log finished: {log} (exit 1)"]) + "\n"
        fake = self.root / "make"
        fake.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys
            from pathlib import Path
            if "stack-preflight" not in sys.argv:
                sys.exit(0)
            Path({str(log)!r}).write_text({text!r})
            print("make: *** [stack-preflight] Error 1", file=sys.stderr)
            sys.exit({code})
            """))
        fake.chmod(0o755)
        return str(log)

    FAILED = "\u2717 create failed at stack-preflight: "

    def test_make_step_failure_prints_one_block_with_cause_fix_and_log_and_keeps_the_exit_code(self) -> None:
        log = self._fake_failing_preflight(["== [preflight]", "stack.sh: AWS login session unavailable; run: aws login --profile dd-demo"], code=7)
        result = self._run_with_fake_make("create", "--yes")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn(f"{self.FAILED}AWS login session is missing or has expired\n  Fix: aws login --profile dd-demo, then run the same command again\n  Log: {log}\n", result.stderr)
        for noise in ("Traceback", "CalledProcessError", "returned non-zero", "MODE=cloud", "--no-print-directory"):
            self.assertNotIn(noise, result.stderr)
        self.assertNotIn("\033[", result.stderr)

    def test_unknown_failure_falls_back_to_the_last_terraform_error_line(self) -> None:
        log = self._fake_failing_preflight([
            "stack.sh: step 'terraform aws' FAILED (command: terraform apply)",
            "\u2577", "\u2502 Error: creating EC2 Instance: InsufficientInstanceCapacity: no t4g.xlarge left", "\u2502 ", "\u2502   with aws_instance.vm,", "\u2575"])
        result = self._run_with_fake_make("create", "--yes")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"{self.FAILED}Error: creating EC2 Instance: InsufficientInstanceCapacity: no t4g.xlarge left\n", result.stderr)
        self.assertIn("  Fix: read the log, fix the cause, then run the same command again", result.stderr)
        self.assertIn(f"  Log: {log}", result.stderr)

    def test_stack_sh_line_is_the_cause_when_no_error_block_exists(self) -> None:
        self._fake_failing_preflight(["stack.sh: OWNER 'a b' is not valid"])
        result = self._run_with_fake_make("create", "--yes")
        self.assertIn(f"{self.FAILED}OWNER 'a b' is not valid\n", result.stderr)

    def test_known_breakers_map_to_a_cause_and_an_exact_fix(self) -> None:
        cases = {
            "expired mid-apply": (["Error: operation error STS: ExpiredToken: The security token included in the request is expired"], "AWS login", "aws login --profile demo-profile"),
            "confluent": (["Error: 401 Unauthorized", "  with confluent_environment.main,"], "Confluent Cloud rejected", "confluent_cloud_api_key"),
            "datadog": (["Error: error validating provider credentials: 403 Forbidden", "  with provider[\"datadog\"]"], "Datadog rejected", "dd_api_key, dd_app_key"),
            "ip changed": (["ssh: connect to host 198.51.100.7 port 22: Operation timed out"], "public IP probably changed", "allowed_cidr"),
            "cloud-init": (["ssh: connect to host 198.51.100.7 port 22: Connection refused", "cloud-init status: running"], "cloud-init has not finished", "wait 2 to 3 minutes"),
            "state lock": (["Error: Error acquiring the state lock", "  ID:        1234-abcd"], "state is locked", "terraform force-unlock 1234-abcd"),
            "docker": (["Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"], "Docker is not running", "start Docker"),
        }
        for name, (lines, cause, fix) in cases.items():
            with self.subTest(case=name):
                self._fake_failing_preflight(lines)
                result = self._run_with_fake_make("create", "--yes")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(cause, result.stderr)
                self.assertIn(fix, result.stderr)

    def test_failure_block_is_red_on_a_terminal_and_plain_with_no_color(self) -> None:
        self._fake_failing_preflight(["stack.sh: AWS login session unavailable; run: aws login --profile dd-demo"])
        code, screen = self._stderr_on_a_terminal("create", "--yes")
        self.assertEqual(code, 2, screen)
        self.assertIn("\033[1;31m\u2717 create failed at stack-preflight: AWS login session is missing or has expired", screen)
        self.assertTrue(screen.rstrip().endswith("\033[0m"), repr(screen))
        code, screen = self._stderr_on_a_terminal("create", "--yes", no_color=True)
        self.assertEqual(code, 2, screen)
        self.assertNotIn("\033[", screen)
        self.assertIn(self.FAILED + "AWS login session is missing or has expired", screen)

    def test_log_from_an_earlier_run_is_not_blamed(self) -> None:
        old = self.root / ".state" / "logs" / "hybrid-preflight-old.log"
        old.parent.mkdir(parents=True)
        old.write_text("stack.sh: AWS login session unavailable\n")
        os.utime(old, (1, 1))
        fake = self.root / "make"
        fake.write_text("#!/bin/sh\ncase \"$*\" in *stack-preflight*) exit 3;; esac\n")
        fake.chmod(0o755)
        result = self._run_with_fake_make("create", "--yes")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("exited 3 before stack.sh wrote a log", result.stderr)
        self.assertNotIn("AWS login", result.stderr)

    def test_jr_dockerfile_is_resolved_from_the_jr_build_context(self) -> None:
        compose = (self.root / "compose" / "compose.yaml").read_text()
        makefile = (self.root / "Makefile").read_text()
        self.assertIn("dockerfile: ${JR_DOCKERFILE:-../../jr/Dockerfile}", compose)
        self.assertIn("JR_DOCKERFILE ?= $(if $(wildcard $(CURDIR)/vendor/jr),../../jr/Dockerfile,../../overlay/jr/Dockerfile)", makefile)


if __name__ == "__main__":
    unittest.main()
