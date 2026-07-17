#!/usr/bin/env python3
"""Create, inspect, or remove the local Sub2API image client configuration."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from image_client import (
    DEFAULT_BASE_URL,
    DEFAULT_CONFIG_PATH,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_TIMEOUT_SECONDS,
    Config,
    ConfigError,
    config_from_mapping,
    load_config,
    print_json,
    public_error,
    save_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Securely configure the Sub2API image skill."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--base-url", help="Sub2API base URL ending in /v1")
    parser.add_argument("--model", help="Default image model")
    parser.add_argument("--output-dir", help="Default image output directory")
    parser.add_argument("--timeout", type=int, help="Request timeout in seconds")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--show", action="store_true", help="Show non-secret settings")
    actions.add_argument("--revoke", action="store_true", help="Delete local configuration")
    return parser.parse_args()


def remove_config(path: Path) -> dict[str, object]:
    expanded = path.expanduser()
    try:
        expanded.unlink()
        removed = True
    except FileNotFoundError:
        removed = False
    except OSError as exc:
        raise ConfigError(f"Cannot remove configuration at {expanded}: {exc}") from exc
    return {"ok": True, "removed": removed, "config_path": str(expanded.resolve())}


def configure(args: argparse.Namespace) -> dict[str, object]:
    path = args.config.expanduser()
    existing: Config | None = None
    if path.exists():
        existing = load_config(path)

    if not sys.stdin.isatty():
        raise ConfigError(
            "Interactive terminal required so the API key can be read without echo"
        )

    label = "Sub2API user API key (input hidden)"
    if existing is not None:
        label += " [press Enter to keep the existing key]"
    entered_key = getpass.getpass(f"{label}: ")
    api_key = entered_key if entered_key else (existing.api_key if existing else "")

    mapping = {
        "base_url": args.base_url
        or (existing.base_url if existing else DEFAULT_BASE_URL),
        "api_key": api_key,
        "model": args.model or (existing.model if existing else DEFAULT_MODEL),
        "output_dir": args.output_dir
        or (existing.output_dir if existing else DEFAULT_OUTPUT_DIR),
        "timeout_seconds": args.timeout
        if args.timeout is not None
        else (existing.timeout_seconds if existing else DEFAULT_TIMEOUT_SECONDS),
    }
    config = config_from_mapping(mapping)
    written_path = save_config(config, path)
    result = config.public_dict(written_path)
    result["ok"] = True
    result["permissions"] = oct(os.stat(written_path).st_mode & 0o777)
    return result


def main() -> int:
    args = parse_args()
    try:
        if args.revoke:
            result = remove_config(args.config)
        elif args.show:
            config = load_config(args.config)
            result = config.public_dict(args.config.expanduser())
            result["ok"] = True
            result["permissions"] = oct(
                os.stat(args.config.expanduser()).st_mode & 0o777
            )
        else:
            result = configure(args)
        print_json(result)
        return 0
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
