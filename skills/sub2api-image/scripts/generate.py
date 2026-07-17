#!/usr/bin/env python3
"""Generate images with Sub2API and save validated files locally."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from image_client import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    ImageClient,
    load_config,
    print_json,
    public_error,
    resolve_size,
    safe_response_metadata,
    save_response_images,
    validate_model,
)


OUTPUT_FORMATS = ("png", "jpeg", "webp")


def validate_prompt(value: str) -> str:
    prompt = value.strip()
    if not prompt:
        raise ConfigError("Image prompt is empty")
    if len(prompt) > 100_000:
        raise ConfigError("Image prompt is too long")
    return prompt


def orientation_for(width: int, height: int) -> str:
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def request_id(headers: dict[str, str]) -> str | None:
    for name in ("x-request-id", "openai-request-id", "cf-ray"):
        if headers.get(name):
            return headers[name]
    return None


def generate_images(
    config: Config,
    *,
    prompt: str,
    tier: str = "1K",
    orientation: str = "square",
    exact_size: str | None = None,
    model: str | None = None,
    count: int = 1,
    quality: str | None = None,
    output_format: str = "png",
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    if not 1 <= count <= 10:
        raise ConfigError("Image count must be between 1 and 10")
    normalized_format = output_format.lower().strip()
    if normalized_format not in OUTPUT_FORMATS:
        raise ConfigError("Output format must be png, jpeg, or webp")
    selected_model = validate_model(model or config.model)
    selected_prompt = validate_prompt(prompt)
    requested_size, requested_tier = resolve_size(tier, orientation, exact_size)
    requested_width, requested_height = map(int, requested_size.split("x"))
    requested_orientation = orientation_for(requested_width, requested_height)

    payload: dict[str, Any] = {
        "model": selected_model,
        "prompt": selected_prompt,
        "size": requested_size,
        "n": count,
        "response_format": "b64_json",
        "output_format": normalized_format,
    }
    if quality:
        payload["quality"] = quality.strip()

    started = time.monotonic()
    client = ImageClient(config)
    response, headers = client.generate(payload)
    images = save_response_images(
        client, response, output_dir or config.output_dir, operation="generate"
    )
    elapsed = round(time.monotonic() - started, 3)
    tier_match = all(image["actual_tier"] == requested_tier for image in images)
    orientation_match = all(
        orientation_for(image["width"], image["height"]) == requested_orientation
        for image in images
    )
    exact_size_match = all(image["actual_size"] == requested_size for image in images)
    ok = tier_match and orientation_match and (not exact_size or exact_size_match)

    report: dict[str, Any] = {
        "ok": ok,
        "operation": "generate",
        "model": selected_model,
        "requested_size": requested_size,
        "requested_tier": requested_tier,
        "requested_orientation": requested_orientation,
        "requested_count": count,
        "output_format": normalized_format,
        "tier_match": tier_match,
        "orientation_match": orientation_match,
        "exact_size_match": exact_size_match,
        "elapsed_seconds": elapsed,
        "images": images,
    }
    identifier = request_id(headers)
    if identifier:
        report["request_id"] = identifier
    metadata = safe_response_metadata(response)
    if metadata:
        report["api_metadata"] = metadata
    if not ok:
        report["error"] = {
            "category": "resolution_mismatch",
            "message": "Returned image dimensions do not match the requested resolution or orientation",
        }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate images through Sub2API")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--tier", choices=("1K", "2K", "4K"), default="1K")
    parser.add_argument(
        "--orientation",
        choices=("square", "landscape", "portrait"),
        default="square",
    )
    parser.add_argument("--size", help="Exact WIDTHxHEIGHT; overrides tier/orientation")
    parser.add_argument("--model")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--quality")
    parser.add_argument("--output-format", choices=OUTPUT_FORMATS, default="png")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        report = generate_images(
            config,
            prompt=args.prompt,
            tier=args.tier,
            orientation=args.orientation,
            exact_size=args.size,
            model=args.model,
            count=args.n,
            quality=args.quality,
            output_format=args.output_format,
            output_dir=args.output_dir,
        )
        print_json(report)
        return 0 if report["ok"] else 3
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
