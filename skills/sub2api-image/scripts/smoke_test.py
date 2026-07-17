#!/usr/bin/env python3
"""Run one low-cost 1K image request against the configured Sub2API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from generate import generate_images
from image_client import DEFAULT_CONFIG_PATH, load_config, print_json, public_error


SMOKE_PROMPT = "A simple blue geometric bird centered on a plain white background"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Sub2API image smoke test")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        report = generate_images(
            config,
            prompt=SMOKE_PROMPT,
            tier="1K",
            orientation="square",
            count=1,
            output_format="png",
            output_dir=args.output_dir,
        )
        report["smoke_test"] = True
        report["billing_verification"] = (
            "Not available to a user API key; verify image_count, image_size, "
            "image_size_source, image_size_breakdown, and charge in Sub2API admin."
        )
        print_json(report)
        return 0 if report["ok"] else 3
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
