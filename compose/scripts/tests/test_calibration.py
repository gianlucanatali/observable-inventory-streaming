"""Offline calibration contract: no credentials or cloud executables used."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

OVERLAY = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('calibration', OVERLAY / 'compose/scripts/calibration.py')
assert SPEC is not None and SPEC.loader is not None
CALIBRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CALIBRATION)


class CalibrationTests(unittest.TestCase):
    def make(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('TF_VAR_', 'MAKE', 'FARGATE_'))}
        return subprocess.run(
            ['make', '--no-print-directory', '-C', str(OVERLAY),
             'MODE=cloud', 'STACK=brand-new', *args],
            env=env, capture_output=True, text=True,
        )

    def test_arbitrary_name_gets_size_profile(self):
        for stack in ('brand-new', 'rehearsal', 'hybrid'):
            result = self.make('calibration-check', f'STACK={stack}')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('calibration.t4g.xlarge.env', result.stdout)
            self.assertIn('calibration.fargate-512-1024.env', result.stdout)

    def test_dev_does_not_need_cloud_profile(self):
        result = self.make('MODE=dev', 'calibration-check', 'TF_VAR_instance_type=unknown')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_sizes_block_preflight_before_lifecycle(self):
        for selector in ('TF_VAR_instance_type=t4g.unknown', 'FARGATE_SIZE=999-999'):
            for target in ('stack-preflight', 'stack-up'):
                result = self.make(target, 'STACK_SH=/usr/bin/false', selector)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('no matching tracked calibration', result.stderr)
                self.assertNotIn('/usr/bin/false', result.stdout)

    def test_effective_values_reach_lifecycle_and_recursive_make(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / 'lifecycle'
            stub.write_text('#!/usr/bin/env python3\nimport json, os, subprocess\n'
                            'print(json.dumps({k:os.environ[k] for k in '
                            '["TF_VAR_instance_type", "TF_VAR_service_sizing", "FARGATE_SIZE", "DC"]}))\n'
                            'subprocess.run(["make", "--no-print-directory", "calibration-check"], check=True)\n')
            stub.chmod(0o755)
            result = self.make('stack-preflight', f'STACK_SH={stub}')
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{')))
            self.assertEqual(payload['TF_VAR_instance_type'], 't4g.xlarge')
            sizing = json.loads(payload['TF_VAR_service_sizing'])
            self.assertEqual(sizing['inventory-api-110'], {'cpu': 512, 'memory': 1024})
            for release in ('inventory-api-100', 'inventory-api-120'):
                self.assertEqual(sizing[release], sizing['inventory-api-110'], release)
            self.assertEqual(len(sizing), 8)
            self.assertIn('--env-file ' + str(OVERLAY / 'compose/calibration.t4g.xlarge.env'), payload['DC'])
            self.assertIn('--env-file ' + str(OVERLAY / 'compose/calibration.fargate-512-1024.env'), payload['DC'])

    def test_fargate_size_must_match_tracked_cpu_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / 'overlay'
            compose = overlay / 'compose'
            compose.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            (compose / 'calibration.t4g.xlarge.env').write_text('CATALOGUE_PRODUCTS=3000\n')
            (compose / 'calibration.fargate-1024-2048.env').write_text(
                'TF_VAR_service_sizing={"inventory-api-110":{"cpu":2048,"memory":4096}}\n')
            subprocess.run(['git', '-C', tmp, 'add', 'overlay'], check=True)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                CALIBRATION.profiles(overlay, 't4g.xlarge', '1024-2048')

    def test_untracked_file_does_not_count_as_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / 'overlay'
            compose = overlay / 'compose'
            compose.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            (compose / 'calibration.t4g.xlarge.env').write_text('CATALOGUE_PRODUCTS=3000\n')
            with self.assertRaisesRegex(ValueError, 'no matching tracked calibration'):
                CALIBRATION.profiles(overlay, 't4g.xlarge', '512-1024')


if __name__ == '__main__':
    unittest.main()
