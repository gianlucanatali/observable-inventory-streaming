#!/usr/bin/env python3
"""Print the non-secret make defaults that ./demo derives from demo.yaml, for the Makefile.

Usage: demo-yaml.py <path/to/demo.yaml>
Output (one line, space separated):
  <stack> <aws_profile> <aws_region> <layers> <allowed_cidr or -> <keep_images: true|false> <mode: cloud|local> <owner or ->
mode: local reads no cloud keys: the cloud fields print as `-` and the Makefile uses only layers and mode.
The file is parsed and validated by ./demo's own parse_yaml/validate, so make and ./demo accept
exactly the same files. Secrets are never printed. Exit 2 with a message on stderr if invalid.
"""
from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

DEMO_CLI = Path(__file__).resolve().parents[2] / "demo"


def load_demo_cli():
    loader = SourceFileLoader("demo_cli", str(DEMO_CLI))
    spec = importlib.util.spec_from_loader("demo_cli", loader)
    if spec is None:
        raise SystemExit(f"demo-yaml.py: cannot load {DEMO_CLI}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: demo-yaml.py <demo.yaml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    demo = load_demo_cli()
    try:
        values = demo.parse_yaml(path)
        demo.validate(values)
    except ValueError as error:
        print(f"make: {path}: {error}", file=sys.stderr)
        return 2
    print(values.get("stack", "-"), values.get("aws_profile", "-"), values.get("aws_region", "-"), values["layers"],
          values.get("allowed_cidr") or "-", values.get("keep_images", "false"), values["mode"], values.get("owner") or "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
