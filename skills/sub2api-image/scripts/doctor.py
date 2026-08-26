#!/usr/bin/env python3
"""Diagnose Sub2API image configuration without sending an image request."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from image_client import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_TIMEOUT_SECONDS,
    default_stream_for_profile,
    ConfigError,
    ImageClient,
    SkillError,
    discover_config_path,
    load_config,
    max_images_per_request,
    public_error,
    read_config_state,
)


def _nearest_existing_parent(path: Path) -> Path | None:
    selected = path
    while not selected.exists() and selected != selected.parent:
        selected = selected.parent
    return selected if selected.exists() else None


def _output_check(path: Path | str) -> dict[str, Any]:
    selected = Path(path).expanduser().resolve()
    parent = selected if selected.is_dir() else _nearest_existing_parent(selected)
    is_directory = selected.is_dir() if selected.exists() else None
    writable = bool(parent and parent.is_dir() and os.access(parent, os.W_OK))
    return {
        "ok": (is_directory is not False) and writable,
        "path": str(selected),
        "exists": selected.exists(),
        "is_directory": is_directory,
        "nearest_existing_parent": str(parent) if parent is not None else None,
        "parent_writable": writable,
        "write_test_performed": False,
    }


def _codex_paths() -> dict[str, Any]:
    configured = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    config_path = codex_home / "config.toml"
    sandbox_log = codex_home / ".sandbox" / "sandbox.log"
    return {
        "codex_home": str(codex_home.resolve()),
        "config_path": str(config_path.resolve()),
        "config_exists": config_path.is_file(),
        "sandbox_log_path": str(sandbox_log.resolve()),
        "sandbox_log_exists": sandbox_log.is_file(),
    }


def run_doctor(
    *,
    config_path: Path | str | None = None,
    output_dir: Path | str | None = None,
    network: bool = False,
    network_timeout_seconds: int = 30,
) -> dict[str, Any]:
    if not 1 <= network_timeout_seconds <= 300:
        raise ConfigError("Doctor network timeout must be between 1 and 300 seconds")
    selected_path = discover_config_path(config_path)
    runtime = {
        "ok": sys.version_info >= (3, 10),
        "python": platform.python_version(),
        "executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(),
        "native_windows": os.name == "nt",
    }
    report: dict[str, Any] = {
        "ok": False,
        "operation": "doctor",
        "image_request_sent": False,
        "billing_expected": False,
        "runtime": runtime,
        "codex": _codex_paths(),
    }

    config = None
    try:
        config = load_config(config_path)
        credential_protection = "environment"
        credential_readable = True
        if selected_path.is_file():
            state = read_config_state(selected_path)
            credential_protection = state.credential_protection or "posix-mode-0600"
            credential_readable = state.credential_error is None
            if not credential_readable and os.environ.get("SUB2API_IMAGE_API_KEY"):
                credential_protection = "environment_override"
        report["configuration"] = {
            "ok": True,
            "path": str(selected_path.resolve()),
            "base_url": config.base_url,
            "api_key": "<configured>",
            "model": config.model,
            "timeout_seconds": config.timeout_seconds,
            "provider_profile": config.provider_profile,
            "default_stream": default_stream_for_profile(config.provider_profile),
            "max_images_per_request": max_images_per_request(config.provider_profile),
            "credential_protection": credential_protection,
            "credential_readable": credential_readable,
        }
        if config.timeout_seconds < DEFAULT_TIMEOUT_SECONDS:
            report["configuration"]["warning"] = (
                f"Configured timeout is {config.timeout_seconds} seconds; paid image "
                f"workflows should use at least {DEFAULT_TIMEOUT_SECONDS} seconds"
            )
    except Exception as exc:
        report["configuration"] = {
            "ok": False,
            "path": str(selected_path.resolve()),
            "error": public_error(exc)["error"],
        }

    selected_output = output_dir or (config.output_dir if config is not None else DEFAULT_OUTPUT_DIR)
    report["output"] = _output_check(selected_output)

    if network:
        if config is None:
            report["network"] = {
                "ok": False,
                "image_request_sent": False,
                "billing_expected": False,
                "error": {
                    "category": "configuration",
                    "message": "Network diagnostics require a readable configuration",
                },
            }
        else:
            try:
                network_config = replace(
                    config,
                    timeout_seconds=min(
                        config.timeout_seconds,
                        network_timeout_seconds,
                    ),
                )
                report["network"] = ImageClient(network_config).probe_health()
                report["network"]["timeout_seconds"] = network_config.timeout_seconds
            except Exception as exc:
                report["network"] = {
                    "ok": False,
                    "image_request_sent": False,
                    "billing_expected": False,
                    "error": public_error(exc)["error"],
                }

    checks = [runtime["ok"], report["configuration"]["ok"], report["output"]["ok"]]
    if network:
        checks.append(report["network"]["ok"])
    report["ok"] = all(checks)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose Sub2API image setup without generating an image"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"Configuration path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--output-dir", type=Path, help="Output directory to inspect")
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Network diagnostic timeout in seconds (default: 30, maximum: 300)",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Check TLS and the /health ingress route without image billing",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_doctor(
            config_path=args.config,
            output_dir=args.output_dir,
            network=args.network,
            network_timeout_seconds=args.timeout,
        )
    except SkillError as exc:
        report = public_error(exc)
    except Exception as exc:
        report = public_error(exc)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
