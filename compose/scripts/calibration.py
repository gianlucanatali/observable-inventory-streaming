#!/usr/bin/env python3
"""Resolve tracked, non-secret size profiles without reading runtime env files."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


def profiles(overlay, instance_type, fargate_size):
    for value in (instance_type, fargate_size):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', value):
            raise ValueError('invalid calibration size selector')
    paths = [overlay / 'compose' / f'calibration.{instance_type}.env',
             overlay / 'compose' / f'calibration.fargate-{fargate_size}.env']
    for path in paths:
        tracked = subprocess.run(
            ['git', '-C', str(overlay), 'ls-files', '--error-unmatch',
             '--', str(path)], capture_output=True, text=True,
        )
        if tracked.returncode or not path.is_file() or path.is_symlink():
            raise ValueError(f'no matching tracked calibration: {path.name}; '
                             'add and verify a size profile before stack-up')
    values = dict(line.split('=', 1) for line in paths[1].read_text().splitlines()
                  if line and not line.startswith('#'))
    try:
        sizing = json.loads(values['TF_VAR_service_sizing'])
        regression = sizing['inventory-api-110']
        if f"{regression['cpu']}-{regression['memory']}" != fargate_size:
            raise ValueError(f'{paths[1].name} does not match its CPU-memory size selector')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{paths[1].name}: invalid TF_VAR_service_sizing') from exc
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-type', required=True)
    parser.add_argument('--fargate-size', required=True)
    args = parser.parse_args()
    try:
        paths = profiles(Path(__file__).resolve().parents[2], args.instance_type, args.fargate_size)
    except ValueError as exc:
        print(f'calibration: {exc}', file=sys.stderr)
        return 1
    print(' '.join(map(str, paths)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
